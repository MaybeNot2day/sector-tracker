"""EDIT_TOKEN gate on watchlist mutations.

Contract: when settings.edit_token is empty every request is allowed; when it
is set, mutation requests must carry the exact token in X-Edit-Token or get
401 "edit_token_required". Reads are never gated.

The app lifespan starts network pollers, so these tests never run it: the
dependency is exercised directly and over HTTP via a TestClient that is not
entered as a context manager (which is what triggers lifespan).
"""

from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from starlette.testclient import TestClient

from app.main import app, require_edit_token

TOKEN = "s3cret-edit-token"


@pytest.fixture
def configure_edit_token(tmp_path: Path) -> Iterator[Callable[[str], None]]:
    """Install a stub app.state.settings with a chosen edit_token; restore after.

    watchlist_path points into tmp_path so an accidentally ungated mutation
    could never touch real data.
    """
    had_settings = hasattr(app.state, "settings")
    original = app.state.settings if had_settings else None

    def _configure(edit_token: str) -> None:
        app.state.settings = SimpleNamespace(
            edit_token=edit_token,
            watchlist_path=tmp_path / "watchlists.yaml",
        )

    yield _configure

    if had_settings:
        app.state.settings = original
    else:
        del app.state.settings


# --- require_edit_token: allow/deny matrix ---


@pytest.mark.parametrize(
    ("edit_token", "header"),
    [
        pytest.param("", None, id="empty-token-missing-header"),
        pytest.param("", "anything", id="empty-token-ignores-header"),
        pytest.param(TOKEN, TOKEN, id="exact-match"),
    ],
)
def test_require_edit_token_allows(
    configure_edit_token: Callable[[str], None],
    edit_token: str,
    header: str | None,
) -> None:
    configure_edit_token(edit_token)

    assert require_edit_token(header) is None


@pytest.mark.parametrize(
    "header",
    [
        pytest.param(None, id="missing-header"),
        pytest.param("", id="empty-header"),
        pytest.param("wrong", id="wrong-token"),
        pytest.param(TOKEN[:-1], id="prefix-of-token"),
        pytest.param(TOKEN + "x", id="token-plus-suffix"),
        pytest.param(TOKEN.upper(), id="case-differs"),
    ],
)
def test_require_edit_token_rejects_with_401(
    configure_edit_token: Callable[[str], None],
    header: str | None,
) -> None:
    configure_edit_token(TOKEN)

    with pytest.raises(HTTPException) as excinfo:
        require_edit_token(header)

    assert excinfo.value.status_code == 401
    assert excinfo.value.detail == "edit_token_required"


# --- gate enforced over HTTP, before any handler state is touched ---

MUTATION_REQUESTS = [
    pytest.param("POST", "/api/groups", {"name": "NEWGRP"}, id="create-group"),
    pytest.param("DELETE", "/api/groups/TEST", None, id="delete-group"),
    pytest.param("POST", "/api/groups/TEST/assets", {"symbol": "AAPL"}, id="create-asset"),
    pytest.param("DELETE", "/api/groups/TEST/assets/AAPL", None, id="delete-asset"),
]


@pytest.mark.parametrize(("method", "path", "body"), MUTATION_REQUESTS)
def test_mutation_endpoints_reject_missing_token(
    configure_edit_token: Callable[[str], None],
    method: str,
    path: str,
    body: dict[str, str] | None,
) -> None:
    configure_edit_token(TOKEN)
    client = TestClient(app)

    response = client.request(method, path, json=body)

    assert response.status_code == 401
    assert response.json()["detail"] == "edit_token_required"


def test_mutation_endpoint_rejects_wrong_token_header(
    configure_edit_token: Callable[[str], None],
) -> None:
    configure_edit_token(TOKEN)
    client = TestClient(app)

    response = client.post(
        "/api/groups",
        json={"name": "NEWGRP"},
        headers={"X-Edit-Token": "wrong"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "edit_token_required"


# --- wiring: exactly the four mutation routes are gated, reads stay open ---


def test_gate_is_wired_to_exactly_the_mutation_routes() -> None:
    gated = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        and any(dep.call is require_edit_token for dep in route.dependant.dependencies)
        for method in route.methods
    }

    assert gated == {
        ("POST", "/api/groups"),
        ("DELETE", "/api/groups/{group_name}"),
        ("POST", "/api/groups/{group_name}/assets"),
        ("DELETE", "/api/groups/{group_name}/assets/{symbol}"),
        ("POST", "/api/reports"),
        ("DELETE", "/api/reports/{report_id}"),
    }


# --- payload validation: '/' in a symbol or group name is rejected up front;
# uvicorn decodes %2F before routing, so such an asset could never be deleted ---


def test_create_asset_rejects_symbol_containing_slash(
    configure_edit_token: Callable[[str], None],
) -> None:
    configure_edit_token("")
    client = TestClient(app)

    response = client.post("/api/groups/TEST/assets", json={"symbol": "BRK/A"})

    assert response.status_code == 422
