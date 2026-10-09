import sqlite3


def graph_public(row: sqlite3.Row) -> dict:
	return {
		"id": row["id"],
		"user_id": row["user_id"],
		"name": row["name"],
		"created_at": row["created_at"],
		"status": row.get("status", "active"),
	}


def create_graph(conn: sqlite3.Connection, user_id: int, name: str, created_at: float | None = None, status: str = "active") -> int:
	created = created_at if created_at is not None else __import__("time").time()
	cur = conn.execute(
		"INSERT INTO graphs (user_id, name, created_at, status) VALUES (?, ?, ?, ?)",
		(user_id, name, created, status),
	)
	return int(cur.lastrowid)


def set_graph_status(conn: sqlite3.Connection, graph_id: int, status: str) -> None:
	if status not in {"active", "needs_regeneration"}:
		raise ValueError(f"Unsupported graph status: {status}")
	conn.execute("UPDATE graphs SET status = ? WHERE id = ?", (status, graph_id))


def mark_graph_for_regeneration(conn: sqlite3.Connection, graph_id: int) -> None:
	set_graph_status(conn, graph_id, "needs_regeneration")


def attach_document_to_graph(conn: sqlite3.Connection, graph_id: int, document_id: int) -> None:
	conn.execute(
		"UPDATE documents SET graph_id = ? WHERE id = ?",
		(graph_id, document_id),
	)


def detach_document_from_graph(conn: sqlite3.Connection, graph_id: int, document_id: int) -> None:
	conn.execute(
		"UPDATE documents SET graph_id = NULL WHERE id = ? AND graph_id = ?",
		(document_id, graph_id),
	)
