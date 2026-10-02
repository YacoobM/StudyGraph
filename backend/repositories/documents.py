import sqlite3


def document_public(row: sqlite3.Row) -> dict:
	payload = {
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
	if "graph_id" in row.keys():
		payload["graph_id"] = row["graph_id"]
	return payload
