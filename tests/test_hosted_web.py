"""The hosted entry point rejects requests from other sites."""

from product_finder.config import AppConfig
from deploy import serve


def test_hosted_web_origin_and_session(tmp_path, monkeypatch):
    config = AppConfig(db_path=str(tmp_path / "test.db"))
    host = "products-sandbox.stoked.tech"
    origin = f"https://{host}"
    monkeypatch.setenv("PRODUCT_FINDER_HOST", host)
    monkeypatch.setenv("PRODUCT_FINDER_SECRET_KEY", "private-test-session-key")
    monkeypatch.setattr(serve, "load_config", lambda path: config)
    client = serve.hosted_app().test_client()

    assert client.get("/", base_url="https://other.example").status_code == 400
    home = client.get("/", base_url=origin)
    assert home.status_code == 200
    assert home.headers["Cache-Control"] == "private, no-store"

    for headers in ({}, {"Origin": "https://products.stoked.tech"}):
        denied = client.post("/projects/new", base_url=origin, headers=headers,
                             data={"name": "Unwanted project"})
        assert denied.status_code == 403

    created = client.post("/projects/new", base_url=origin, headers={"Origin": origin},
                          data={"name": "Sandbox project"})
    assert created.status_code == 302
    cookie = created.headers["Set-Cookie"]
    assert cookie.startswith("__Host-product-finder=")
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    projects = client.get("/projects", base_url=origin)
    assert b"Sandbox project" in projects.data
    assert b"Unwanted project" not in projects.data
