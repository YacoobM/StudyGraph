"""Parent deletion cleans only descendant files, before cascading row deletion."""
import sys
import json
from pathlib import Path
from unittest.mock import Mock, MagicMock
from urllib.error import HTTPError

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from repositories import supabase_library as library
from repositories import supabase_documents as documents
from repositories.users import SupabaseIdentity


@pytest.mark.parametrize("kind", ["subject", "topic", "graph"])
@pytest.mark.parametrize("failure", [False, True])
def test_descendant_cleanup_precedes_parent_delete(monkeypatch, kind, failure):
    identity = SupabaseIdentity("user", "user@example.com", "token")
    events = []

    def request(caller, table, query, method="GET", **kwargs):
        assert caller is identity
        events.append((method, table, query))
        if "&id=eq.parent" in query:
            return [{"id": "parent"}]
        if table == "topics" and method == "GET":
            assert query == "?select=id&subject_id=eq.parent"
            return [{"id": "child-topic"}]
        if table == "graphs" and method == "GET":
            assert query == f"?select=id&topic_id=eq.{'child-topic' if kind == 'subject' else 'parent'}"
            return [{"id": "child-graph"}]
        return []

    graph_id = "parent" if kind == "graph" else "child-graph"
    doc = {"id": "doc", "graph_id": graph_id, "storage_path": "user/file.pdf"}
    listing = Mock(return_value=[doc])

    def cleanup(caller, rows):
        assert caller is identity
        assert rows == [doc]
        events.append(("FILE", rows[0]["id"]))
        if failure:
            raise HTTPException(502, "Storage unavailable")

    monkeypatch.setattr(library, "request_table", request)
    monkeypatch.setattr(library, "list_graph_documents", listing)
    monkeypatch.setattr(library, "delete_document_files", cleanup)
    action = getattr(library, f"delete_{kind}")
    if failure:
        with pytest.raises(HTTPException):
            action(identity, "parent")
        assert not any(event[0] == "DELETE" for event in events)
    else:
        action(identity, "parent")
        assert events[-2:] == [("FILE", "doc"), ("DELETE", kind + "s", "?id=eq.parent")]
    listing.assert_called_once_with(identity, graph_id)


@pytest.mark.parametrize("kind", ["subject", "topic", "graph"])
def test_inaccessible_parent_never_touches_storage(monkeypatch, kind):
    monkeypatch.setattr(library, "request_table", Mock(return_value=[]))
    cleanup = Mock()
    monkeypatch.setattr(library, "delete_document_files", cleanup)
    with pytest.raises(HTTPException) as error:
        getattr(library, f"delete_{kind}")(Mock(), "missing")
    assert error.value.status_code == 404
    cleanup.assert_not_called()


@pytest.mark.parametrize("status", [404, 403, 500])
def test_storage_retry_only_ignores_missing_file(monkeypatch, status):
    monkeypatch.setattr(documents, "SUPABASE_URL", "https://example.com")
    monkeypatch.setattr(documents, "SUPABASE_PUBLISHABLE_KEY", "key")
    monkeypatch.setattr(documents, "urlopen", Mock(side_effect=HTTPError("url", status, "error", {}, None)))
    identity = SupabaseIdentity("user", "user@example.com", "token")
    if status == 404:
        documents.delete_document_file(identity, {"storage_path": "user/file.pdf"})
    else:
        with pytest.raises(HTTPException):
            documents.delete_document_file(identity, {"storage_path": "user/file.pdf"})


@pytest.fixture
def bulk_storage(monkeypatch):
    monkeypatch.setattr(documents, "SUPABASE_URL", "https://example.com")
    monkeypatch.setattr(documents, "SUPABASE_PUBLISHABLE_KEY", "public-key")
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b"[]"
    send = Mock(return_value=response)
    monkeypatch.setattr(documents, "urlopen", send)
    return SupabaseIdentity("user", "user@example.com", "token"), send, response


@pytest.mark.parametrize("count", [0, 1, 1000, 1001, 2501])
def test_bulk_delete_request_boundaries(bulk_storage, count):
    identity, send, _ = bulk_storage
    paths = [f"user/graph-{i % 3}/file {i}.pdf" for i in range(count)]
    documents.delete_document_files(identity, [{"storage_path": path} for path in paths])
    assert send.call_count == (count + 999) // 1000
    actual = []
    for call in send.call_args_list:
        request = call.args[0]
        assert request.full_url == "https://example.com/storage/v1/object/studygraph-documents"
        assert request.get_method() == "DELETE"
        assert request.get_header("Authorization") == "Bearer token"
        assert request.get_header("Apikey") == "public-key"
        assert request.get_header("Content-type") == "application/json"
        batch = json.loads(request.data)["prefixes"]
        assert 1 <= len(batch) <= 1000
        actual.extend(batch)
    assert actual == paths


def test_bulk_delete_skips_missing_paths_and_deduplicates(bulk_storage):
    identity, send, _ = bulk_storage
    path = "user/folder/a #?%.pdf"
    documents.delete_document_files(identity, [
        {}, {"storage_path": None}, {"storage_path": ""},
        {"storage_path": path}, {"storage_path": path},
    ])
    assert send.call_count == 1
    assert json.loads(send.call_args.args[0].data) == {"prefixes": [path]}


@pytest.mark.parametrize("status", [403, 404, 500])
def test_failed_batch_stops_later_batches(bulk_storage, status):
    identity, send, response = bulk_storage
    send.side_effect = [response, HTTPError("url", status, "error", {}, None), response]
    with pytest.raises(HTTPException) as error:
        documents.delete_document_files(identity, [
            {"storage_path": f"user/{i}.pdf"} for i in range(2501)
        ])
    assert error.value.status_code == 502
    assert send.call_count == 2


def test_hierarchy_combines_documents_across_graphs(monkeypatch):
    identity = SupabaseIdentity("user", "user@example.com", "token")
    rows = {"a": [{"storage_path": "user/a.pdf"}], "b": [{"storage_path": "user/b.pdf"}]}
    monkeypatch.setattr(library, "list_graph_documents", lambda caller, graph_id: rows[graph_id])
    cleanup = Mock()
    monkeypatch.setattr(library, "delete_document_files", cleanup)
    library._delete_graph_files(identity, [{"id": "a"}, {"id": "b"}])
    cleanup.assert_called_once_with(identity, rows["a"] + rows["b"])
