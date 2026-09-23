from __future__ import annotations

import re
import sqlite3
import time

from fastapi import APIRouter, Depends, Header, HTTPException

from repositories.database import connect, init_db
from repositories.users import (
	create_session,
	current_user,
	hash_password,
	user_public,
	verify_password,
)
from schemas.auth import LoginRequest, SignupRequest


MIN_PASSWORD_LEN = 8
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

router = APIRouter(tags=["auth"])


@router.post("/api/auth/signup", status_code=201)
async def signup(body: SignupRequest):
	email = body.email.strip().lower()

	if not EMAIL_RE.match(email):
		raise HTTPException(status_code=422, detail="Enter a valid email address.")
	if len(body.password) < MIN_PASSWORD_LEN:
		raise HTTPException(
			status_code=422,
			detail=f"Use at least {MIN_PASSWORD_LEN} characters for your password.",
		)

	name = body.name.strip() or email.split("@")[0]
	password_hash, salt = hash_password(body.password)

	with connect() as conn:
		existing = conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone()
		if existing:
			raise HTTPException(status_code=409, detail="That email already has an account.")

		cur = conn.execute(
			"INSERT INTO users (email, name, password_hash, salt, created_at) VALUES (?,?,?,?,?)",
			(email, name, password_hash, salt, time.time()),
		)
		user_id = cur.lastrowid
		row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

	return {"token": create_session(user_id), "user": user_public(row)}


@router.post("/api/auth/login")
async def login(body: LoginRequest):
	email = body.email.strip().lower()

	with connect() as conn:
		row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

	if row is None:
		hash_password(body.password, "dummysaltdummysalt")
		raise HTTPException(status_code=401, detail="That email and password do not match.")

	if not verify_password(body.password, row["password_hash"], row["salt"]):
		raise HTTPException(status_code=401, detail="That email and password do not match.")

	return {"token": create_session(row["id"]), "user": user_public(row)}


@router.get("/api/auth/me")
async def me(user: sqlite3.Row = Depends(current_user)):
	return {"user": user_public(user)}


@router.post("/api/auth/logout")
async def logout(authorization: str = Header(default="")):
	if authorization.startswith("Bearer "):
		with connect() as conn:
			conn.execute("DELETE FROM sessions WHERE token = ?", (authorization[7:].strip(),))
	return {"ok": True}

__all__ = ["current_user", "router"]
