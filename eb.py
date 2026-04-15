"""
eb.py

FINAL Elastic Beanstalk Auto Deploy Script

Features:
✓ Creates bucket if missing
✓ Uploads dashboard.zip
✓ Creates application if missing
✓ Auto version label using timestamp
✓ Waits until version processed
✓ Checks if environment exists
✓ Updates existing environment
✓ Creates new environment if missing
✓ Waits until Ready
✓ Prints dashboard URL

Run:
python eb.py
"""

import boto3
import time
import sys
from botocore.exceptions import ClientError

# =====================================================
# CONFIG
# =====================================================
REGION = "us-east-1"

APP_NAME = "FogEdgeDashboard"
ENV_NAME = "FogEdgeDashboard-env"

ZIP_FILE = "dashboard.zip"

SERVICE_ROLE = "LabRole"
INSTANCE_PROFILE = "LabInstanceProfile"

VERSION_LABEL = "v" + str(int(time.time()))

ENV_VARS = {
    "AWS_REGION": "us-east-1",
    "READINGS_TABLE": "fog_sensor_readings",
    "LATEST_TABLE": "fog_sensor_latest"
}

# =====================================================
# CLIENTS
# =====================================================
eb = boto3.client("elasticbeanstalk", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
sts = boto3.client("sts")

# =====================================================
# HELPERS
# =====================================================
def ok(msg):
    print("✓", msg)

def warn(msg):
    print("⚠", msg)

def fail(msg):
    print("✗", msg)
    sys.exit(1)

# =====================================================
# BUCKET
# =====================================================
def get_bucket():
    account = sts.get_caller_identity()["Account"]
    return f"fogedge-eb-{account}"

def ensure_bucket():
    bucket = get_bucket()

    try:
        s3.head_bucket(Bucket=bucket)
        warn(f"Bucket exists: {bucket}")
    except:
        s3.create_bucket(Bucket=bucket)
        ok(f"Bucket created: {bucket}")

    return bucket

# =====================================================
# UPLOAD ZIP
# =====================================================
def upload_zip(bucket):
    try:
        s3.upload_file(ZIP_FILE, bucket, ZIP_FILE)
        ok("dashboard.zip uploaded")
    except Exception as e:
        fail(str(e))

# =====================================================
# APPLICATION
# =====================================================
# Replace ONLY this function in your eb.py

def ensure_application():
    try:
        apps = eb.describe_applications(
            ApplicationNames=[APP_NAME]
        )["Applications"]

        if apps:
            warn("Application already exists")
            return

        eb.create_application(
            ApplicationName=APP_NAME
        )

        ok("Application created")

    except Exception as e:
        fail(str(e))

# =====================================================
# VERSION
# =====================================================
def create_version(bucket):
    try:
        eb.create_application_version(
            ApplicationName=APP_NAME,
            VersionLabel=VERSION_LABEL,
            SourceBundle={
                "S3Bucket": bucket,
                "S3Key": ZIP_FILE
            },
            Process=True
        )
        ok(f"Version created: {VERSION_LABEL}")

    except Exception as e:
        fail(str(e))

def wait_version():
    print("\nWaiting for version processing...\n")

    while True:
        resp = eb.describe_application_versions(
            ApplicationName=APP_NAME,
            VersionLabels=[VERSION_LABEL]
        )

        status = resp["ApplicationVersions"][0]["Status"]

        print("Version:", status)

        if status == "PROCESSED":
            ok("Version ready")
            return

        if status == "FAILED":
            fail("Version processing failed")

        time.sleep(10)

# =====================================================
# ENVIRONMENT
# =====================================================
def env_exists():
    resp = eb.describe_environments(
        ApplicationName=APP_NAME,
        EnvironmentNames=[ENV_NAME],
        IncludeDeleted=False
    )

    return len(resp["Environments"]) > 0

def option_settings():
    opts = []

    for k, v in ENV_VARS.items():
        opts.append({
            "Namespace": "aws:elasticbeanstalk:application:environment",
            "OptionName": k,
            "Value": v
        })

    opts.append({
        "Namespace": "aws:autoscaling:launchconfiguration",
        "OptionName": "IamInstanceProfile",
        "Value": INSTANCE_PROFILE
    })

    opts.append({
        "Namespace": "aws:elasticbeanstalk:environment",
        "OptionName": "ServiceRole",
        "Value": SERVICE_ROLE
    })

    return opts

# Replace ONLY create_environment() in eb.py

def create_environment():
    # Auto fetch latest Python platform
    platforms = eb.list_available_solution_stacks()["SolutionStacks"]

    python_stack = None

    for stack in reversed(platforms):
        if "Python 3.11" in stack:
            python_stack = stack
            break

    if not python_stack:
        fail("No Python 3.11 platform found in this region")

    print("Using Platform:", python_stack)

    eb.create_environment(
        ApplicationName=APP_NAME,
        EnvironmentName=ENV_NAME,
        VersionLabel=VERSION_LABEL,
        SolutionStackName=python_stack,
        OptionSettings=option_settings()
    )

    ok("Environment creation started")

def update_environment():
    eb.update_environment(
        EnvironmentName=ENV_NAME,
        VersionLabel=VERSION_LABEL,
        OptionSettings=option_settings()
    )

    ok("Existing environment update started")

# =====================================================
# WAIT READY
# =====================================================
def wait_ready():
    print("\nWaiting for environment...\n")

    while True:
        resp = eb.describe_environments(
            ApplicationName=APP_NAME,
            EnvironmentNames=[ENV_NAME]
        )

        env = resp["Environments"][0]

        status = env["Status"]
        health = env.get("Health", "Unknown")

        print("Status:", status, "| Health:", health)

        if status == "Ready":
            cname = env["CNAME"]
            print("\n🌐 Dashboard URL:")
            print("http://" + cname)
            return

        time.sleep(20)

# =====================================================
# MAIN
# =====================================================
def main():
    print("\n🚀 FogEdge Dashboard Deployment\n")

    bucket = ensure_bucket()

    upload_zip(bucket)

    ensure_application()

    create_version(bucket)

    wait_version()

    if env_exists():
        warn("Environment exists")
        update_environment()
    else:
        create_environment()

    wait_ready()

if __name__ == "__main__":
    main()