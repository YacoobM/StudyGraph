#!/usr/bin/env python3
"""
StudyGraph backend integration tests — real HTTP against a running server.

Run:
    python dev_server.py &
    python test_integration.py

Exits non-zero if any test fails.
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE = "http://localhost:8000"
DB = Path(__file__).parent / "studygraph.db"

PASSED, FAILED = [], []


# ---------------------------------------------------------------- helpers
def call(method, path, body=None, token=None, form=None, raw=False):
    """Returns (status, parsed_body)."""
    url = BASE + path
    headers = {}
    data = None

    if token:
        headers["Authorization"] = f"Bearer {token}"

    if form is not None:
        boundary = f"----sg{uuid.uuid4().hex}"
        buf = io.BytesIO()
        for name, value in form.get("fields", {}).items():
            buf.write(f"--{boundary}\r\n".encode())
            buf.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            buf.write(f"{value}\r\n".encode())
        if "file" in form:
            fname, content, ctype = form["file"]
            buf.write(f"--{boundary}\r\n".encode())
            buf.write(
                f'Content-Disposition: form-data; name="file"; filename="{fname}"\r\n'.encode()
            )
            buf.write(f"Content-Type: {ctype}\r\n\r\n".encode())
            buf.write(content)
            buf.write(b"\r\n")
        buf.write(f"--{boundary}--\r\n".encode())
        data = buf.getvalue()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = r.read()
            return r.status, (payload if raw else _parse(payload))
    except urllib.error.HTTPError as e:
        payload = e.read()
        return e.code, (payload if raw else _parse(payload))


def _parse(payload):
    if not payload:
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return {"_raw": payload.decode(errors="replace")}


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append((name, detail))
        print(f"  FAIL  {name}" + (f"  -> {detail}" if detail else ""))


def section(title):
    print(f"\n{title}")
    print("-" * len(title))


PDF_BYTES = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
) * 12


# ---------------------------------------------------------------- tests
def main():
    print("=" * 66)
    print("StudyGraph integration tests")
    print(f"target: {BASE}")
    print("=" * 66)

    # ---- reachability
    section("1. Server reachable")
    try:
        status, body = call("GET", "/health")
    except Exception as e:
        print(f"\nCannot reach {BASE}: {e}")
        print("Start it first:  python dev_server.py")
        sys.exit(2)
    check("GET /health returns 200", status == 200, f"got {status}")
    check("health payload is {'status':'ok'}", body.get("status") == "ok", str(body))

    # ---- route surface
    section("2. Route surface")
    status, spec = call("GET", "/openapi.json")
    paths = spec.get("paths", {}) if status == 200 else {}
    for route in [
        "/api/auth/signup", "/api/auth/login", "/api/auth/me", "/api/auth/logout",
        "/api/documents", "/api/documents/{doc_id}",
        "/api/upload", "/api/upload/status", "/api/graph", "/api/query",
    ]:
        check(f"route exists: {route}", route in paths, "missing from OpenAPI")

    # ---- signup validation
    section("3. Sign-up validation")
    email = f"sam_{uuid.uuid4().hex[:8]}@monash.edu"

    status, body = call("POST", "/api/auth/signup",
                        {"email": "not-an-email", "password": "password123", "name": "X"})
    check("rejects malformed email (422)", status == 422, f"got {status}: {body}")

    status, body = call("POST", "/api/auth/signup",
                        {"email": email, "password": "short", "name": "X"})
    check("rejects password under 8 chars (422)", status == 422, f"got {status}: {body}")

    status, body = call("POST", "/api/auth/signup",
                        {"email": email, "password": "correct horse battery", "name": "Sam"})
    check("accepts valid signup (201)", status == 201, f"got {status}: {body}")
    token_a = body.get("token") if status == 201 else None
    check("signup returns a token", bool(token_a))
    check("signup returns the user", (body or {}).get("user", {}).get("email") == email, str(body))
    check("signup does not echo the password",
          "password" not in json.dumps(body or {}).lower(), str(body))

    status, body = call("POST", "/api/auth/signup",
                        {"email": email, "password": "correct horse battery", "name": "Sam"})
    check("rejects duplicate email (409)", status == 409, f"got {status}: {body}")

    # ---- password storage
    section("4. Password storage")
    if DB.exists():
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        conn.close()
        check("password is not stored in plaintext",
              row is not None and "correct horse battery" not in row["password_hash"])
        check("hash is PBKDF2-sized (64 hex chars)",
              row is not None and len(row["password_hash"]) == 64,
              f'len={len(row["password_hash"]) if row else "no row"}')
        check("per-user salt is stored", row is not None and len(row["salt"]) >= 16)
    else:
        check("database file exists", False, f"{DB} not found")

    # ---- login
    section("5. Sign-in")
    status, body = call("POST", "/api/auth/login", {"email": email, "password": "wrong password"})
    check("wrong password rejected (401)", status == 401, f"got {status}")
    wrong_pw_msg = (body or {}).get("detail")

    status, body = call("POST", "/api/auth/login",
                        {"email": f"ghost_{uuid.uuid4().hex[:6]}@x.com", "password": "whatever12"})
    check("unknown email rejected (401)", status == 401, f"got {status}")
    check("same message for wrong password and unknown email (no account enumeration)",
          (body or {}).get("detail") == wrong_pw_msg,
          f'{wrong_pw_msg!r} vs {(body or {}).get("detail")!r}')

    status, body = call("POST", "/api/auth/login", {"email": email, "password": "correct horse battery"})
    check("correct credentials accepted (200)", status == 200, f"got {status}: {body}")
    token_a = (body or {}).get("token") or token_a
    check("login is case-insensitive on email",
          call("POST", "/api/auth/login",
               {"email": email.upper(), "password": "correct horse battery"})[0] == 200)

    # ---- session
    section("6. Session enforcement")
    check("GET /api/auth/me with token (200)", call("GET", "/api/auth/me", token=token_a)[0] == 200)
    check("GET /api/auth/me without token (401)", call("GET", "/api/auth/me")[0] == 401)
    check("GET /api/auth/me with junk token (401)",
          call("GET", "/api/auth/me", token="not-a-real-token")[0] == 401)
    check("GET /api/documents without token (401)", call("GET", "/api/documents")[0] == 401)

    # ---- documents
    section("7. Document management")
    status, body = call("GET", "/api/documents", token=token_a)
    check("document list starts empty for a new user",
          status == 200 and body.get("documents") == [], str(body))

    status, body = call("POST", "/api/documents", token=token_a,
                        form={"file": ("lecture-08.pdf", PDF_BYTES, "application/pdf")})
    check("upload a PDF (201)", status == 201, f"got {status}: {body}")
    doc = (body or {}).get("document", {})
    doc_id = doc.get("id")
    check("row records the filename", doc.get("filename") == "lecture-08.pdf", str(doc))
    check("row records a byte size", doc.get("size_bytes") == len(PDF_BYTES), str(doc))
    check("row starts as queued", doc.get("status") == "queued", str(doc))

    status, body = call("POST", "/api/documents", token=token_a,
                        form={"file": ("virus.exe", b"MZ\x90\x00", "application/octet-stream")})
    check("rejects unsupported file type (415)", status == 415, f"got {status}: {body}")

    status, body = call("POST", "/api/documents", token=token_a,
                        form={"file": ("empty.pdf", b"", "application/pdf")})
    check("rejects empty file (422)", status == 422, f"got {status}: {body}")

    status, body = call("GET", "/api/documents", token=token_a)
    check("list shows exactly the one accepted upload",
          status == 200 and len(body.get("documents", [])) == 1, str(body))
    check("totals block is present",
          isinstance((body or {}).get("totals"), dict), str(body))

    status, body = call("PATCH", f"/api/documents/{doc_id}", token=token_a,
                        body={"status": "done", "concepts": 19, "relationships": 24})
    check("patch updates status and counts",
          status == 200 and body["document"]["concepts"] == 19
          and body["document"]["status"] == "done", f"{status}: {body}")

    status, _ = call("PATCH", "/api/documents/999999", token=token_a, body={"status": "done"})
    check("patch on a missing document (404)", status == 404, f"got {status}")

    # ---- ownership isolation
    section("8. Ownership isolation")
    email_b = f"other_{uuid.uuid4().hex[:8]}@monash.edu"
    _, body_b = call("POST", "/api/auth/signup",
                     {"email": email_b, "password": "another password", "name": "Riya"})
    token_b = (body_b or {}).get("token")

    status, body = call("GET", "/api/documents", token=token_b)
    check("second user sees an empty library",
          status == 200 and body.get("documents") == [], str(body))
    check("second user cannot delete the first user's document (404)",
          call("DELETE", f"/api/documents/{doc_id}", token=token_b)[0] == 404)
    check("second user cannot patch the first user's document (404)",
          call("PATCH", f"/api/documents/{doc_id}", token=token_b, body={"status": "done"})[0] == 404)
    check("first user's document survived those attempts",
          len(call("GET", "/api/documents", token=token_a)[1]["documents"]) == 1)

    # ---- extraction pipeline
    section("9. Extraction pipeline (upload -> poll -> graph)")
    status, body = call("POST", "/api/upload", token=token_a,
                        form={"file": ("lecture-08.pdf", PDF_BYTES, "application/pdf")})
    check("POST /api/upload accepted (200)", status == 200, f"got {status}: {body}")
    check("responds with status=processing", (body or {}).get("status") == "processing", str(body))
    check("assigns a week", str((body or {}).get("week", "")).startswith("Week"), str(body))

    stages, final = [], None
    deadline = time.time() + 30
    while time.time() < deadline:
        _, s = call("GET", "/api/upload/status")
        stage = f'{s.get("stage")}:{s.get("operation", "")}'
        if stage not in stages:
            stages.append(stage)
        if s.get("stage") in ("done", "error"):
            final = s
            break
        time.sleep(0.3)

    check("pipeline reached a terminal stage", final is not None, f"stages seen: {stages}")
    check("pipeline finished without error",
          final is not None and final.get("stage") == "done", str(final))
    check("pipeline reported progress stages (poll loop is exercised)",
          len(stages) >= 2, f"stages: {stages}")
    check("pipeline reports concept count",
          final is not None and final.get("total_concepts", 0) > 0, str(final))

    status, graph = call("GET", "/api/graph")
    check("GET /api/graph returns 200", status == 200, f"got {status}")
    check("graph has nodes", len(graph.get("nodes", [])) > 0, str(graph)[:200])
    check("graph has edges", len(graph.get("edges", [])) > 0, str(graph)[:200])
    node_keys = set(graph["nodes"][0].keys()) if graph.get("nodes") else set()
    check("nodes carry id and label", {"id", "label"} <= node_keys, str(node_keys))
    edge_keys = set(graph["edges"][0].keys()) if graph.get("edges") else set()
    check("edges carry from/to/relation", {"from", "to", "relation"} <= edge_keys, str(edge_keys))

    # ---- RAG route
    section("10. Query route")
    status, body = call("POST", "/api/query", {"query": "how does the proton gradient form"})
    check("POST /api/query returns 200", status == 200, f"got {status}: {body}")
    for key in ("answer", "citations", "citation_label", "chunks_used", "chunks", "subgraph"):
        check(f"response carries `{key}` (RAGResponse shape)", key in (body or {}), str(body)[:160])

    status, body = call("POST", "/api/query", {"query": ""})
    check("empty query rejected (422)", status == 422, f"got {status}")

    # ---- CORS
    section("11. CORS for the browser")
    req = urllib.request.Request(
        BASE + "/api/auth/login", method="OPTIONS",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            allow_origin = r.headers.get("access-control-allow-origin")
            allow_headers = (r.headers.get("access-control-allow-headers") or "").lower()
        check("preflight allows a localhost origin", allow_origin is not None, "no header")
        check("preflight allows the content-type header",
              "content-type" in allow_headers or allow_headers == "*", allow_headers)
    except Exception as e:
        check("preflight succeeds", False, str(e))

    # ---- deletion
    section("12. Deletion")
    check("delete the document (200)", call("DELETE", f"/api/documents/{doc_id}", token=token_a)[0] == 200)
    check("library is empty afterwards",
          call("GET", "/api/documents", token=token_a)[1]["documents"] == [])
    check("deleting it again returns 404",
          call("DELETE", f"/api/documents/{doc_id}", token=token_a)[0] == 404)

    # ---- logout
    section("13. Sign-out")
    check("logout returns 200", call("POST", "/api/auth/logout", token=token_a)[0] == 200)
    check("token stops working after logout (401)",
          call("GET", "/api/auth/me", token=token_a)[0] == 401)
    check("other user's session is unaffected",
          call("GET", "/api/auth/me", token=token_b)[0] == 200)

    # ---- summary
    total = len(PASSED) + len(FAILED)
    print("\n" + "=" * 66)
    print(f"RESULT: {len(PASSED)}/{total} passed")
    if FAILED:
        print(f"\n{len(FAILED)} failure(s):")
        for name, detail in FAILED:
            print(f"  - {name}" + (f"\n      {detail}" if detail else ""))
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
