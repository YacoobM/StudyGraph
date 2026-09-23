"""User authentication and session management."""

import logging
import secrets
import time
import sqlite3
from typing import TypedDict
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import json

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHash
from email_validator import validate_email, EmailNotValidError
from fastapi import Header, HTTPException

from app.config import SUPABASE_PUBLISHABLE_KEY, SUPABASE_URL
from app.logging_config import get_logger
from repositories.database import connect


logger = get_logger(__name__)

SESSION_TTL_SECONDS = 60 * 60 * 24 * 14

# Argon2 hasher (singleton)
_PASSWORD_HASHER = PasswordHasher()


# === Type Definitions ===

class SupabaseUser(TypedDict, total=False):
    """Supabase user object returned from /auth/v1/user."""
    id: str
    email: str
    user_metadata: dict


# === Password Management ===

def hash_password(password: str) -> str:
    """
    Hash a password using Argon2.
    
    Args:
        password: The plaintext password to hash.
    
    Returns:
        A hashed password string (includes salt embedded).
    """
    return _PASSWORD_HASHER.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    """
    Verify a plaintext password against a stored Argon2 hash.
    
    Args:
        password: The plaintext password to verify.
        stored_hash: The stored Argon2 hash (includes salt).
    
    Returns:
        True if the password matches, False otherwise.
    """
    try:
        _PASSWORD_HASHER.verify(stored_hash, password)
        return True
    except (VerifyMismatchError, InvalidHash):
        return False


# === User Representation ===

def user_public(row: sqlite3.Row) -> dict:
    """Convert a user database row to a public-safe dict."""
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "created_at": row["created_at"],
    }


# === Session Management ===

def create_session(user_id: int) -> str:
    """
    Create a new session token for a user.
    
    Args:
        user_id: The user's database ID.
    
    Returns:
        A random session token (valid for SESSION_TTL_SECONDS).
    """
    token = secrets.token_urlsafe(32)
    now = time.time()
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            (token, user_id, now, now + SESSION_TTL_SECONDS),
        )
    return token


# === Supabase Integration ===

def _supabase_user(token: str) -> SupabaseUser:
    """
    Fetch the authenticated user from Supabase using a Bearer token.
    
    Args:
        token: The Supabase auth token (JWT).
    
    Returns:
        A SupabaseUser dict with id, email, and metadata.
    
    Raises:
        HTTPException(401): If the token is invalid or Supabase request fails.
        HTTPException(500): If Supabase is not configured.
    """
    if not SUPABASE_URL or not SUPABASE_PUBLISHABLE_KEY:
        logger.error("supabase_not_configured")
        raise HTTPException(
            status_code=500,
            detail="Authentication service unavailable.",
        )

    request = Request(
        f"{SUPABASE_URL}/auth/v1/user",
        headers={
            "Authorization": f"Bearer {token}",
            "apikey": SUPABASE_PUBLISHABLE_KEY,
        },
    )
    
    try:
        with urlopen(request, timeout=10) as response:
            data = json.loads(response.read())
            return data
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        logger.warning(
            "supabase_request_failed error_type=%s",
            type(e).__name__,
        )
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    except Exception as e:
        logger.exception("supabase_unexpected_error")
        raise HTTPException(status_code=401, detail="Sign in to continue.")


# === User Lookup & Creation ===

def current_user(authorization: str = Header(default="")) -> sqlite3.Row:
    """
    Authenticate a user via Bearer token and return their database record.
    Creates a new user if they don't exist (JIT provisioning).
    
    Args:
        authorization: The Authorization header (e.g., "Bearer <token>").
    
    Returns:
        A sqlite3.Row representing the authenticated user.
    
    Raises:
        HTTPException(401): If the token is invalid or user cannot be authenticated.
    """
    # Validate Bearer token format
    if not authorization.startswith("Bearer "):
        logger.warning("current_user_missing_bearer_token")
        raise HTTPException(status_code=401, detail="Sign in to continue.")

    token = authorization[7:].strip()
    
    # Fetch user from Supabase
    supabase_user = _supabase_user(token)
    auth_user_id = supabase_user.get("id")
    email_raw = (supabase_user.get("email") or "").strip().lower()
    
    # Validate required fields
    if not auth_user_id or not email_raw:
        logger.warning(
            "supabase_incomplete_user auth_user_id=%s has_email=%s",
            bool(auth_user_id),
            bool(email_raw),
        )
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    
    # Validate email format
    try:
        email = validate_email(email_raw, check_deliverability=False).normalized
    except EmailNotValidError as e:
        logger.warning("invalid_email_from_supabase error=%s", e)
        raise HTTPException(status_code=401, detail="Sign in to continue.")

    # Upsert user: insert if not exists, or update if exists
    with connect() as conn:
        name = (
            (supabase_user.get("user_metadata") or {}).get("full_name")
            or email.split("@", 1)[0]
        )
        password_hash = hash_password(secrets.token_urlsafe(32))

        conn.execute(
			"""
			INSERT INTO users
				(auth_user_id, email, name, password_hash, salt, created_at)
			VALUES (?, ?, ?, ?, ?, ?)
			ON CONFLICT(email) DO UPDATE SET
				auth_user_id = COALESCE(users.auth_user_id, excluded.auth_user_id),
				name = COALESCE(users.name, excluded.name)
			""",
			(
				auth_user_id,
				email,
				name,
				password_hash,
				"",
				time.time(),
			),
		)

        # Fetch the final user record
        row = conn.execute(
            "SELECT * FROM users WHERE auth_user_id = ?",
            (auth_user_id,),
        ).fetchone()

    if row is None:
        logger.error("current_user_missing_after_upsert auth_user_id=%s", auth_user_id)
        raise HTTPException(status_code=401, detail="Unable to authenticate.")

    logger.debug("current_user_authenticated user_id=%s", row["id"])
    return row