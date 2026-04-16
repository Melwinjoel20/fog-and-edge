"""
dashboard/application.py
Flask dashboard for the Fog/Edge project — 7-sensor edition.
Elastic Beanstalk requires the Flask app object to be called 'application'.
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

# ── AWS config ────────────────────────────────────────────────────────────────
REGION         = os.environ.get("AWS_REGION",     "us-east-1")
READINGS_TABLE = os.environ.get("READINGS_TABLE", "fog_sensor_readings")
LATEST_TABLE   = os.environ.get("LATEST_TABLE",   "fog_sensor_latest")

dynamodb     = boto3.resource("dynamodb", region_name=REGION)
readings_tbl = dynamodb.Table(READINGS_TABLE)
latest_tbl   = dynamodb.Table(LATEST_TABLE)

# ── 7 sensor IDs (must match simulator.py) ───────────────────────────────────
SENSOR_IDS = [
    "temp-sensor-001",
    "temp-sensor-002",
    "humidity-sensor-001",
    "power-sensor-001",
    "network-sensor-001",
    "network-sensor-002",
    "airflow-sensor-001",
]

# Primary chart metric per sensor type prefix
SENSOR_CHART_METRIC = {
    "temp":     "temperature_c",
    "humidity": "humidity_pct",
    "power":    "load_pct",
    "network":  "latency_ms",
    "airflow":  "airflow_cfm",
}

CATEGORY_LABELS = {
    "temperature": "Temperature",
    "humidity":    "Humidity",
    "power":       "Power / UPS",
    "network":     "Network",
    "airflow":     "Airflow",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def from_decimal(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: from_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [from_decimal(i) for i in obj]
    return obj


def get_latest():
    try:
        items = latest_tbl.scan().get("Items", [])
        return sorted(from_decimal(items), key=lambda x: x.get("sensor_id", ""))
    except Exception as e:
        log.error("get_latest error: %s", e)
        return []


def get_history(sensor_id: str, limit: int = 20):
    try:
        resp = readings_tbl.query(
            KeyConditionExpression=Key("pk").eq(sensor_id),
            ScanIndexForward=False,
            Limit=limit,
        )
        rows = from_decimal(resp.get("Items", []))
        rows.reverse()
        return rows
    except Exception as e:
        log.error("get_history error for %s: %s", sensor_id, e)
        return []


def _chart_metric_for_sensor(sensor_id: str) -> str:
    for prefix, metric in SENSOR_CHART_METRIC.items():
        if sensor_id.startswith(prefix):
            return metric
    return "value"


def build_charts():
    charts = {}
    for sid in SENSOR_IDS:
        rows       = get_history(sid, 60)
        metric_key = _chart_metric_for_sensor(sid)
        charts[sid] = {
            "labels": [r.get("timestamp", "")[11:16] for r in rows],
            "values": [r.get("metrics", {}).get(metric_key, 0) for r in rows],
        }
    return charts


def collect_alerts(latest):
    alerts = []
    for item in latest:
        for a in item.get("alerts", []):
            alerts.append({
                "sensor_id":   item.get("sensor_id"),
                "sensor_name": item.get("sensor_name", ""),
                "rack":        item.get("rack", "?"),
                "aisle":       item.get("aisle", "?"),
                "metric":      a.get("metric"),
                "level":       a.get("level"),
                "value":       a.get("value"),
                "timestamp":   item.get("last_updated", ""),
            })
    return sorted(alerts, key=lambda x: 0 if x["level"] == "CRITICAL" else 1)


def overall_sla(latest):
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


def group_by_category(latest):
    groups = {}
    for item in latest:
        stype = item.get("sensor_type", "unknown")
        if stype not in groups:
            groups[stype] = {
                "label":        CATEGORY_LABELS.get(stype, stype.title()),
                "sensor_type":  stype,
                "sensors":      [],
                "healthy":      0,
                "total":        0,
                "worst_status": "OK",
                "alerts":       [],
            }
        g = groups[stype]
        g["sensors"].append(item)
        g["total"]  += 1
        if item.get("sla_met", False):
            g["healthy"] += 1
        status = item.get("status", "OK")
        if status == "CRITICAL" or (status == "WARNING" and g["worst_status"] != "CRITICAL"):
            g["worst_status"] = status
        for a in item.get("alerts", []):
            g["alerts"].append({**a,
                                 "sensor_id":   item.get("sensor_id"),
                                 "sensor_name": item.get("sensor_name", ""),
                                 "rack":        item.get("rack", "?"),
                                 "aisle":       item.get("aisle", "?")})
    return groups


# ── Routes ────────────────────────────────────────────────────────────────────

@application.route("/")
def index():
    latest = get_latest()
    return render_template(
        "dashboard.html",
        latest=latest,
        alerts=collect_alerts(latest),
        sla=overall_sla(latest),
        categories=group_by_category(latest),
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
        "categories": group_by_category(latest),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


@application.route("/api/category/<sensor_type>")
def api_category(sensor_type):
    latest  = get_latest()
    sensors = [s for s in latest if s.get("sensor_type") == sensor_type]
    if not sensors:
        return jsonify({"error": "Unknown category"}), 404

    racks  = sorted(set(s.get("rack",  "?") for s in sensors))
    aisles = sorted(set(s.get("aisle", "?") for s in sensors))
    grid   = {}
    for s in sensors:
        key = (s.get("rack", "?"), s.get("aisle", "?"))
        grid[str(key)] = {
            "sensor_id":    s.get("sensor_id"),
            "sensor_name":  s.get("sensor_name"),
            "rack":         s.get("rack"),
            "aisle":        s.get("aisle"),
            "status":       s.get("status"),
            "sla_health":   s.get("sla_health"),
            "metrics":      s.get("metrics", {}),
            "alerts":       s.get("alerts", []),
            "last_updated": s.get("last_updated"),
        }

    return jsonify({
        "sensor_type": sensor_type,
        "label":       CATEGORY_LABELS.get(sensor_type, sensor_type.title()),
        "racks":       racks,
        "aisles":      aisles,
        "grid":        grid,
        "sensors":     sensors,
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