"""Users and access tokens (opt-in: JARVIS_AUTH_ENABLED=true; needs the database).

    python -m jarvis.users add alice      # prints alice's token ONCE
    python -m jarvis.users list
    python -m jarvis.users remove alice

With auth off, every request runs as the single user "local", which is also the owner the
tasks MCP server uses for other clients (e.g. Claude Desktop), so nothing changes for a
one-person setup.

Tokens are 256-bit random strings. Only their SHA-256 is stored: a leaked database does
not leak usable tokens, and a plain (unsalted) hash is enough because the input is random,
not a guessable password. Lookup is by hash, so there is no string comparison to time.
"""
import argparse
import hashlib
import re
import secrets
import sys

import psycopg

LOCAL = "local"
USER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")  # also safe as a folder name

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id           text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9_-]{0,31}$'),
    token_sha256 text UNIQUE NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
"""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def ensure_schema(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(SCHEMA)


def add_user(dsn: str, user_id: str) -> str:
    """Create a user and return their new token (shown once, never stored)."""
    if not USER_ID.match(user_id) or user_id == LOCAL:
        raise ValueError("user id: 1-32 of a-z 0-9 _ -, starting with a letter or digit; not 'local'")
    token = "jv_" + secrets.token_urlsafe(32)
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(SCHEMA)
        c.execute("INSERT INTO users (id, token_sha256) VALUES (%s, %s)", (user_id, _hash(token)))
    return token


def remove_user(dsn: str, user_id: str) -> bool:
    with psycopg.connect(dsn, autocommit=True) as c:
        return c.execute("DELETE FROM users WHERE id = %s", (user_id,)).rowcount > 0


def list_users(dsn: str) -> list[str]:
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(SCHEMA)
        return [r[0] for r in c.execute("SELECT id FROM users ORDER BY id")]


def authenticate(dsn: str, token: str | None) -> str | None:
    """User id for a token, or None."""
    if not token or len(token) > 200:
        return None
    with psycopg.connect(dsn, autocommit=True) as c:
        row = c.execute("SELECT id FROM users WHERE token_sha256 = %s", (_hash(token),)).fetchone()
    return row[0] if row else None


def main(argv=None) -> int:
    from .config import get_settings

    ap = argparse.ArgumentParser(description="Manage Jarvis users")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("add").add_argument("user")
    sub.add_parser("remove").add_argument("user")
    sub.add_parser("list")
    a = ap.parse_args(argv)
    dsn = get_settings().database_url
    if not dsn:
        print("JARVIS_DATABASE_URL is not set: users need the database", file=sys.stderr)
        return 2
    if a.cmd == "add":
        try:
            token = add_user(dsn, a.user)
        except (ValueError, psycopg.errors.UniqueViolation) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"Created user {a.user!r}. Token (shown once, store it safely):\n{token}")
    elif a.cmd == "remove":
        print("removed" if remove_user(dsn, a.user) else "no such user")
    else:
        print("\n".join(list_users(dsn)) or "(no users)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
