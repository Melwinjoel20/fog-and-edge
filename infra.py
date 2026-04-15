"""
infra.py – One script to deploy the entire Fog/Edge project.

UPDATED VERSION:
- No need to pass --role-arn manually
- Automatically detects LabRole from current AWS account
- Idempotent: safe to re-run if already partially deployed
- Includes ALL 6 steps: DynamoDB, FogEdgeLambda, FogSensorSimulator,
  IoT Core, EventBridge, SNS (email alerts)

Usage:
    python infra.py

Optional:
    python infra.py --region us-east-1
    python infra.py --teardown
"""

import argparse
import boto3
import io
import json
import sys
import time
import zipfile
from pathlib import Path

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
RESET  = "\033[0m"

ok   = lambda s: print(f"  {GREEN}✓{RESET} {s}")
warn = lambda s: print(f"  {YELLOW}⚠{RESET}  {s}")
err  = lambda s: print(f"  {RED}✗{RESET} {s}")
hdr  = lambda s: print(f"\n{'─'*60}\n  {s}\n{'─'*60}")

# ── Alert email ───────────────────────────────────────────────────────────────
ALERT_EMAIL = "melwinpintoir@gmail.com"


# ─────────────────────────────────────────────────────────────────────────────
# AUTO DETECT LAB ROLE
# ─────────────────────────────────────────────────────────────────────────────
def get_lab_role_arn(region):
    hdr("Detecting IAM LabRole")
    iam = boto3.client("iam", region_name=region)

    possible_roles = [
        "LabRole",
        "labrole",
        "LabRole-us-east-1",
        "LearnerLabRole",
    ]

    for role in possible_roles:
        try:
            resp = iam.get_role(RoleName=role)
            arn = resp["Role"]["Arn"]
            ok(f"Using IAM Role: {role} → {arn}")
            return arn
        except Exception:
            pass

    err("Could not auto-detect LabRole.")
    err("Please verify the role exists in IAM console.")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# ZIP HELPER
