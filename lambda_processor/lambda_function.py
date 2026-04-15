"""
lambda_processor/lambda_function.py
Triggered by AWS IoT Rule whenever a message arrives on fog/sensors/data.
Computes SLA health score, writes to two DynamoDB tables,
and publishes CRITICAL / WARNING alerts to SNS.
"""
import json
import os
import logging
from datetime import datetime, timezone
from decimal import Decimal
import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

READINGS_TABLE = os.environ.get("READINGS_TABLE", "fog_sensor_readings")
LATEST_TABLE   = os.environ.get("LATEST_TABLE",   "fog_sensor_latest")
SNS_TOPIC_ARN  = os.environ.get("SNS_TOPIC_ARN",  "")          # injected by infra.py
REGION         = os.environ.get("AWS_REGION",     "us-east-1")

dynamodb       = boto3.resource("dynamodb", region_name=REGION)
readings_table = dynamodb.Table(READINGS_TABLE)
latest_table   = dynamodb.Table(LATEST_TABLE)
sns_client     = boto3.client("sns", region_name=REGION)


# ── Helpers ───────────────────────────────────────────────────────────────────
def to_decimal(obj):
    """Recursively convert floats → Decimal (DynamoDB requirement)."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_decimal(i) for i in obj]
    return obj


def compute_sla(reading: dict) -> dict:
    """
    SLA health score 0-100, computed per sensor type using its relevant metrics.

    Scoring per sensor type:
      temperature  : score drops linearly above 28°C, 0 at 40°C
      humidity     : score drops linearly above 60%, 0 at 80%
      power        : voltage score (drops below 220V) + load score (rises above 70%)
      network      : latency score + packet loss score
      airflow      : airflow_cfm score (drops below 400) + delta_t score (rises above 12°C)

    status_score   : 100 (OK) | 60 (WARNING) | 20 (CRITICAL)
    final health   : 50% metric score + 50% status score
    """
    metrics     = reading.get("metrics", {})
    sensor_type = reading.get("sensor_type", "unknown")
    status      = reading.get("status", "OK")

    status_score = 100.0 if status == "OK" else 60.0 if status == "WARNING" else 20.0

    if sensor_type == "temperature":
        temp = float(metrics.get("temperature_c", 22.0))
        metric_score = max(0.0, 100.0 - max(0.0, (temp - 28.0) / (40.0 - 28.0) * 100.0))

    elif sensor_type == "humidity":
        hum = float(metrics.get("humidity_pct", 50.0))
        metric_score = max(0.0, 100.0 - max(0.0, (hum - 60.0) / (80.0 - 60.0) * 100.0))

    elif sensor_type == "power":
        voltage = float(metrics.get("voltage_v", 230.0))
        load    = float(metrics.get("load_pct",  50.0))
        v_score = max(0.0, min(100.0, (voltage - 200.0) / (220.0 - 200.0) * 100.0))
        l_score = max(0.0, 100.0 - max(0.0, (load - 70.0) / (90.0 - 70.0) * 100.0))
        metric_score = (v_score + l_score) / 2.0

    elif sensor_type == "network":
        latency     = float(metrics.get("latency_ms",      50.0))
        packet_loss = float(metrics.get("packet_loss_pct",  0.0))
        lat_score  = max(0.0, 100.0 - max(0.0, (latency - 100.0) / (300.0 - 100.0) * 100.0))
        pkt_score  = max(0.0, 100.0 - (packet_loss / 5.0) * 100.0)
        metric_score = (lat_score + pkt_score) / 2.0

    elif sensor_type == "airflow":
        cfm     = float(metrics.get("airflow_cfm", 500.0))
        delta_t = float(metrics.get("delta_t_c",   10.0))
        cfm_score = max(0.0, min(100.0, (cfm - 280.0) / (400.0 - 280.0) * 100.0))
        dt_score  = max(0.0, 100.0 - max(0.0, (delta_t - 12.0) / (20.0 - 12.0) * 100.0))
        metric_score = (cfm_score + dt_score) / 2.0

    else:
        metric_score = 100.0

    health = round(metric_score * 0.5 + status_score * 0.5, 1)

    return {
        "health_score": health,
        "sla_met":      health >= 75.0,
    }


def publish_sns_alert(reading: dict, sla: dict):
    """
    Send an SNS email whenever the sensor status is WARNING or CRITICAL.
    Does nothing if SNS_TOPIC_ARN is not set or status is OK.
    """
    if not SNS_TOPIC_ARN:
        log.warning("SNS_TOPIC_ARN not set – skipping notification")
        return

    status = reading.get("status", "OK")
    if status == "OK":
        return  # no alert needed

    sensor_id   = reading.get("sensor_id",   "unknown")
    sensor_name = reading.get("sensor_name", "unknown")
    sensor_type = reading.get("sensor_type", "unknown")
    location    = reading.get("location",    "unknown")
    timestamp   = reading.get("timestamp",   datetime.now(timezone.utc).isoformat())
    metrics     = reading.get("metrics",     {})
    alerts      = reading.get("alerts",      [])
    health      = sla["health_score"]

    # Build a readable alert list
    alert_lines = "\n".join(
        f"  • {a['metric']} = {a['value']}  [{a['level']}]"
        for a in alerts
    ) or "  • (no specific metric alerts)"

    # Build metrics block
    metric_lines = "\n".join(
        f"  {k}: {v}" for k, v in metrics.items()
    )

    subject = f"[ColoGuard {status}] {sensor_id} – SLA Health {health}%"

    message = f"""
