"""Cognito sign-in with opaque, revocable, server-side browser sessions."""

import base64
import hashlib
import hmac
import secrets
from datetime import timedelta

import boto3
from botocore.exceptions import ClientError
from fastapi import HTTPException, Request

from app.config import settings
from app.demo import DEMO_USERNAME
from app.passwords import (
    password_hash as password_hash,
    password_matches as password_matches,
    password_needs_rehash,
)
from app.auth_database import login_connection
from app.control import utcnow

COOKIE = "coa_session"
SESSION_SECONDS = 3600


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()


def check_origin(request: Request):
    expected = settings().get("PUBLIC_ORIGIN", str(request.base_url).rstrip("/"))
    if request.headers.get("origin") != expected:
        raise HTTPException(403, "This request must originate from this application.")


def login(username, password, ip):
    username = username.strip().lower()
    attempt_key = fingerprint(ip)
    # Durable attempt limit also covers nonexistent accounts and Cognito outages.
    with login_connection() as conn:
        row = conn.execute(
            "INSERT INTO control.login_attempts(key,attempts,resets_at) VALUES (%s,1,%s) "
            "ON CONFLICT (key) DO UPDATE SET "
            "attempts=CASE WHEN control.login_attempts.resets_at < now() THEN 1 ELSE control.login_attempts.attempts+1 END,"
            "resets_at=CASE WHEN control.login_attempts.resets_at < now() THEN EXCLUDED.resets_at ELSE control.login_attempts.resets_at END "
            "RETURNING attempts",
            (attempt_key, utcnow() + timedelta(minutes=15)),
        ).fetchone()
    if row["attempts"] > 12:
        raise HTTPException(
            429,
            "Too many sign-in attempts. Try again in 15 minutes.",
            headers={"Retry-After": "900"},
        )
    cfg = settings()
    with login_connection() as conn:
        user = conn.execute(
            "SELECT username,role,password_hash,enabled FROM control.accounts WHERE username=%s",
            (username,),
        ).fetchone()
    valid = False
    local_auth = (
        cfg.get("AUTH_PROVIDER", "local") != "cognito" or username == DEMO_USERNAME
    )
    encoded = None
    if not local_auth:
        client_id, client_secret = (
            cfg["COGNITO_CLIENT_ID"],
            cfg["COGNITO_CLIENT_SECRET"],
        )
        secret_hash = base64.b64encode(
            hmac.new(
                client_secret.encode(), (username + client_id).encode(), hashlib.sha256
            ).digest()
        ).decode()
        cognito = boto3.client(
            "cognito-idp", region_name=cfg.get("AWS_REGION", "us-east-1")
        )
        try:
            result = cognito.initiate_auth(
                ClientId=client_id,
                AuthFlow="USER_PASSWORD_AUTH",
                AuthParameters={
                    "USERNAME": username,
                    "PASSWORD": password,
                    "SECRET_HASH": secret_hash,
                },
            )
            if result.get("AuthenticationResult"):
                identity = cognito.get_user(
                    AccessToken=result["AuthenticationResult"]["AccessToken"]
                )
                valid = identity["Username"].casefold() == username
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in {
                "NotAuthorizedException",
                "UserNotFoundException",
                "PasswordResetRequiredException",
                "UserNotConfirmedException",
                "InvalidParameterException",
            }:
                raise HTTPException(
                    503, "Sign-in is temporarily unavailable. Try again."
                ) from exc
    else:
        # Unknown accounts also perform a password verification.
        encoded = user["password_hash"] if user else None
        valid = password_matches(password, encoded)
    if not valid or not user or not user["enabled"]:
        raise HTTPException(401, "The email or password is incorrect.")
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with login_connection() as conn:
        current = conn.execute(
            "SELECT username,role,password_hash,enabled FROM control.accounts WHERE username=%s FOR UPDATE",
            (username,),
        ).fetchone()
        if (
            not current
            or not current["enabled"]
            or (
                local_auth
                and current["password_hash"] != encoded
                and not password_matches(password, current["password_hash"])
            )
        ):
            raise HTTPException(401, "The email or password is incorrect.")
        if local_auth and password_needs_rehash(current["password_hash"]):
            conn.execute(
                "UPDATE control.accounts SET password_hash=%s WHERE username=%s",
                (password_hash(password), username),
            )
        user = current
        conn.execute("DELETE FROM control.sessions WHERE expires_at < now()")
        conn.execute(
            "INSERT INTO control.sessions(token_hash,username,csrf,expires_at) VALUES (%s,%s,%s,%s)",
            (
                fingerprint(token),
                username,
                csrf,
                utcnow() + timedelta(seconds=SESSION_SECONDS),
            ),
        )
    return token, {"username": username, "role": user["role"], "csrf": csrf}


def current_user(request: Request):
    token = request.cookies.get(COOKIE, "")
    if not token:
        raise HTTPException(401, "Sign in to continue.")
    with login_connection() as conn:
        row = conn.execute(
            "SELECT a.username,a.role,s.csrf FROM control.sessions s JOIN control.accounts a "
            "ON a.username=s.username WHERE s.token_hash=%s AND s.expires_at > now() AND a.enabled",
            (fingerprint(token),),
        ).fetchone()
    if not row:
        raise HTTPException(401, "Your session has expired. Sign in again.")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_origin(request)
        if not hmac.compare_digest(
            request.headers.get("x-csrf-token", ""), row["csrf"]
        ):
            raise HTTPException(
                403, "The session security token is missing or invalid."
            )
    return row


def admin_user(request: Request):
    user = current_user(request)
    if user["role"] != "admin":
        raise HTTPException(
            403, "COA administration requires an administrator account."
        )
    return user


def logout(request: Request):
    with login_connection() as conn:
        conn.execute(
            "DELETE FROM control.sessions WHERE token_hash=%s",
            (fingerprint(request.cookies.get(COOKIE, "")),),
        )
