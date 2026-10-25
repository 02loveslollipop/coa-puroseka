"""Authentication-only database connection; never reuse chat-state credentials."""

from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from app.config import settings


@contextmanager
def login_connection():
    with psycopg.connect(
        settings()["LOGIN_DATABASE_URL"], connect_timeout=10, row_factory=dict_row
    ) as conn:
        yield conn
