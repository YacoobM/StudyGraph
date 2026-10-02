import hashlib
import hmac
import json
import secrets
import time
import sqlite3
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import Depends, Header, HTTPException

from app.config import SUPABASE_PUBLISHABLE_KEY, SUPABASE_URL
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


def _supabase_user(token: str) -> dict:
	if not SUPABASE_URL or not SUPABASE_PUBLISHABLE_KEY:
		raise HTTPException(status_code=500, detail="Supabase auth is not configured.")

	request = Request(
		f"{SUPABASE_URL}/auth/v1/user",
		headers={
			"Authorization": f"Bearer {token}",
			"apikey": SUPABASE_PUBLISHABLE_KEY,
		},
	)
	try:
		with urlopen(request, timeout=10) as response:
			return json.loads(response.read())
	except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
		raise HTTPException(status_code=401, detail="Sign in to continue.")


@dataclass(frozen=True)
class SupabaseIdentity:
	auth_user_id: str
	email: str
	access_token: str


def current_supabase_identity(authorization: str = Header(default="")) -> SupabaseIdentity:
	if not authorization.startswith("Bearer "):
		raise HTTPException(status_code=401, detail="Sign in to continue.")

	token = authorization[7:].strip()
	supabase_user = _supabase_user(token)
	auth_user_id = supabase_user.get("id")
	email = (supabase_user.get("email") or "").strip().lower()
	if not auth_user_id or not email:
		raise HTTPException(status_code=401, detail="Supabase returned an incomplete user.")

	return SupabaseIdentity(
		auth_user_id=auth_user_id,
		email=email,
		access_token=token,
	)


def current_user(authorization: str = Header(default="")) -> sqlite3.Row:
	if not authorization.startswith("Bearer "):
		raise HTTPException(status_code=401, detail="Sign in to continue.")

	token = authorization[7:].strip()
	supabase_user = _supabase_user(token)
	auth_user_id = supabase_user.get("id")
	email = (supabase_user.get("email") or "").strip().lower()
	if not auth_user_id or not email:
		raise HTTPException(status_code=401, detail="Supabase returned an incomplete user.")

	with connect() as conn:
		row = conn.execute(
			"SELECT * FROM users WHERE auth_user_id = ?",
			(auth_user_id,),
		).fetchone()

		if row is None:
			row = conn.execute(
				"SELECT * FROM users WHERE email = ?",
				(email,),
			).fetchone()

		if row is not None and not row["auth_user_id"]:
			conn.execute(
				"UPDATE users SET auth_user_id = ? WHERE id = ?",
				(auth_user_id, row["id"]),
			)
			row = conn.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()

		if row is None:
			name = (supabase_user.get("user_metadata") or {}).get("full_name") or email.split("@", 1)[0]
			password_hash, salt = hash_password(secrets.token_urlsafe(32))
			conn.execute(
				"INSERT INTO users (auth_user_id, email, name, password_hash, salt, created_at) VALUES (?,?,?,?,?,?)",
				(auth_user_id, email, name, password_hash, salt, time.time()),
			)
			row = conn.execute("SELECT * FROM users WHERE auth_user_id = ?", (auth_user_id,)).fetchone()

		if row is None:
			raise HTTPException(status_code=401, detail="Unable to link your Supabase account.")

	return row
