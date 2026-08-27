"""
Auth + document management for StudyGraph.

Adds what main.py does not have:
  POST   /api/auth/signup
  POST   /api/auth/login
  POST   /api/auth/logout
  GET    /api/auth/me
  GET    /api/documents
  POST   /api/documents
  DELETE /api/documents/{doc_id}
  PATCH  /api/documents/{doc_id}

Storage is SQLite (stdlib), password hashing is PBKDF2-HMAC-SHA256 (stdlib).
No new pip dependencies, so this runs even without the heavy ML stack in
requirements.txt installed.

To mount on the existing app, add two lines to main.py:

    from auth_documents import router as auth_router
    app.include_router(auth_router)

Everything here is namespaced under /api/auth and /api/documents, so it
cannot collide with existing routes except /api/documents/file/{filename},
which main.py already owns and this module deliberately does not define.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field

# ----------------------------------------------------------------------------
# config
# ----------------------------------------------------------------------------

DB_PATH = Path(__file__).parent / "studygraph.db"
DOC_STORE = Path(__file__).parent / "uploads"
DOC_STORE.mkdir(exist_ok=True)

PBKDF2_ITERATIONS = 210_000
SESSION_TTL_SECONDS = 60 * 60 * 24 * 14  # 14 days
MIN_PASSWORD_LEN = 8
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB

ALLOWED_SUFFIXES = {".pdf", ".docx", ".pptx", ".txt", ".md"}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

router = APIRouter(tags=["auth", "documents"])


# ----------------------------------------------------------------------------
# database
# ----------------------------------------------------------------------------

def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT    NOT NULL UNIQUE,
                name          TEXT    NOT NULL,
                password_hash TEXT    NOT NULL,
                salt          TEXT    NOT NULL,
                created_at    REAL    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT    PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at REAL    NOT NULL,
                expires_at REAL    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS documents (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                filename      TEXT    NOT NULL,
                stored_name   TEXT    NOT NULL,
                size_bytes    INTEGER NOT NULL,
                mime          TEXT    NOT NULL,
                status        TEXT    NOT NULL DEFAULT 'queued',
                week          TEXT,
                concepts      INTEGER NOT NULL DEFAULT 0,
                relationships INTEGER NOT NULL DEFAULT 0,
                error         TEXT,
                uploaded_at   REAL    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_documents_user ON documents(user_id);
            """
        )


# ----------------------------------------------------------------------------
# password hashing
# ----------------------------------------------------------------------------

def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS
    )
    return digest.hex(), salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    candidate, _ = hash_password(password, salt)
    # constant-time compare so timing does not leak the hash
    return hmac.compare_digest(candidate, stored_hash)


# ----------------------------------------------------------------------------
# models
# ----------------------------------------------------------------------------

class SignupRequest(BaseModel):
    email: str
    password: str
    name: str = Field(default="")


class LoginRequest(BaseModel):
    email: str
    password: str


class DocumentPatch(BaseModel):
    status: Optional[str] = None
    week: Optional[str] = None
    concepts: Optional[int] = None
    relationships: Optional[int] = None
    error: Optional[str] = None


def user_public(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "created_at": row["created_at"],
    }


def document_public(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "filename": row["filename"],
        "size_bytes": row["size_bytes"],
        "size_kb": round(row["size_bytes"] / 1024),
        "mime": row["mime"],
        "status": row["status"],
        "week": row["week"],
        "concepts": row["concepts"],
        "relationships": row["relationships"],
        "error": row["error"],
        "uploaded_at": row["uploaded_at"],
    }


# ----------------------------------------------------------------------------
# session handling
# ----------------------------------------------------------------------------

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
    """FastAPI dependency. Expects `Authorization: Bearer <token>`."""
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


# ----------------------------------------------------------------------------
# auth routes
# ----------------------------------------------------------------------------

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

    # Same message and same work either way, so the response does not reveal
    # whether the email exists.
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


# ----------------------------------------------------------------------------
# document routes
# ----------------------------------------------------------------------------

@router.get("/api/documents")
async def list_documents(user: sqlite3.Row = Depends(current_user)):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM documents WHERE user_id = ? ORDER BY uploaded_at DESC",
            (user["id"],),
        ).fetchall()

    docs = [document_public(r) for r in rows]
    return {
        "documents": docs,
        "totals": {
            "count": len(docs),
            "concepts": sum(d["concepts"] for d in docs),
            "relationships": sum(d["relationships"] for d in docs),
            "size_kb": sum(d["size_kb"] for d in docs),
        },
    }


@router.post("/api/documents", status_code=201)
async def create_document(
    file: UploadFile = File(...),
    week: str = Form(default=""),
    user: sqlite3.Row = Depends(current_user),
):
    """
    Register an uploaded file against the signed-in user.

    This owns the document *record*. Concept extraction still belongs to
    main.py's /api/upload — the frontend calls this first to get a row it can
    show immediately, then PATCHes the row with counts when extraction lands.
    """
    filename = Path(file.filename or "").name
    if not filename:
        raise HTTPException(status_code=422, detail="Choose a file to upload.")

    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        allowed = ", ".join(sorted(ALLOWED_SUFFIXES))
        raise HTTPException(status_code=415, detail=f"That file type is not supported. Use: {allowed}")

    data = await file.read()
    if len(data) == 0:
        raise HTTPException(status_code=422, detail="That file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"That file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )

    stored_name = f"{user['id']}_{int(time.time() * 1000)}_{filename}"
    (DOC_STORE / stored_name).write_bytes(data)

    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO documents
                (user_id, filename, stored_name, size_bytes, mime, status, week, uploaded_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                user["id"],
                filename,
                stored_name,
                len(data),
                file.content_type or "application/octet-stream",
                "queued",
                week or None,
                time.time(),
            ),
        )
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (cur.lastrowid,)).fetchone()

    return {"document": document_public(row)}


@router.patch("/api/documents/{doc_id}")
async def update_document(
    doc_id: int, body: DocumentPatch, user: sqlite3.Row = Depends(current_user)
):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=422, detail="Nothing to update.")

    assignments = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        owned = conn.execute(
            "SELECT 1 FROM documents WHERE id = ? AND user_id = ?", (doc_id, user["id"])
        ).fetchone()
        if not owned:
            raise HTTPException(status_code=404, detail="That document is not in your library.")

        conn.execute(
            f"UPDATE documents SET {assignments} WHERE id = ? AND user_id = ?",
            (*fields.values(), doc_id, user["id"]),
        )
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()

    return {"document": document_public(row)}


@router.delete("/api/documents/{doc_id}")
async def delete_document(doc_id: int, user: sqlite3.Row = Depends(current_user)):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM documents WHERE id = ? AND user_id = ?", (doc_id, user["id"])
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="That document is not in your library.")

        conn.execute("DELETE FROM documents WHERE id = ? AND user_id = ?", (doc_id, user["id"]))

    stored = DOC_STORE / row["stored_name"]
    if stored.exists():
        try:
            stored.unlink()
        except OSError:
            pass  # row is gone; a stray file is not worth failing the request

    return {"ok": True, "deleted": doc_id}


init_db()