ColoGuard – Fog-Based SLA Monitoring
======================================
Status    : {status}
Sensor ID : {sensor_id}
Name      : {sensor_name}
Type      : {sensor_type}
Location  : {location}
Timestamp : {timestamp}

SLA Health Score : {health} / 100
SLA Met          : {'Yes' if sla['sla_met'] else 'No'}

Current Metrics
---------------
{metric_lines}

Active Alerts
-------------
{alert_lines}

--------------------------------------
This notification was generated automatically by ColoGuard.
Log in to the dashboard to view live sensor data.
""".strip()

    try:
        sns_client.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject,
            Message=message,
        )
        log.info("SNS alert sent for %s (status=%s)", sensor_id, status)
    except Exception as e:
        log.error("Failed to publish SNS alert: %s", e)


# ── Handler ───────────────────────────────────────────────────────────────────
def lambda_handler(event, context):
    log.info("Received: %s", json.dumps(event))

    sensor_id   = event.get("sensor_id",   "unknown")
    sensor_name = event.get("sensor_name", "unknown")
    sensor_type = event.get("sensor_type", "unknown")
    timestamp   = event.get("timestamp",   datetime.now(timezone.utc).isoformat())
    sla         = compute_sla(event)
    now_epoch   = int(datetime.now(timezone.utc).timestamp())

    # ── fog_sensor_readings  (time-series, 7-day TTL) ─────────────────────
    readings_table.put_item(Item=to_decimal({
        **event,
        "pk":         sensor_id,
        "sk":         timestamp,
        "sla_health": sla["health_score"],
        "sla_met":    sla["sla_met"],
        "ttl":        now_epoch + 7 * 86400,
    }))

    # ── fog_sensor_latest  (one row per sensor, always up-to-date) ────────
    latest_table.put_item(Item=to_decimal({
        **event,
        "sensor_id":    sensor_id,
        "last_updated": timestamp,
        "sla_health":   sla["health_score"],
        "sla_met":      sla["sla_met"],
    }))

    # ── SNS alert (only for WARNING / CRITICAL) ───────────────────────────
    publish_sns_alert(event, sla)

    log.info(
        "Stored %s (%s / %s) | SLA health=%.1f met=%s",
        sensor_id, sensor_name, sensor_type,
        sla["health_score"], sla["sla_met"],
    )

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message":     "OK",
            "sensor_id":   sensor_id,
            "sensor_name": sensor_name,
            "sensor_type": sensor_type,
            "sla_health":  sla["health_score"],
            "sla_met":     sla["sla_met"],
        }),
    }