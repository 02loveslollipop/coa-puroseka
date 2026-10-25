"""Provision authentication and HTTPS with CloudFormation; keep secrets server-side."""

import json
import secrets
from urllib.parse import quote

from botocore.exceptions import ClientError

from app.config import settings
from psycopg.conninfo import conninfo_to_dict


def runtime_databases(app, host):
    """Reuse separate service passwords across deployments; keep values private."""
    for key, role in (
        ("LOGIN_DATABASE_URL", "puroseka_login"),
        ("CHAT_DATABASE_URL", "puroseka_chat"),
    ):
        previous = conninfo_to_dict(app[key]) if app.get(key) else {}
        if previous and previous.get("user") != role:
            raise ValueError("Unexpected service database role")
        password = previous.get("password") or secrets.token_urlsafe(48)
        app[key] = (
            f"postgresql://{role}:{quote(password, safe='')}@{host}:5432/puroseka?sslmode=require"
        )
    return app


def configure_access(d, o):
    from scripts.aws import ROOT, client, save, wait_stack

    cfg = settings()
    username = (
        (cfg.get("ADMIN_USERNAME") or cfg.get("CHAT_USERNAME", "")).strip().lower()
    )
    password = cfg.get("ADMIN_PASSWORD") or cfg.get("CHAT_PASSWORD")
    if not username or not password:
        raise RuntimeError("Set the shared account credentials in .env first")
    cf = client("cloudformation")
    gateway_parameters = [
        {"ParameterKey": "VpcId", "ParameterValue": d["vpc"]},
        {"ParameterKey": "SubnetIds", "ParameterValue": ",".join(d["subnets"])},
        {"ParameterKey": "AppSecurityGroupId", "ParameterValue": o["AppSecurityGroup"]},
    ]
    stack_outputs = {}
    for suffix, filename, params in [
        ("auth", "auth.yaml", []),
        ("gateway", "gateway.yaml", gateway_parameters),
    ]:
        name = d["stack"] + "-" + suffix
        body = (ROOT / "infra" / filename).read_text()
        cf.validate_template(TemplateBody=body)
        try:
            cf.describe_stacks(StackName=name)
        except ClientError as exc:
            if "does not exist" not in str(exc):
                raise
            cf.create_stack(
                StackName=name,
                TemplateBody=body,
                Parameters=params,
                Tags=[{"Key": "Project", "Value": d["stack"]}],
            )
        else:
            status = cf.describe_stacks(StackName=name)["Stacks"][0]["StackStatus"]
            if not status.endswith("_IN_PROGRESS"):
                try:
                    cf.update_stack(
                        StackName=name, TemplateBody=body, Parameters=params
                    )
                except ClientError as exc:
                    if "No updates are to be performed" not in str(exc):
                        raise
        wait_stack({"stack": name})
        stack_outputs[suffix] = {
            x["OutputKey"]: x["OutputValue"]
            for x in cf.describe_stacks(StackName=name)["Stacks"][0]["Outputs"]
        }
    identity = stack_outputs["auth"]
    cognito = client("cognito-idp")
    pool, app_client = identity["UserPoolId"], identity["ClientId"]
    try:
        cognito.admin_get_user(UserPoolId=pool, Username=username)
    except cognito.exceptions.UserNotFoundException:
        cognito.admin_create_user(
            UserPoolId=pool, Username=username, MessageAction="SUPPRESS"
        )
    cognito.admin_set_user_password(
        UserPoolId=pool, Username=username, Password=password, Permanent=True
    )
    client_secret = cognito.describe_user_pool_client(
        UserPoolId=pool, ClientId=app_client
    )["UserPoolClient"]["ClientSecret"]
    sm = client("secretsmanager")
    app = json.loads(sm.get_secret_value(SecretId=o["AppSecretArn"])["SecretString"])
    runtime_databases(app, o["DatabaseHost"])
    app.update(
        {
            "AUTH_PROVIDER": "cognito",
            "COGNITO_USER_POOL_ID": pool,
            "COGNITO_CLIENT_ID": app_client,
            "COGNITO_CLIENT_SECRET": client_secret,
        }
    )
    sm.put_secret_value(SecretId=o["AppSecretArn"], SecretString=json.dumps(app))
    admin = json.loads(
        sm.get_secret_value(SecretId=o["AdminSecretArn"])["SecretString"]
    )
    admin.update({"ADMIN_USERNAME": username, "ADMIN_PASSWORD": password})
    sm.put_secret_value(SecretId=o["AdminSecretArn"], SecretString=json.dumps(admin))
    d["gateway"] = stack_outputs["gateway"]
    d["auth_stack"] = d["stack"] + "-auth"
    d["gateway_stack"] = d["stack"] + "-gateway"
    save(d)
    print(
        "Cognito account and HTTPS configuration ready. Password remains server-side.",
        flush=True,
    )


def clear_seed_password(o):
    from scripts.aws import client

    sm = client("secretsmanager")
    secret = json.loads(
        sm.get_secret_value(SecretId=o["AdminSecretArn"])["SecretString"]
    )
    secret.pop("ADMIN_PASSWORD", None)
    sm.put_secret_value(SecretId=o["AdminSecretArn"], SecretString=json.dumps(secret))
    app = json.loads(sm.get_secret_value(SecretId=o["AppSecretArn"])["SecretString"])
    app.pop("CONTROL_DATABASE_URL", None)
    app.pop("control_password", None)
    sm.put_secret_value(SecretId=o["AppSecretArn"], SecretString=json.dumps(app))
