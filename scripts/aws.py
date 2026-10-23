"""Small, resumable Learner Lab deployment and lifecycle commands. Never prints secrets."""

import argparse
import base64
import hashlib
import json
import pathlib
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import boto3
from botocore.exceptions import ClientError
from app.config import settings

STATE = ROOT / ".deploy/state.json"


def save(d):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(d, indent=2))
    STATE.chmod(0o600)


def client(name):
    return boto3.client(name, region_name="us-east-1")


def state():
    return json.loads(STATE.read_text())


def outputs(d):
    return {
        o["OutputKey"]: o["OutputValue"]
        for o in client("cloudformation")
        .describe_stacks(StackName=d["stack"])["Stacks"][0]
        .get("Outputs", [])
    }


def wait_stack(d, allow_rollback=False):
    cf = client("cloudformation")
    while True:
        s = cf.describe_stacks(StackName=d["stack"])["Stacks"][0]["StackStatus"]
        print("Stack:", s, flush=True)
        if s in ("CREATE_COMPLETE", "UPDATE_COMPLETE") or (
            allow_rollback and s == "UPDATE_ROLLBACK_COMPLETE"
        ):
            return
        if not s.endswith("_IN_PROGRESS"):
            events = cf.describe_stack_events(StackName=d["stack"])["StackEvents"]
            for e in events:
                if e["ResourceStatus"].endswith("FAILED"):
                    print(
                        e["LogicalResourceId"],
                        e.get("ResourceStatusReason", ""),
                        flush=True,
                    )
            raise RuntimeError("CloudFormation failed: " + s)
        time.sleep(15)


def apply(d, image="", desired_count=0):
    cf = client("cloudformation")
    params = [
        {"ParameterKey": k, "ParameterValue": v}
        for k, v in {
            "VpcId": d["vpc"],
            "SubnetIds": ",".join(d["subnets"]),
            "BrowserCidr": d["cidr"],
            "LabRoleArn": d["role"],
            "ImageUri": image,
            "DesiredCount": str(desired_count),
            "TargetGroupArn": d.get("gateway", {}).get("TargetGroupArn", ""),
            "PublicOrigin": d.get("gateway", {}).get("Url", ""),
        }.items()
    ]
    args = {
        "StackName": d["stack"],
        "TemplateBody": (ROOT / "infra/stack.yaml").read_text(),
        "Parameters": params,
        "Tags": [{"Key": "Project", "Value": "puroseka-semantic-poc"}],
    }
    try:
        cf.describe_stacks(StackName=d["stack"])
    except ClientError as e:
        if "does not exist" not in str(e):
            raise
        cf.create_stack(**args)
        return
    try:
        cf.update_stack(**args)
    except ClientError as e:
        if "No updates are to be performed" not in str(e):
            raise


def bootstrap(cidr=None):
    ec2 = client("ec2")
    identity = client("sts").get_caller_identity()
    print("Authenticated to Learner Lab in us-east-1.", flush=True)
    if STATE.exists():
        d = state()
    else:
        vpc = next(
            v["VpcId"] for v in ec2.describe_vpcs()["Vpcs"] if v.get("IsDefault")
        )
        allsubs = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc]}])[
            "Subnets"
        ]
        subnets = []
        zones = set()
        for s in sorted(allsubs, key=lambda x: x["AvailabilityZone"]):
            if s["AvailabilityZone"] not in zones:
                subnets.append(s["SubnetId"])
                zones.add(s["AvailabilityZone"])
            if len(subnets) == 2:
                break
        d = {
            "stack": "puroseka-semantic-poc",
            "vpc": vpc,
            "subnets": subnets,
            "role": f"arn:aws:iam::{identity['Account']}:role/LabRole",
            "cidr": cidr
            or urllib.request.urlopen("https://checkip.amazonaws.com")
            .read()
            .decode()
            .strip()
            + "/32",
        }
        save(d)
    if cidr:
        d["cidr"] = cidr
        save(d)
    # Validate template without provisioning first.
    client("cloudformation").validate_template(
        TemplateBody=(ROOT / "infra/stack.yaml").read_text()
    )
    apply(d, d.get("image", ""), d.get("desired_count", 1 if d.get("image") else 0))
    print(
        "Foundation provisioning requested. Run wait or deploy to continue.", flush=True
    )


def configure_secrets(d, o):
    cfg = settings()
    if not cfg.get("MISTRAL_API_KEY"):
        raise RuntimeError("Set MISTRAL_API_KEY in .env before deployment")
    sm = client("secretsmanager")
    for key, name, user in [
        ("AdminSecretArn", "ADMIN_DATABASE_URL", "postgres"),
        ("AppSecretArn", "DATABASE_URL", "puroseka_reader"),
    ]:
        secret = json.loads(sm.get_secret_value(SecretId=o[key])["SecretString"])
        password = secret["password"]
        secret[name] = (
            f"postgresql://{user}:{password}@{o['DatabaseHost']}:5432/puroseka?sslmode=require"
        )
        if key == "AppSecretArn":
            secret.update(
                {
                    "MISTRAL_API_KEY": cfg["MISTRAL_API_KEY"],
                    "MISTRAL_MODEL": cfg.get("MISTRAL_MODEL", "ministral-8b-latest"),
                }
            )
        sm.put_secret_value(SecretId=o[key], SecretString=json.dumps(secret))
    print("Database and model credentials stored in Secrets Manager.", flush=True)


