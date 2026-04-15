# Fog / Edge Computing – AWS Learner Lab (Serverless Edition)

## Architecture (no EC2!)

```
EventBridge (every 1 min)
        │
        ▼
FogSensorSimulator (Lambda)
  – generates fake sensor data
  – publishes to IoT Core topic: fog/sensors/data
        │
        ▼  (IoT Rule)
FogEdgeLambda (Lambda)
  – computes SLA health score
  – writes to DynamoDB
        │
        ▼
DynamoDB
  fog_sensor_readings  (full history, 7-day TTL)
  fog_sensor_latest    (one row per sensor)
        │
        ▼
Elastic Beanstalk (Flask dashboard)
  – live charts + alerts + SLA score
  – auto-refreshes every 30 seconds
```

---

## Project Structure

```
fog-edge-project/
├── infra.py                          ← Run this ONE script to deploy everything
├── lambda_processor/
│   └── lambda_function.py            ← IoT → DynamoDB writer
├── lambda_simulator/
│   └── simulator.py                  ← Fake sensor data publisher
└── dashboard/
    ├── application.py                ← Flask web app
    ├── requirements.txt
    ├── Procfile
    └── templates/
        └── dashboard.html
```

---

## Step-by-Step Deployment

### Prerequisites (on your local machine)

```bash
pip install boto3 awscli
```

Start your AWS Learner Lab, then click **AWS Details** and configure:
```bash
aws configure
# Paste: Access Key ID, Secret Access Key, Session Token
# Region: us-east-1
# Output: json
```

---

### Step 1 – Run infra.py (deploys EVERYTHING)

Find your LabRole ARN:
> AWS Console → IAM → Roles → search "LabRole" → click it → copy the ARN

```bash
python infra.py --role-arn arn:aws:iam::YOUR_ACCOUNT_ID:role/LabRole
```

This one command:
- ✅ Creates both DynamoDB tables
- ✅ Deploys FogEdgeLambda (IoT → DynamoDB)
- ✅ Deploys FogSensorSimulator (EventBridge → IoT)
- ✅ Sets up IoT Core Thing, policy, and rule
- ✅ Creates EventBridge schedule (every 1 minute)

**Test it immediately** (don't wait for the 1-min schedule):
> AWS Console → Lambda → FogSensorSimulator → Test → click Test

Then check:
> DynamoDB → Tables → fog_sensor_latest → Explore table items
> You should see 3 rows (sensor-001, sensor-002, sensor-003)

---

### Step 2 – Deploy the Dashboard to Elastic Beanstalk

**Package the dashboard:**
```bash
cd dashboard/
zip -r ../dashboard.zip . -x "*.pyc" -x "__pycache__/*"
cd ..
```

**Deploy in AWS Console:**
1. Go to **Elastic Beanstalk → Create application**
2. Application name: `FogEdgeDashboard`
3. Platform: **Python 3.11**
4. Application code: Upload your code → upload `dashboard.zip`
5. Service role: `LabRole`  (or `aws-elasticbeanstalk-service-role`)
6. Instance profile: `LabInstanceProfile`
7. Click **Create environment** (takes ~5 minutes)

**Add environment variables:**
> EB → Your environment → Configuration → Updates, monitoring, and logging → Environment properties

Add:
```
AWS_REGION      = us-east-1
READINGS_TABLE  = fog_sensor_readings
LATEST_TABLE    = fog_sensor_latest
```

**Open your dashboard:**
> Click the environment URL shown in Elastic Beanstalk

---

### Teardown (when done)

```bash
python infra.py --teardown
```

This deletes: both Lambdas, both DynamoDB tables, EventBridge rule.
(Manually delete the Elastic Beanstalk environment from the Console.)

---

## Troubleshooting

| Error | Fix |
|-------|-----|
| `InvalidParameterValueException: role` | Wait 10s for IAM role to propagate, re-run infra.py |
| Lambda shows "No sensor data" in DynamoDB | Go to Lambda → FogSensorSimulator → Test and run it manually |
| Dashboard shows blank page | Check EB Logs → confirm env vars are set |
| `AccessDeniedException` in Lambda logs | Add `AmazonDynamoDBFullAccess` to LabRole in IAM |
| `AccessDeniedException` on IoT publish | Add `AWSIoTDataAccess` to LabRole in IAM |
| Learner Lab session expired | Restart lab, re-run `aws configure` with new keys |

---

## How the SLA Score Works

```
latency_score = max(0, 100 - max(0, latency_ms - 100))   # full marks under 100ms
status_score  = 100 (OK) | 60 (WARNING) | 20 (CRITICAL)
health_score  = (latency_score × 0.5) + (status_score × 0.5)
sla_met       = health_score >= 75
```

Alert thresholds:
| Metric      | WARNING | CRITICAL |
|-------------|---------|----------|
| temperature | 35°C    | 45°C     |
| humidity    | 75%     | 90%      |
| cpu_load    | 70%     | 90%      |
| latency_ms  | 150ms   | 300ms    |
