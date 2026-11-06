import hashlib
from uuid import UUID, uuid5


def build_document_chunks(pages, source, document_id=None, size=1024, overlap=150):
	"""
 	Split page text into overlapping chunks while preserving spanned-page citations.

	When a document ID is available, derive stable chunk IDs from it and each chunk's
	content digest so reprocessing the same content keeps the same identifiers.
	"""
	full_text = ""
	position_to_page = []
	for page in pages:
		page_text = page["text"]
		full_text += page_text + " "
		position_to_page.extend([page["page"]] * (len(page_text) + 1))

	if not full_text.strip():
		return []

	step = max(size - overlap, 1)
	chunks = []
	start = 0
	while start < len(full_text):
		end = min(start + size, len(full_text))
		chunk_text = full_text[start:end]
		spanned_pages = sorted(set(position_to_page[start:end]))
		chunk_index = len(chunks)
		digest = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()[:12]
		if document_id is None:
			chunk_id = f"{source}:chunk:{chunk_index}:{digest}"
		else:
			chunk_id = str(uuid5(UUID(str(document_id)), f"{chunk_index}:{digest}"))
		chunks.append({
			"chunk_id": chunk_id,
			"text": chunk_text,
			"pages": spanned_pages,
			"source": source,
			"index": chunk_index,
			"content_hash": hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
		})
		start += step
	return chunks