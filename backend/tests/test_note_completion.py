"""Completion updates must respect the graph in the URL and retain user identity."""
import sys
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.routes import notes
from repositories.users import SupabaseIdentity, current_supabase_identity


@pytest.fixture
def completion_api(monkeypatch):
    identity = SupabaseIdentity("user-id", "reader@example.com", "user-token")
    lookup = Mock(return_value={"id": "note"})
    update = Mock(return_value={"done": True})
    monkeypatch.setattr(notes, "get_supabase_note", lookup)
    monkeypatch.setattr(notes, "update_note_section", update)
    app = FastAPI()
    app.include_router(notes.router)
    app.dependency_overrides[current_supabase_identity] = lambda: identity
    ids = [str(uuid4()) for _ in range(3)]
    url = f"/api/graphs/{ids[0]}/notes/{ids[1]}/sections/{ids[2]}"
    with TestClient(app) as client:
        yield client, url, ids, identity, lookup, update


def test_wrong_graph_does_not_update_section(completion_api):
    client, url, ids, identity, lookup, update = completion_api
    lookup.return_value = None
    assert client.patch(url, json={"done": True}).status_code == 404
    lookup.assert_called_once_with(identity, ids[0], ids[1])
    update.assert_not_called()


@pytest.mark.parametrize("done", [True, False])
def test_completion_uses_authenticated_identity(completion_api, done):
    client, url, ids, identity, lookup, update = completion_api
    update.return_value = {"id": ids[2], "done": done}
    response = client.patch(url, json={"done": done, "title": "ignored"})
    assert response.status_code == 200
    assert response.json()["section"]["done"] is done
    lookup.assert_called_once_with(identity, ids[0], ids[1])
    update.assert_called_once_with(identity, ids[1], ids[2], {"done": done})


@pytest.mark.parametrize("body", [{}, {"done": "false"}, {"done": 1}, {"done": None}])
def test_invalid_completion_is_rejected(completion_api, body):
    client, url, _, _, lookup, update = completion_api
    assert client.patch(url, json=body).status_code == 422
    lookup.assert_not_called()
    update.assert_not_called()


def test_missing_section_returns_not_found(completion_api):
    client, url, _, _, _, update = completion_api
    update.return_value = None
    assert client.patch(url, json={"done": True}).status_code == 404
