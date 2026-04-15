"""
dashboard/application.py
Flask dashboard for the Fog/Edge project.
Elastic Beanstalk requires the Flask app object to be called 'application'.
Reads from DynamoDB and serves a live auto-refreshing UI.
"""

import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from flask import Flask, jsonify, render_template

application = Flask(__name__)
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ── AWS config (set via Elastic Beanstalk environment variables) ──────────────
REGION         = os.environ.get("AWS_REGION",       "us-east-1")
READINGS_TABLE = os.environ.get("READINGS_TABLE",   "fog_sensor_readings")
LATEST_TABLE   = os.environ.get("LATEST_TABLE",     "fog_sensor_latest")

dynamodb       = boto3.resource("dynamodb", region_name=REGION)
readings_tbl   = dynamodb.Table(READINGS_TABLE)
latest_tbl     = dynamodb.Table(LATEST_TABLE)

# Must match sensor_id values used by the simulator and processor
SENSOR_IDS = [
    "temp-sensor-001",
    "humidity-sensor-002",
    "power-sensor-003",
    "network-sensor-004",
    "airflow-sensor-005",
]

# Maps each sensor to the primary metric key inside its nested 'metrics' dict.
# Used by build_charts() to extract the right value per sensor.
SENSOR_CHART_METRIC = {
    "temp-sensor-001":     "temperature_c",
    "humidity-sensor-002": "humidity_pct",
    "power-sensor-003":    "load_pct",
    "network-sensor-004":  "latency_ms",
    "airflow-sensor-005":  "airflow_cfm",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def from_decimal(obj):
    """Recursively convert DynamoDB Decimals to plain floats."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: from_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [from_decimal(i) for i in obj]
    return obj


def get_latest():
    """Scan fog_sensor_latest for one current row per sensor."""
    try:
        items = latest_tbl.scan().get("Items", [])
        return sorted(from_decimal(items), key=lambda x: x.get("sensor_id", ""))
    except Exception as e:
        log.error("get_latest error: %s", e)
        return []


def get_history(sensor_id: str, limit: int = 20):
    """
    Query fog_sensor_readings for the most recent `limit` readings
    for a given sensor. The lambda_processor writes with pk=sensor_id,
    sk=timestamp, so we query on pk.
    """
    try:
        resp = readings_tbl.query(
            KeyConditionExpression=Key("pk").eq(sensor_id),
            ScanIndexForward=False,
            Limit=limit,
        )
        rows = from_decimal(resp.get("Items", []))
        rows.reverse()   # oldest-first for charts
        return rows
    except Exception as e:
        log.error("get_history error for %s: %s", sensor_id, e)
        return []


def build_charts():
    """
    Build chart data for all 5 sensors.
    Each sensor's primary metric is pulled from the nested 'metrics' dict
    that the simulator/processor stores (e.g. metrics.temperature_c).
    The JS dashboard expects chartData[sensor_id][metric_key] and
    chartData[sensor_id].labels.
    """
    charts = {}
    for sid in SENSOR_IDS:
        rows = get_history(sid, 60)
        metric_key = SENSOR_CHART_METRIC[sid]
        charts[sid] = {
            "labels": [r.get("timestamp", "")[11:16] for r in rows],
            "values": [r.get("metrics", {}).get(metric_key, 0) for r in rows],
        }
    return charts


def collect_alerts(latest):
    """
    Flatten per-sensor alert lists into a single sorted list.
    Alerts are embedded in each latest row by the processor
    (copied verbatim from the simulator payload).
    CRITICAL alerts sort to the top.
    """
    alerts = []
    for item in latest:
        for a in item.get("alerts", []):
            alerts.append({
                "sensor_id": item.get("sensor_id"),
                "metric":    a.get("metric"),
                "level":     a.get("level"),
                "value":     a.get("value"),
                "timestamp": item.get("last_updated", ""),
            })
    return sorted(alerts, key=lambda x: 0 if x["level"] == "CRITICAL" else 1)


def overall_sla(latest):
    """Aggregate SLA health across all sensors for the summary bar."""
    if not latest:
        return {"score": 0, "healthy": 0, "total": 0, "pct": 0}
    scores  = [item.get("sla_health", 0) for item in latest]
    healthy = sum(1 for item in latest if item.get("sla_met", False))
    return {
        "score":   round(sum(scores) / len(scores), 1),
        "healthy": healthy,
        "total":   len(latest),
        "pct":     round(healthy / len(latest) * 100, 1),
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@application.route("/")
def index():
    latest = get_latest()
    return render_template(
        "dashboard.html",
        latest=latest,
        alerts=collect_alerts(latest),
        sla=overall_sla(latest),
        chart_data=json.dumps(build_charts()),
        now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    )


@application.route("/api/latest")
def api_latest():
    latest = get_latest()
    return jsonify({
        "sensors":    latest,
        "alerts":     collect_alerts(latest),
        "sla":        overall_sla(latest),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


@application.route("/api/history/<sensor_id>")
def api_history(sensor_id):
    if sensor_id not in SENSOR_IDS:
        return jsonify({"error": "Unknown sensor"}), 404
    return jsonify(get_history(sensor_id))


@application.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    application.run(host="0.0.0.0", port=5000, debug=False)