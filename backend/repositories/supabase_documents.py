import json
import logging
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import uuid4

from fastapi import HTTPException

from app.config import SUPABASE_PUBLISHABLE_KEY, SUPABASE_URL
from repositories.supabase_graphs import request_table
from repositories.users import SupabaseIdentity


logger = logging.getLogger(__name__)
BUCKET = "studygraph-documents"


def _storage_request(identity: SupabaseIdentity, path: str, method: str, data: bytes | None = None, mime: str | None = None) -> None:
	if not SUPABASE_URL or not SUPABASE_PUBLISHABLE_KEY:
		raise HTTPException(status_code=503, detail="Supabase is not configured.")

	headers = {
		"Authorization": f"Bearer {identity.access_token}",
		"apikey": SUPABASE_PUBLISHABLE_KEY,
	}
	if mime:
		headers["Content-Type"] = mime
	request = Request(
		f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{quote(path, safe='/')}",
		data=data,
		headers=headers,
		method=method,
	)
	try:
		with urlopen(request, timeout=30) as response:
			response.read()
	except (HTTPError, URLError, TimeoutError) as error:
		raise HTTPException(status_code=502, detail="Unable to store document in Supabase Storage.") from error


def list_graph_documents(identity: SupabaseIdentity, graph_id: str) -> list[dict]:
	return request_table(
		identity,
		"documents",
		f"?select=id,graph_id,filename,storage_path,size_bytes,mime,status,week,concepts,relationships,error,uploaded_at&graph_id=eq.{graph_id}&order=uploaded_at.desc,id.desc",
	)



def persist_document_chunks(
	identity: SupabaseIdentity,
	document_id: str,
	chunks: list[dict],
	embeddings: list[list[float]],
) -> list[dict]:
	if not chunks:
		return []
	if len(chunks) != len(embeddings):
		raise ValueError("Each chunk must have exactly one embedding.")
	rows = []
	for offset in range(0, len(chunks), 500):
		batch = chunks[offset:offset + 500]
		payload = [
			{
				"id": chunk["chunk_id"],
				"document_id": document_id,
				"chunk_index": chunk["index"],
				"content": chunk["text"],
				"pages": chunk["pages"],
				"content_hash": chunk["content_hash"],
				"embedding": "[" + ",".join(format(value, ".9g") for value in embeddings[offset + index]) + "]",
			}
			for index, chunk in enumerate(batch)
		]
		rows.extend(request_table(
			identity,
			"document_chunks",
			"?select=id,document_id,chunk_index,content_hash",
			method="POST",
			payload=payload,
			prefer="return=representation",
		))
	return rows


def set_graph_document_status(
	identity: SupabaseIdentity,
	document_id: str,
	status: str,
	error: str | None = None,
	week: str | None = None,
) -> None:
	payload = {"status": status, "error": error}
	if week is not None:
		payload["week"] = week
	request_table(
		identity,
		"documents",
		f"?id=eq.{document_id}",
		method="PATCH",
		payload=payload,
		prefer="return=minimal",
	)


def delete_graph_document_data(identity: SupabaseIdentity, document: dict) -> None:
	request_table(
		identity,
		"document_chunks",
		f"?document_id=eq.{document['id']}",
		method="DELETE",
	)


	if document.get("storage_path"):
		_storage_request(identity, document["storage_path"], "DELETE")
	request_table(
		identity,
		"documents",
		f"?id=eq.{document['id']}&graph_id=eq.{document['graph_id']}",
		method="DELETE",
	)


def signed_graph_document_url(identity: SupabaseIdentity, graph_id: str, document_id: str) -> str:
	rows = request_table(
		identity,
		"documents",
		f"?select=storage_path&graph_id=eq.{graph_id}&id=eq.{document_id}",
	)
	if not rows or not rows[0].get("storage_path"):
		raise HTTPException(status_code=404, detail="Document not found in this graph.")
	path = rows[0]["storage_path"]
	request = Request(
		f"{SUPABASE_URL}/storage/v1/object/sign/{BUCKET}/{quote(path, safe='/')}",
		data=json.dumps({"expiresIn": 300}).encode(),
		headers={
			"Authorization": f"Bearer {identity.access_token}",
			"apikey": SUPABASE_PUBLISHABLE_KEY,
			"Content-Type": "application/json",
			"Accept": "application/json",
		},
		method="POST",
	)
	try:
		with urlopen(request, timeout=10) as response:
			payload = json.loads(response.read())
	except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
		raise HTTPException(status_code=502, detail="Unable to open document from Supabase Storage.") from error
	signed = payload.get("signedURL") or payload.get("signedUrl")
	if not signed:
		raise HTTPException(status_code=502, detail="Supabase did not return a signed document URL.")
	return signed if signed.startswith("http") else f"{SUPABASE_URL}/storage/v1{signed}"


def create_graph_document(
	identity: SupabaseIdentity,
	graph_id: str,
	filename: str,
	content: bytes,
	mime: str,
) -> dict:
	owned_graph = request_table(
		identity,
		"graphs",
		f"?select=id&id=eq.{graph_id}",
	)
	if not owned_graph:
		raise HTTPException(status_code=404, detail="Graph not found.")

	storage_path = f"{identity.auth_user_id}/{uuid4().hex}/{filename}"
	_storage_request(identity, storage_path, "POST", data=content, mime=mime)
	try:
		rows = request_table(
			identity,
			"documents",
			"?select=id,graph_id,filename,storage_path,size_bytes,mime,status,week,concepts,relationships,error,uploaded_at",
			method="POST",
			payload={
				"graph_id": graph_id,
				"filename": filename,
				"storage_path": storage_path,
				"size_bytes": len(content),
				"mime": mime,
				"status": "uploaded",
			},
			prefer="return=representation",
		)
		if len(rows) != 1:
			raise HTTPException(status_code=502, detail="Supabase did not return the created document.")
		return rows[0]
	except Exception:
		try:
			_storage_request(identity, storage_path, "DELETE")
		except HTTPException:
			logger.exception("document_storage_cleanup_failed graph_id=%s", graph_id)
		raise
