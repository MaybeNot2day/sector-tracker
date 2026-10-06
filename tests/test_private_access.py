from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import app


@pytest.fixture()
def private_board(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(
        app.state, "settings",
        SimpleNamespace(read_username="reader", read_password="pässword", edit_token="edit"),
        raising=False,
    )
    monkeypatch.setattr(app.state, "groups", [], raising=False)
    monkeypatch.setattr(app.state, "base_groups", [], raising=False)
    monkeypatch.setattr(app.state, "candle_stream_service", None, raising=False)
    return TestClient(app)


@pytest.mark.parametrize("path", ["/", "/static/app.js", "/api/groups", "/api/health"])
def test_private_board_requires_read_credentials(private_board: TestClient, path: str) -> None:
    response = private_board.get(path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic ")
    assert private_board.get(path, auth=("reader", "wrong")).status_code == 401
    assert private_board.get(path, auth=("reader", "pässword")).status_code == 200


def test_read_credentials_do_not_authorize_backup(private_board: TestClient) -> None:
    response = private_board.get("/api/backup", auth=("reader", "pässword"))
    assert response.status_code == 401
    assert response.json()["detail"] == "edit_token_required"


@pytest.mark.parametrize("authorization", ["Basic !!!", "Basic YWJj", "Bearer anything"])
def test_invalid_authorization_is_rejected_not_a_server_error(
    private_board: TestClient, authorization: str
) -> None:
    response = private_board.get("/api/groups", headers={"Authorization": authorization})
    assert response.status_code == 401


def test_private_websocket_requires_credentials(private_board: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as rejected:
        with private_board.websocket_connect("/ws/candles?symbol=BTC&interval=1m"):
            pass
    assert rejected.value.code == 1008

    authorization = base64.b64encode("reader:pässword".encode()).decode()
    with private_board.websocket_connect(
        "/ws/candles?symbol=BTC&interval=1m", headers={"Authorization": f"Basic {authorization}"}
    ) as websocket:
        with pytest.raises(WebSocketDisconnect) as unavailable:
            websocket.receive_text()
        assert unavailable.value.code == 1011


def test_loopback_probe_does_not_require_read_credentials(private_board: TestClient) -> None:
    client = TestClient(app, client=("127.0.0.1", 50000))
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/groups").status_code == 401


def test_edit_token_authorizes_private_automation_reads(private_board: TestClient) -> None:
    assert private_board.get("/api/groups", headers={"X-Edit-Token": "wrong"}).status_code == 401
    assert private_board.get("/api/groups", headers={"X-Edit-Token": "edit"}).status_code == 200
