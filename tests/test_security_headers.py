from starlette.testclient import TestClient

from app.main import app


def test_security_headers_cover_html_responses() -> None:
    client = TestClient(app, base_url="https://board.example")

    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["strict-transport-security"] == (
        "max-age=31536000; includeSubDomains"
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["permissions-policy"] == "camera=(), geolocation=(), microphone=()"
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_hsts_is_not_sent_over_plain_http() -> None:
    client = TestClient(app, base_url="http://board.example")

    response = client.get("/api/health")

    assert response.status_code == 200
    assert "strict-transport-security" not in response.headers
