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
from app.control import account, control_connection, utcnow

COOKIE = "coa_session"
SESSION_SECONDS = 3600


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1)
    return salt + ":" + digest.hex()


def password_matches(password, encoded):
    try:
        salt, _ = encoded.split(":", 1)
        return hmac.compare_digest(password_hash(password, salt), encoded)
    except (ValueError, AttributeError):
        return False


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
    with control_connection() as conn:
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
    user = account(username)
    valid = False
    if cfg.get("AUTH_PROVIDER", "local") == "cognito":
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
        # Same work for unknown accounts to avoid a fast username-existence oracle.
        encoded = (
            user["password_hash"] if user else password_hash(secrets.token_urlsafe(32))
        )
        valid = password_matches(password, encoded)
    if not valid or not user or not user["enabled"]:
        raise HTTPException(401, "The email or password is incorrect.")
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with control_connection() as conn:
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
    with control_connection() as conn:
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
    with control_connection() as conn:
        conn.execute(
            "DELETE FROM control.sessions WHERE token_hash=%s",
            (fingerprint(request.cookies.get(COOKIE, "")),),
        )
