import sqlite3
from repositories.database import connect

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

def _update_document_status(document_id, status, **fields):
    if document_id is None:
        return
    allowed = {"status", "error", "week", "concepts", "relationships"}
    updates = {key: value for key, value in fields.items() if key in allowed}
    updates["status"] = status
    
    if not updates:
        return
    
    assignments = ", ".join(f"{key} = ?" for key in updates.keys())
    params = list(updates.values()) + [document_id]
    
    with connect() as conn:
        conn.execute(
            f"UPDATE documents SET {assignments} WHERE id = ?",
            params,
        )