# ─────────────────────────────────────────────────────────────────────────────
def _zip_file(src_path: str, arcname: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(src_path, arcname=arcname)
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 – DynamoDB
# ─────────────────────────────────────────────────────────────────────────────
def create_dynamodb(region: str):
    hdr("Step 1 – DynamoDB Tables")
    ddb = boto3.client("dynamodb", region_name=region)

    tables = {
        "fog_sensor_readings": {
            "KeySchema": [
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            "AttributeDefinitions": [
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            "ttl_attribute": "ttl",
        },
        "fog_sensor_latest": {
            "KeySchema": [{"AttributeName": "sensor_id", "KeyType": "HASH"}],
            "AttributeDefinitions": [
                {"AttributeName": "sensor_id", "AttributeType": "S"}
            ],
        },
    }

    for name, cfg in tables.items():
        print(f"\n  Creating {name}...")
        try:
            ddb.create_table(
                TableName=name,
                KeySchema=cfg["KeySchema"],
                AttributeDefinitions=cfg["AttributeDefinitions"],
                BillingMode="PAY_PER_REQUEST",
                Tags=[{"Key": "Project", "Value": "FogEdge"}],
            )
            if "ttl_attribute" in cfg:
                ddb.get_waiter("table_exists").wait(TableName=name)
                ddb.update_time_to_live(
                    TableName=name,
                    TimeToLiveSpecification={
                        "Enabled": True,
                        "AttributeName": cfg["ttl_attribute"],
                    },
                )
            ok(f"{name} created")
        except ddb.exceptions.ResourceInUseException:
            warn(f"{name} already exists – skipping")

    print("\n  Waiting for tables to become ACTIVE...")
    for name in tables:
        ddb.get_waiter("table_exists").wait(TableName=name)
        ok(f"{name} is ACTIVE")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 – SNS Topic + Email Subscription
# ─────────────────────────────────────────────────────────────────────────────
def setup_sns(region: str) -> str:
    hdr("Step 2 – SNS Topic & Email Subscription")
    sns = boto3.client("sns", region_name=region)

    topic_name = "ColoGuardAlerts"

    # create_topic is idempotent – returns existing ARN if already exists
    resp      = sns.create_topic(
        Name=topic_name,
        Tags=[{"Key": "Project", "Value": "FogEdge"}],
    )
    topic_arn = resp["TopicArn"]
    ok(f"SNS Topic '{topic_name}' ready → {topic_arn}")

    # Subscribe the alert email (sends a confirmation email)
    sub_resp = sns.subscribe(
        TopicArn=topic_arn,
        Protocol="email",
        Endpoint=ALERT_EMAIL,
        ReturnSubscriptionArn=True,
    )
    sub_arn = sub_resp.get("SubscriptionArn", "")

    if sub_arn == "pending confirmation":
        warn(
            f"Subscription for {ALERT_EMAIL} is PENDING CONFIRMATION.\n"
            f"  ➜ Check your inbox and click the confirmation link before alerts will arrive."
        )
    else:
        ok(f"Email subscription confirmed → {sub_arn}")

    return topic_arn


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 & 4 – Lambda deploy (shared helper)
# ─────────────────────────────────────────────────────────────────────────────
def deploy_lambda(region: str, role_arn: str, name: str,
                  file_path: str, handler: str, env_vars: dict) -> str:
    hdr(f"Deploying Lambda: {name}")
    lam = boto3.client("lambda", region_name=region)
    zipped = _zip_file(file_path, Path(file_path).name)

    try:
        lam.update_function_code(FunctionName=name, ZipFile=zipped)
        waiter = lam.get_waiter("function_updated")
        waiter.wait(FunctionName=name)
        lam.update_function_configuration(
            FunctionName=name,
            Timeout=60,
            Environment={"Variables": env_vars},
        )
        warn(f"{name} already exists – updated code + config")
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(6):
            try:
                lam.create_function(
                    FunctionName=name,
                    Runtime="python3.12",
                    Role=role_arn,
                    Handler=handler,
                    Code={"ZipFile": zipped},
                    Timeout=60,
                    MemorySize=128,
                    Environment={"Variables": env_vars},
                    Tags={"Project": "FogEdge"},
                )
                break
            except lam.exceptions.InvalidParameterValueException as e:
                if "role" in str(e).lower() and attempt < 5:
                    print(f"    Role not ready, retrying in 5s... ({attempt+1}/5)")
                    time.sleep(5)
                else:
                    raise

    lam.get_waiter("function_active").wait(FunctionName=name)
    arn = lam.get_function(FunctionName=name)["Configuration"]["FunctionArn"]
    ok(f"{name} is active → {arn}")
    return arn


# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 – IoT Core: Thing + policy + rule → FogEdgeLambda
# ─────────────────────────────────────────────────────────────────────────────
def setup_iot(region: str, processor_arn: str):
    hdr("Step 5 – IoT Core (Thing + Policy + Rule)")
    iot = boto3.client("iot",    region_name=region)
    lam = boto3.client("lambda", region_name=region)

    thing_name  = "fog-node-lambda-01"
    policy_name = "FogEdgePolicy"
    rule_name   = "FogEdgeSensorRule"

    try:
        iot.create_thing(thingName=thing_name)
        ok(f"IoT Thing '{thing_name}' created")
    except iot.exceptions.ResourceAlreadyExistsException:
        warn(f"IoT Thing '{thing_name}' already exists")

    policy_doc = json.dumps({
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["iot:Connect"],
                "Resource": f"arn:aws:iot:{region}:*:client/{thing_name}",
            },
            {
                "Effect": "Allow",
                "Action": ["iot:Publish"],
                "Resource": f"arn:aws:iot:{region}:*:topic/fog/sensors/data",
            },
        ],
    })
    try:
        iot.create_policy(policyName=policy_name, policyDocument=policy_doc)
        ok(f"IoT Policy '{policy_name}' created")
    except iot.exceptions.ResourceAlreadyExistsException:
        warn(f"IoT Policy '{policy_name}' already exists")

    try:
        lam.add_permission(
            FunctionName=processor_arn.split(":")[-1],
            StatementId="iot-invoke-fog",
            Action="lambda:InvokeFunction",
            Principal="iot.amazonaws.com",
        )
        ok("IoT → FogEdgeLambda invoke permission added")
    except lam.exceptions.ResourceConflictException:
        warn("IoT invoke permission already exists on FogEdgeLambda")

    try:
        iot.create_topic_rule(
            ruleName=rule_name,
            topicRulePayload={
                "sql":         "SELECT * FROM 'fog/sensors/data'",
                "description": "Forward sensor data to FogEdgeLambda",
                "actions":     [{"lambda": {"functionArn": processor_arn}}],
                "ruleDisabled": False,
                "awsIotSqlVersion": "2016-03-23",
            },
        )
        ok(f"IoT Rule '{rule_name}' created")
    except Exception as e:
        if "already exists" in str(e).lower():
            warn(f"IoT Rule '{rule_name}' already exists")
        else:
            raise

    endpoint = iot.describe_endpoint(endpointType="iot:Data-ATS")["endpointAddress"]
    ok(f"IoT endpoint: {endpoint}")
    return endpoint


# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 – EventBridge: trigger simulator every 1 minute
# ─────────────────────────────────────────────────────────────────────────────
def setup_eventbridge(region: str, simulator_arn: str):
    hdr("Step 6 – EventBridge Schedule (every 1 min)")
    events = boto3.client("events", region_name=region)
    lam    = boto3.client("lambda", region_name=region)

    rule_name = "FogSensorTrigger"

    resp = events.put_rule(
        Name=rule_name,
        ScheduleExpression="rate(1 minute)",
        State="ENABLED",
        Description="Trigger FogSensorSimulator every minute",
    )
    rule_arn = resp["RuleArn"]
    ok(f"EventBridge rule '{rule_name}' created/updated")

    try:
        lam.add_permission(
            FunctionName=simulator_arn.split(":")[-1],
            StatementId="eventbridge-invoke-simulator",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=rule_arn,
        )
        ok("EventBridge → FogSensorSimulator invoke permission added")
    except lam.exceptions.ResourceConflictException:
        warn("EventBridge invoke permission already exists on FogSensorSimulator")

    events.put_targets(
        Rule=rule_name,
        Targets=[{"Id": "FogSimulatorTarget", "Arn": simulator_arn}],
    )
    ok("EventBridge target set → FogSensorSimulator")
    return rule_arn


# ─────────────────────────────────────────────────────────────────────────────
# TEARDOWN
# ─────────────────────────────────────────────────────────────────────────────
def teardown(region: str):
    hdr("TEARDOWN – deleting all FogEdge resources")
    lam    = boto3.client("lambda",   region_name=region)
    ddb    = boto3.client("dynamodb", region_name=region)
    iot    = boto3.client("iot",      region_name=region)
    events = boto3.client("events",   region_name=region)
    sns    = boto3.client("sns",      region_name=region)

    for fn in ["FogEdgeLambda", "FogSensorSimulator"]:
        try:
            lam.delete_function(FunctionName=fn)
            ok(f"Lambda {fn} deleted")
        except lam.exceptions.ResourceNotFoundException:
            warn(f"Lambda {fn} not found")

    for t in ["fog_sensor_readings", "fog_sensor_latest"]:
        try:
            ddb.delete_table(TableName=t)
            ok(f"DynamoDB {t} deleted")
        except ddb.exceptions.ResourceNotFoundException:
            warn(f"DynamoDB {t} not found")

    try:
        rules = events.list_targets_by_rule(Rule="FogSensorTrigger").get("Targets", [])
        if rules:
            events.remove_targets(Rule="FogSensorTrigger", Ids=[r["Id"] for r in rules])
        events.delete_rule(Name="FogSensorTrigger")
        ok("EventBridge rule 'FogSensorTrigger' deleted")
    except events.exceptions.ResourceNotFoundException:
        warn("EventBridge rule not found")

    try:
        iot.delete_topic_rule(ruleName="FogEdgeSensorRule")
        ok("IoT Rule 'FogEdgeSensorRule' deleted")
    except Exception:
        warn("IoT Rule not found")

    try:
        iot.delete_policy(policyName="FogEdgePolicy")
        ok("IoT Policy 'FogEdgePolicy' deleted")
    except Exception:
        warn("IoT Policy not found")

    try:
        iot.delete_thing(thingName="fog-node-lambda-01")
        ok("IoT Thing 'fog-node-lambda-01' deleted")
    except Exception:
        warn("IoT Thing not found")

    # Delete SNS topic and all subscriptions
    try:
        topics = sns.list_topics().get("Topics", [])
        for t in topics:
            arn = t["TopicArn"]
            if "ColoGuardAlerts" in arn:
                # Unsubscribe all
                subs = sns.list_subscriptions_by_topic(TopicArn=arn).get("Subscriptions", [])
                for s in subs:
                    if s["SubscriptionArn"] not in ("PendingConfirmation", "Deleted"):
                        sns.unsubscribe(SubscriptionArn=s["SubscriptionArn"])
                sns.delete_topic(TopicArn=arn)
                ok(f"SNS Topic '{arn}' deleted")
    except Exception as e:
        warn(f"SNS cleanup issue: {e}")

    print("\n  ✅ Teardown complete.")
    print("  ⚠  Manually delete the Elastic Beanstalk environment from the Console.")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Deploy the Fog/Edge project end-to-end (idempotent)"
    )
    parser.add_argument("--region",   default="us-east-1")
    parser.add_argument("--teardown", action="store_true", help="Destroy everything")
    args = parser.parse_args()

    if args.teardown:
        teardown(args.region)
        return

    print(f"\n{'='*60}")
    print("  🚀 Fog / Edge Project – Full Deployment (Idempotent)")
    print(f"  Region : {args.region}")
    print(f"{'='*60}")

    # Step 0 – detect role
    role_arn = get_lab_role_arn(args.region)

    # Step 1 – DynamoDB
    create_dynamodb(args.region)

    # Step 2 – SNS topic + email subscription  ← NEW
    sns_topic_arn = setup_sns(args.region)

    # Step 3 – FogEdgeLambda (IoT → DynamoDB writer + SNS alerter)
    processor_arn = deploy_lambda(
        region    = args.region,
        role_arn  = role_arn,
        name      = "FogEdgeLambda",
        file_path = "lambda_processor/lambda_function.py",
        handler   = "lambda_function.lambda_handler",
        env_vars  = {
            "READINGS_TABLE": "fog_sensor_readings",
            "LATEST_TABLE":   "fog_sensor_latest",
            "SNS_TOPIC_ARN":  sns_topic_arn,          # ← injected here
        },
    )

    # Step 4 – FogSensorSimulator (EventBridge → IoT publisher)
    simulator_arn = deploy_lambda(
        region    = args.region,
        role_arn  = role_arn,
        name      = "FogSensorSimulator",
        file_path = "lambda_simulator/simulator.py",
        handler   = "simulator.lambda_handler",
        env_vars  = {
            "AWS_IOT_REGION": args.region,
            "IOT_TOPIC":      "fog/sensors/data",
        },
    )

    # Step 5 – IoT Core
    iot_endpoint = setup_iot(args.region, processor_arn)

    # Step 6 – EventBridge
    eb_rule_arn = setup_eventbridge(args.region, simulator_arn)

    print(f"\n{'='*60}")
    print("  ✅  DEPLOYMENT COMPLETE")
    print(f"{'='*60}")
    print(f"  IoT Endpoint     : {iot_endpoint}")
    print(f"  Processor Lambda : {processor_arn}")
    print(f"  Simulator Lambda : {simulator_arn}")
    print(f"  EventBridge Rule : {eb_rule_arn}")
    print(f"  SNS Topic        : {sns_topic_arn}")
    print(f"  Alert Email      : {ALERT_EMAIL}")
    print()
    print("  ⚠  ACTION REQUIRED:")
    print(f"     Check {ALERT_EMAIL} and confirm the SNS subscription")
    print("     before alerts will be delivered to your inbox.")
    print()
    print("  ▶  Test now (don't wait for EventBridge):")
    print("     AWS Console → Lambda → FogSensorSimulator → Test")
    print("     Then check: DynamoDB → fog_sensor_latest → Explore items")
    print()
    print("  ▶  Dashboard env vars for Elastic Beanstalk:")
    print("     AWS_REGION      = us-east-1")
    print("     READINGS_TABLE  = fog_sensor_readings")
    print("     LATEST_TABLE    = fog_sensor_latest")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()