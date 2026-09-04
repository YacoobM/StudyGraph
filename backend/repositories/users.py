import hashlib
import hmac
import secrets
import time
import sqlite3

from fastapi import Header, HTTPException

from repositories.database import connect


PBKDF2_ITERATIONS = 210_000
SESSION_TTL_SECONDS = 60 * 60 * 24 * 14


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
	salt = salt or secrets.token_hex(16)
	digest = hashlib.pbkdf2_hmac(
		"sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS
	)
	return digest.hex(), salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
	candidate, _ = hash_password(password, salt)
	return hmac.compare_digest(candidate, stored_hash)


def user_public(row: sqlite3.Row) -> dict:
	return {
		"id": row["id"],
		"email": row["email"],
		"name": row["name"],
		"created_at": row["created_at"],
	}


def create_session(user_id: int) -> str:
	token = secrets.token_urlsafe(32)
	now = time.time()
	with connect() as conn:
		conn.execute(
			"INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
			(token, user_id, now, now + SESSION_TTL_SECONDS),
		)
	return token


def current_user(authorization: str = Header(default="")) -> sqlite3.Row:
	if not authorization.startswith("Bearer "):
		raise HTTPException(status_code=401, detail="Sign in to continue.")

	token = authorization[7:].strip()
	with connect() as conn:
		row = conn.execute(
			"""
			SELECT u.*, s.expires_at FROM sessions s
			JOIN users u ON u.id = s.user_id
			WHERE s.token = ?
			""",
			(token,),
		).fetchone()

		if row is None:
			raise HTTPException(status_code=401, detail="Sign in to continue.")

		if row["expires_at"] < time.time():
			conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
			raise HTTPException(status_code=401, detail="Your session expired. Sign in again.")

	return row