def deploy(app_only=False, configure_access=False):
    d = state()
    wait_stack(d, allow_rollback=True)
    o = outputs(d)
    if configure_access:
        from scripts.provision_access import configure_access as provision_access

        provision_access(d, o)
    if not app_only:
        configure_secrets(d, o)
        version = json.loads((ROOT / "artifacts/manifest.json").read_text())["version"]
        for path in (ROOT / "artifacts").iterdir():
            if path.is_file() and path.suffix in (".json", ".gz", ".ttl"):
                client("s3").upload_file(
                    str(path), o["Bucket"], f"{version}/{path.name}"
                )
    registry = o["RepositoryUri"].split("/")[0]
    token = client("ecr").get_authorization_token()["authorizationData"][0][
        "authorizationToken"
    ]
    user, password = base64.b64decode(token).decode().split(":", 1)
    subprocess.run(
        ["docker", "login", "--username", user, "--password-stdin", registry],
        input=password.encode(),
        check=True,
        stdout=subprocess.DEVNULL,
    )
    digest = hashlib.sha256()
    for folder in ["app", "web/src", "semantic"]:
        for p in sorted((ROOT / folder).rglob("*")):
            if p.is_file() and "__pycache__" not in str(p):
                digest.update(p.read_bytes())
    for name in [
        "Dockerfile",
        "requirements.txt",
        "web/package-lock.json",
        "scripts/import_data.py",
        "scripts/setup_access.py",
        "artifacts/manifest.json",
        "artifacts/catalog.json",
        "artifacts/translations.json",
    ]:
        digest.update((ROOT / name).read_bytes())
    image = o["RepositoryUri"] + ":" + digest.hexdigest()[:12]
    subprocess.run(
        ["docker", "build", "--platform", "linux/amd64", "-t", image, "."],
        cwd=ROOT,
        check=True,
    )
    for attempt in range(3):
        try:
            subprocess.run(["docker", "push", image], check=True)
            break
        except subprocess.CalledProcessError:
            if attempt == 2:
                raise
            print("Retrying interrupted image upload...", flush=True)
            time.sleep(5 * (attempt + 1))
    apply(d, image, 1 if app_only and not configure_access else 0)
    wait_stack(d)
    d["image"] = image
    save(d)
    o = outputs(d)
    if not app_only:
        import_data(d, o)
    if configure_access:
        import_data(d, o, module="scripts.setup_access")
        from scripts.provision_access import clear_seed_password

        clear_seed_password(o)
    if not app_only or configure_access:
        apply(d, image, 1)
        wait_stack(d)
    d["desired_count"] = 1
    save(d)
    status(d)


def import_data(d, o, module="scripts.import_data"):
    label = "Access migration" if module == "scripts.setup_access" else "Data import"
    ecs = client("ecs")
    res = ecs.run_task(
        cluster=o["ClusterArn"],
        taskDefinition=o["TaskDefinition"],
        launchType="FARGATE",
        count=1,
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": d["subnets"],
                "securityGroups": [o["AppSecurityGroup"]],
                "assignPublicIp": "ENABLED",
            }
        },
        overrides={
            "cpu": "1024",
            "memory": "3072",
            "containerOverrides": [
                {
                    "name": "app",
                    "command": ["python", "-m", module],
                    "environment": [
                        {"name": "ADMIN_SECRET_ARN", "value": o["AdminSecretArn"]}
                    ],
                }
            ],
        },
    )
    if res.get("failures") or not res.get("tasks"):
        raise RuntimeError(
            "Importer task could not start: " + json.dumps(res.get("failures", []))
        )
    task = res["tasks"][0]["taskArn"]
    print(label + " task started:", task, flush=True)
    while True:
        t = ecs.describe_tasks(cluster=o["ClusterArn"], tasks=[task])["tasks"][0]
        if t["lastStatus"] == "STOPPED":
            c = t.get("containers", [{}])[0]
            if c.get("exitCode") != 0:
                raise RuntimeError(
                    label
                    + " failed: "
                    + str(c.get("reason", t.get("stoppedReason")))
                    + "; inspect CloudWatch log group "
                    + o["LogGroup"]
                )
            print(label + " completed.", flush=True)
            break
        print(label + ":", t["lastStatus"], flush=True)
        time.sleep(15)


