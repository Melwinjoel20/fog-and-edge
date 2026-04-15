"""
lambda_simulator/simulator.py
Triggered by EventBridge every 1 minute.
Generates fake IoT sensor readings and publishes them to AWS IoT Core,
which then fires the FogEdgeLambda processor via the IoT Rule.
No EC2, no MQTT certificates – pure serverless.
"""
import json
import logging
import os
import random
from datetime import datetime, timezone
import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION    = os.environ.get("AWS_IOT_REGION", "us-east-1")
IOT_TOPIC = os.environ.get("IOT_TOPIC",      "fog/sensors/data")
iot_data  = boto3.client("iot-data", region_name=REGION)

SENSORS = [
    {
        "sensor_id":   "temp-sensor-001",
        "sensor_name": "Temperature Sensor - Rack Row A",
        "type":        "temperature",
        "location":    "rack-row-A",
    },
    {
        "sensor_id":   "humidity-sensor-002",
        "sensor_name": "Humidity Sensor - Cooling Zone",
        "type":        "humidity",
        "location":    "cooling-zone",
    },
    {
        "sensor_id":   "power-sensor-003",
        "sensor_name": "Power & UPS Sensor - PDU-1",
        "type":        "power",
        "location":    "pdu-1",
    },
    {
        "sensor_id":   "network-sensor-004",
        "sensor_name": "Network Latency Sensor - Core Switch",
        "type":        "network",
        "location":    "core-switch",
    },
    {
        "sensor_id":   "airflow-sensor-005",
        "sensor_name": "Airflow Sensor - CRAC Unit 1",
        "type":        "airflow",
        "location":    "crac-unit-1",
    },
]

# Alert thresholds per sensor type
THRESHOLDS = {
    "temperature_c":    {"warn": 30.0,  "critical": 40.0},
    "humidity_pct":     {"warn": 65.0,  "critical": 80.0},
    "voltage_v":        {"warn": 210.0, "critical": 200.0},   # below = bad
    "load_pct":         {"warn": 75.0,  "critical": 90.0},
    "latency_ms":       {"warn": 150.0, "critical": 300.0},
    "packet_loss_pct":  {"warn": 1.0,   "critical": 5.0},
    "airflow_cfm":      {"warn": 400.0, "critical": 300.0},   # below = bad
    "delta_t_c":        {"warn": 15.0,  "critical": 20.0},
}


def check_threshold(metric, value) -> dict | None:
    if metric not in THRESHOLDS:
        return None
    t = THRESHOLDS[metric]
    # For metrics where LOW is bad (voltage, airflow)
    if metric in ("voltage_v", "airflow_cfm"):
        if value <= t["critical"]:
            return {"metric": metric, "level": "CRITICAL", "value": value}
        elif value <= t["warn"]:
            return {"metric": metric, "level": "WARNING",  "value": value}
    else:
        if value >= t["critical"]:
            return {"metric": metric, "level": "CRITICAL", "value": value}
        elif value >= t["warn"]:
            return {"metric": metric, "level": "WARNING",  "value": value}
    return None


def generate_reading(sensor: dict) -> dict:
    spike = random.random() < 0.10  # 10% chance of anomaly
    stype = sensor["type"]
    alerts = []
    metrics = {}

    if stype == "temperature":
        metrics["temperature_c"] = round(random.uniform(18.0, 42.0 if spike else 29.0), 2)

    elif stype == "humidity":
        metrics["humidity_pct"] = round(random.uniform(35.0, 85.0 if spike else 64.0), 2)

    elif stype == "power":
        metrics["voltage_v"]  = round(random.uniform(195.0 if spike else 208.0, 240.0), 2)
        metrics["load_pct"]   = round(random.uniform(20.0,  95.0 if spike else 74.0),   2)

    elif stype == "network":
        metrics["latency_ms"]      = round(random.uniform(5.0,   320.0 if spike else 145.0), 1)
        metrics["packet_loss_pct"] = round(random.uniform(0.0,   8.0   if spike else 0.9),   2)

    elif stype == "airflow":
        metrics["airflow_cfm"] = round(random.uniform(280.0 if spike else 380.0, 600.0), 1)
        metrics["delta_t_c"]   = round(random.uniform(5.0,  22.0 if spike else 14.0),   2)

    for metric, value in metrics.items():
        alert = check_threshold(metric, value)
        if alert:
            alerts.append(alert)

    if any(a["level"] == "CRITICAL" for a in alerts):
        status = "CRITICAL"
    elif alerts:
        status = "WARNING"
    else:
        status = "OK"

    return {
        "sensor_id":   sensor["sensor_id"],
        "sensor_name": sensor["sensor_name"],
        "sensor_type": sensor["type"],
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "location":    sensor["location"],
        "metrics":     metrics,
        "status":      status,
        "alerts":      alerts,
        "fog_node_id": "fog-node-lambda-01",
        "datacenter":  "edge-zone-dublin",
    }


def lambda_handler(event, context):
    """EventBridge calls this every minute."""
    published = []
    for sensor in SENSORS:
        reading = generate_reading(sensor)
        iot_data.publish(
            topic=IOT_TOPIC,
            qos=0,
            payload=json.dumps(reading),
        )
        log.info(
            "Published %s (%s) | status=%s | metrics=%s",
            reading["sensor_id"],
            reading["sensor_name"],
            reading["status"],
            reading["metrics"],
        )
        published.append({
            "sensor_id":   reading["sensor_id"],
            "sensor_name": reading["sensor_name"],
            "status":      reading["status"],
        })

    return {
        "statusCode": 200,
        "body": json.dumps({"published": published}),
    }