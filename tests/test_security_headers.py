from starlette.testclient import TestClient

from app.main import app


def test_security_headers_cover_html_responses() -> None:
    client = TestClient(app, base_url="https://board.example")

    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["strict-transport-security"] == ("max-age=31536000; includeSubDomains")
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


def test_static_html_is_revalidated_while_hashed_assets_are_immutable() -> None:
    client = TestClient(app)

    html = client.get("/static/index.html")
    asset = client.get("/static/app.js")

    assert html.status_code == 200
    assert html.headers["cache-control"] == "no-cache"
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
