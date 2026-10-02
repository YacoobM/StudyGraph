import sqlite3

from app.config import DB_PATH


def connect() -> sqlite3.Connection:
	conn = sqlite3.connect(DB_PATH)
	conn.row_factory = sqlite3.Row
	conn.execute("PRAGMA foreign_keys = ON")
	return conn


def init_db() -> None:
	with connect() as conn:
		conn.executescript("""
			CREATE TABLE IF NOT EXISTS users (
				id            INTEGER PRIMARY KEY AUTOINCREMENT,
				auth_user_id  TEXT,
				email         TEXT    NOT NULL UNIQUE,
				name          TEXT    NOT NULL,
				password_hash TEXT    NOT NULL,
				salt          TEXT    NOT NULL,
				created_at    REAL    NOT NULL
			);

			CREATE TABLE IF NOT EXISTS sessions (
				token      TEXT PRIMARY KEY,
				user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
				created_at REAL NOT NULL,
				expires_at REAL NOT NULL
			);

			CREATE TABLE IF NOT EXISTS documents (
				id            INTEGER PRIMARY KEY AUTOINCREMENT,
				user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
				filename      TEXT NOT NULL,
				stored_name   TEXT NOT NULL,
				size_bytes    INTEGER NOT NULL,
				mime          TEXT NOT NULL,
				status        TEXT NOT NULL DEFAULT 'queued',
				week          TEXT,
				concepts      INTEGER NOT NULL DEFAULT 0,
				relationships INTEGER NOT NULL DEFAULT 0,
				error         TEXT,
				uploaded_at   REAL NOT NULL
			);

			CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
			CREATE INDEX IF NOT EXISTS idx_documents_user ON documents(user_id);
			""")
		conn.commit()
		
		columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
		if "auth_user_id" not in columns:
			conn.execute("ALTER TABLE users ADD COLUMN auth_user_id TEXT")
		conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_auth_user_id ON users(auth_user_id)")
		conn.commit() 