def status(d=None):
    d = d or state()
    o = outputs(d)
    ecs = client("ecs")
    print(
        "RDS:",
        client("rds").describe_db_instances(DBInstanceIdentifier=o["DatabaseId"])[
            "DBInstances"
        ][0]["DBInstanceStatus"],
        flush=True,
    )
    if not o.get("ServiceName"):
        print("Foundation ready; application not deployed.")
        return
    svc = ecs.describe_services(cluster=o["ClusterArn"], services=[o["ServiceName"]])[
        "services"
    ][0]
    print("ECS desired/running:", svc["desiredCount"], svc["runningCount"], flush=True)
    tasks = ecs.list_tasks(cluster=o["ClusterArn"], serviceName=o["ServiceName"])[
        "taskArns"
    ]
    if tasks:
        for task in ecs.describe_tasks(cluster=o["ClusterArn"], tasks=tasks)["tasks"]:
            for a in task.get("attachments", []):
                details = {x["name"]: x["value"] for x in a.get("details", [])}
                if "networkInterfaceId" in details:
                    eni = client("ec2").describe_network_interfaces(
                        NetworkInterfaceIds=[details["networkInterfaceId"]]
                    )["NetworkInterfaces"][0]
                    ip = eni.get("Association", {}).get("PublicIp")
                    if ip:
                        print("Website: http://" + ip + ":8000", flush=True)
    print("Browser IP restriction:", d["cidr"], "Log group:", o["LogGroup"], flush=True)
    if d.get("gateway"):
        print("HTTPS chat:", d["gateway"]["Url"], flush=True)
        print("COA administration:", d["gateway"]["Url"] + "/admin/sources", flush=True)
    if svc.get("events"):
        print("Latest service event:", svc["events"][0]["message"], flush=True)


def lifecycle(action):
    d = state()
    o = outputs(d)
    if action == "teardown":
        if d.get("gateway"):
            # Detach the target group before deleting its owning gateway stack.
            detached = {**d, "gateway": {}}
            apply(detached, d["image"], 0)
            wait_stack(detached)
            cf = client("cloudformation")
            cf.delete_stack(StackName=d["gateway_stack"])
            while True:
                try:
                    current = cf.describe_stacks(StackName=d["gateway_stack"])[
                        "Stacks"
                    ][0]["StackStatus"]
                except ClientError as exc:
                    if "does not exist" in str(exc):
                        break
                    raise
                if current == "DELETE_FAILED":
                    raise RuntimeError(
                        "Gateway deletion failed; inspect its CloudFormation events"
                    )
                print("Gateway:", current, flush=True)
                time.sleep(15)
            cf.delete_stack(StackName=d["auth_stack"])
        bucket = client("s3")
        items = bucket.list_objects_v2(Bucket=o["Bucket"])
        while items.get("Contents"):
            bucket.delete_objects(
                Bucket=o["Bucket"],
                Delete={"Objects": [{"Key": x["Key"]} for x in items["Contents"]]},
            )
            items = bucket.list_objects_v2(Bucket=o["Bucket"])
        client("cloudformation").delete_stack(StackName=d["stack"])
        print(
            "Stack deletion requested; run aws cloudformation describe-stacks to track completion. The PoC database and its credentials are deleted with the stack."
        )
        return
    if action == "stop":
        apply(d, d["image"], 0)
        wait_stack(d)
        d["desired_count"] = 0
        save(d)
        db = client("rds").describe_db_instances(DBInstanceIdentifier=o["DatabaseId"])[
            "DBInstances"
        ][0]["DBInstanceStatus"]
        if db == "available":
            client("rds").stop_db_instance(DBInstanceIdentifier=o["DatabaseId"])
        print(
            "Stop requested. The HTTPS load balancer, storage and secrets remain billable; RDS automatically restarts after seven days."
        )
        return
    db = client("rds").describe_db_instances(DBInstanceIdentifier=o["DatabaseId"])[
        "DBInstances"
    ][0]["DBInstanceStatus"]
    if db == "stopped":
        client("rds").start_db_instance(DBInstanceIdentifier=o["DatabaseId"])
    while db != "available":
        print("Waiting for RDS:", db, flush=True)
        time.sleep(15)
        db = client("rds").describe_db_instances(DBInstanceIdentifier=o["DatabaseId"])[
            "DBInstances"
        ][0]["DBInstanceStatus"]
    apply(d, d["image"], 1)
    wait_stack(d)
    d["desired_count"] = 1
    save(d)
    client("ecs").update_service(
        cluster=o["ClusterArn"], service=o["ServiceName"], forceNewDeployment=True
    )
    print("Resume requested; run status for the current task address.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument(
        "action",
        choices=[
            "bootstrap",
            "wait",
            "deploy",
            "status",
            "resume",
            "stop",
            "teardown",
            "import",
            "refresh-secrets",
        ],
    )
    p.add_argument("--browser-cidr")
    p.add_argument(
        "--configure-access",
        action="store_true",
        help="Provision Cognito/HTTPS and migrate only the persistent control schema",
    )
    p.add_argument(
        "--app-only",
        action="store_true",
        help="Update only the application image; skip data import and secret updates",
    )
    a = p.parse_args()
    if a.action == "bootstrap":
        bootstrap(a.browser_cidr)
    elif a.action == "wait":
        wait_stack(state())
    elif a.action == "deploy":
        deploy(app_only=a.app_only, configure_access=a.configure_access)
    elif a.action == "status":
        status()
    elif a.action == "import":
        import_data(state(), outputs(state()))
    elif a.action == "refresh-secrets":
        configure_secrets(state(), outputs(state()))
    else:
        lifecycle(a.action)
