"""F-34: a delete DynamoDB refused is not HTTP 200.

``RegistryStore.delete`` and ``PromptLibraryStore.delete`` swallowed every exception and
returned ``False``; the routers served ``{"success": false}`` with a 200, which every client
reads as "deleted". The store now raises and the routers answer 503 with a retry hint; the
exception's type (never its text, which can echo the request) is what gets logged.
"""

from __future__ import annotations

import pytest
from app.routers import prompts as prompts_router
from app.routers import registry as registry_router
from app.services.auth import get_caller_sub
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient

CALLER = "alice-sub-1111"


def _ddb_error() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "FAKE-echoed-request-body"}},
        "DeleteItem",
    )


# --------------------------------------------------------------------------- the stores raise


def test_the_registry_store_raises_instead_of_returning_false():
    from app.services.registry_store import RegistryStore

    store = object.__new__(RegistryStore)

    class _Table:
        def delete_item(self, **_kw):
            raise _ddb_error()

    store._table = _Table()
    with pytest.raises(ClientError):
        store.delete("default", "slug")


def test_the_prompt_store_raises_instead_of_returning_false():
    from app.services.prompt_library_store import PromptLibraryStore

    store = object.__new__(PromptLibraryStore)

    class _Table:
        def delete_item(self, **_kw):
            raise _ddb_error()

    store._table = _Table()
    with pytest.raises(ClientError):
        store.delete("default", "p")


# --------------------------------------------------------------------------- the routers answer 503


class _Caps:
    authoritative_catalog = "platform"
    notes = ""

    def is_read_only(self, _entry):
        return False


class _FailingRegistry:
    def __init__(self, entry):
        self._entry = entry

    def capabilities(self):
        return _Caps()

    def get(self, org_id, slug):
        return self._entry

    def delete(self, org_id, slug):
        raise _ddb_error()


class _FailingPrompts:
    def __init__(self, entry):
        self._entry = entry

    def get(self, org_id, name):
        return self._entry

    def delete(self, org_id, name):
        raise _ddb_error()


def test_registry_delete_answers_503_and_never_echoes_the_error_text(monkeypatch, caplog):
    from app.services.registry_store import RegistryEntry

    entry = RegistryEntry(agent_slug="my-agent", owner_sub=CALLER, display_name="My Agent")
    monkeypatch.setattr(registry_router, "get_registry_provider", lambda: _FailingRegistry(entry))
    app = FastAPI()
    app.include_router(registry_router.router)
    app.dependency_overrides[get_caller_sub] = lambda: CALLER
    app.dependency_overrides[registry_router.caller_is_admin] = lambda: False
    with caplog.at_level("WARNING"):
        r = TestClient(app).delete("/api/registry/my-agent")
    assert r.status_code == 503, r.text
    assert "FAKE-echoed-request-body" not in r.text
    assert "FAKE-echoed-request-body" not in caplog.text
    assert "ClientError" in caplog.text


def test_prompt_delete_answers_503_and_never_echoes_the_error_text(monkeypatch, caplog):
    from app.services.prompt_library_store import PromptEntry

    entry = PromptEntry(prompt_name="p", owner_sub=CALLER, display_name="P")
    monkeypatch.setattr(prompts_router, "get_prompt_library_store", lambda: _FailingPrompts(entry))
    app = FastAPI()
    app.include_router(prompts_router.router)
    app.dependency_overrides[get_caller_sub] = lambda: CALLER
    with caplog.at_level("WARNING"):
        r = TestClient(app).delete("/api/prompts/p")
    assert r.status_code == 503, r.text
    assert "FAKE-echoed-request-body" not in r.text
    assert "FAKE-echoed-request-body" not in caplog.text


def test_a_successful_delete_is_still_a_200_with_success_true(monkeypatch):
    from app.services.prompt_library_store import PromptEntry

    class _Ok(_FailingPrompts):
        def delete(self, org_id, name):
            return True

    entry = PromptEntry(prompt_name="p", owner_sub=CALLER, display_name="P")
    monkeypatch.setattr(prompts_router, "get_prompt_library_store", lambda: _Ok(entry))
    app = FastAPI()
    app.include_router(prompts_router.router)
    app.dependency_overrides[get_caller_sub] = lambda: CALLER
    r = TestClient(app).delete("/api/prompts/p")
    assert r.status_code == 200 and r.json() == {"success": True, "prompt_name": "p"}
