"""
lambda_simulator/simulator.py
Reduced to 7 sensors to minimise AWS costs.
Triggered by EventBridge (recommended: rate(5 minutes) or rate(15 minutes)).
Publishes to AWS IoT Core → FogEdgeLambda processor via IoT Rule.

Environment variables:
    AWS_IOT_REGION        – AWS region for IoT Core (default: us-east-1)
    IOT_TOPIC             – MQTT topic (default: fog/sensors/data)
    DISPATCH_RATE_MINUTES – How often EventBridge triggers this Lambda (default: 5)
"""
import json
import logging
import os
import random
from datetime import datetime, timezone
import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION                = os.environ.get("AWS_IOT_REGION",        "us-east-1")
IOT_TOPIC             = os.environ.get("IOT_TOPIC",             "fog/sensors/data")
DISPATCH_RATE_MINUTES = int(os.environ.get("DISPATCH_RATE_MINUTES", "5"))

iot_data = boto3.client("iot-data", region_name=REGION)

# ── Sensor fleet (7 sensors, one per category + extras for temp & network) ────
SENSORS = [
    # Temperature – 2 sensors
    {"sensor_id": "temp-sensor-001", "sensor_name": "Temp Sensor – Rack A, Aisle 1",
     "type": "temperature", "location": "rack-row-A", "rack": "A", "aisle": "1", "unit": "1"},
    {"sensor_id": "temp-sensor-002", "sensor_name": "Temp Sensor – Rack B, Aisle 2",
     "type": "temperature", "location": "rack-row-B", "rack": "B", "aisle": "2", "unit": "2"},

    # Humidity – 1 sensor
    {"sensor_id": "humidity-sensor-001", "sensor_name": "Humidity Sensor – Cooling Zone A1",
     "type": "humidity", "location": "cooling-zone-A", "rack": "A", "aisle": "1", "unit": "1"},

    # Power – 1 sensor
    {"sensor_id": "power-sensor-001", "sensor_name": "Power Sensor – PDU-A1",
     "type": "power", "location": "pdu-A1", "rack": "A", "aisle": "1", "unit": "1"},

    # Network – 2 sensors
    {"sensor_id": "network-sensor-001", "sensor_name": "Network Sensor – Core Switch A",
     "type": "network", "location": "core-switch-A", "rack": "A", "aisle": "1", "unit": "1"},
    {"sensor_id": "network-sensor-002", "sensor_name": "Network Sensor – Core Switch B",
     "type": "network", "location": "core-switch-B", "rack": "B", "aisle": "2", "unit": "2"},

    # Airflow – 1 sensor
    {"sensor_id": "airflow-sensor-001", "sensor_name": "Airflow Sensor – CRAC Unit A1",
     "type": "airflow", "location": "crac-unit-A1", "rack": "A", "aisle": "1", "unit": "1"},
]

# Alert thresholds per metric
THRESHOLDS = {
    "temperature_c":   {"warn": 30.0,  "critical": 40.0},
    "humidity_pct":    {"warn": 65.0,  "critical": 80.0},
    "voltage_v":       {"warn": 210.0, "critical": 200.0},   # below = bad
    "load_pct":        {"warn": 75.0,  "critical": 90.0},
    "latency_ms":      {"warn": 150.0, "critical": 300.0},
    "packet_loss_pct": {"warn": 1.0,   "critical": 5.0},
    "airflow_cfm":     {"warn": 400.0, "critical": 300.0},   # below = bad
    "delta_t_c":       {"warn": 15.0,  "critical": 20.0},
}


def check_threshold(metric, value):
    if metric not in THRESHOLDS:
        return None
    t = THRESHOLDS[metric]
    if metric in ("voltage_v", "airflow_cfm"):   # low = bad
        if value <= t["critical"]:
            return {"metric": metric, "level": "CRITICAL", "value": value}
        elif value <= t["warn"]:
            return {"metric": metric, "level": "WARNING",  "value": value}
    else:                                         # high = bad
        if value >= t["critical"]:
            return {"metric": metric, "level": "CRITICAL", "value": value}
        elif value >= t["warn"]:
            return {"metric": metric, "level": "WARNING",  "value": value}
    return None


def generate_reading(sensor: dict) -> dict:
    spike   = random.random() < 0.12   # 12% anomaly chance
    stype   = sensor["type"]
    alerts  = []
    metrics = {}

    if stype == "temperature":
        metrics["temperature_c"] = round(random.uniform(18.0, 42.0 if spike else 29.0), 2)

    elif stype == "humidity":
        metrics["humidity_pct"] = round(random.uniform(35.0, 85.0 if spike else 64.0), 2)

    elif stype == "power":
        metrics["voltage_v"] = round(random.uniform(195.0 if spike else 208.0, 240.0), 2)
        metrics["load_pct"]  = round(random.uniform(20.0,  95.0 if spike else 74.0),   2)

    elif stype == "network":
        metrics["latency_ms"]      = round(random.uniform(5.0,  320.0 if spike else 145.0), 1)
        metrics["packet_loss_pct"] = round(random.uniform(0.0,  8.0   if spike else 0.9),   2)

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
        "rack":        sensor["rack"],
        "aisle":       sensor["aisle"],
        "unit":        sensor["unit"],
        "metrics":     metrics,
        "status":      status,
        "alerts":      alerts,
        "fog_node_id": "fog-node-lambda-01",
        "datacenter":  "edge-zone-dublin",
    }


def lambda_handler(event, context):
    log.info(
        "FogSensorSimulator invoked | dispatch_rate=%d min | sensors=%d | topic=%s",
        DISPATCH_RATE_MINUTES, len(SENSORS), IOT_TOPIC,
    )

    published = []
    for sensor in SENSORS:
        reading = generate_reading(sensor)
        iot_data.publish(topic=IOT_TOPIC, qos=0, payload=json.dumps(reading))
        log.info(
            "Published %s | rack=%s aisle=%s | status=%s | metrics=%s",
            reading["sensor_id"], reading["rack"], reading["aisle"],
            reading["status"], reading["metrics"],
        )
        published.append({
            "sensor_id":   reading["sensor_id"],
            "sensor_name": reading["sensor_name"],
            "rack":        reading["rack"],
            "aisle":       reading["aisle"],
            "status":      reading["status"],
        })

    return {
        "statusCode": 200,
        "body": json.dumps({
            "published":             published,
            "dispatch_rate_minutes": DISPATCH_RATE_MINUTES,
        }),
    }