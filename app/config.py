import json
import os
from functools import lru_cache
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


@lru_cache
def settings():
    config = dict(os.environ)
    if config.get("APP_SECRET_ARN"):
        import boto3

        config.update(
            json.loads(
                boto3.client(
                    "secretsmanager", region_name=config.get("AWS_REGION", "us-east-1")
                ).get_secret_value(SecretId=config["APP_SECRET_ARN"])["SecretString"]
            )
        )
    return config


ARTIFACTS = Path(
    os.environ.get(
        "ARTIFACT_DIR", str(Path(__file__).resolve().parents[1] / "artifacts")
    )
)


def artifact(name):
    return json.loads((ARTIFACTS / name).read_text())
