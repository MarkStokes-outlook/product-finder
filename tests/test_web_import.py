import io

import pytest

from product_finder import db
from product_finder.config import AppConfig
from product_finder.web.app import create_app

VALID_YAML = """
schema: product-finder/import/v1
project:
  name: Widgets
  create: true
items:
  - name: Blue Widget
    search_terms: [blue widget]
    max_price: 50
"""


@pytest.fixture
def cfg(tmp_path):
    return AppConfig(db_path=str(tmp_path / "test.db"))


@pytest.fixture
def client(cfg):
    app = create_app(cfg)
    app.config["TESTING"] = True
    return app.test_client()


def test_import_form_get(client):
    resp = client.get("/projects/import")
    assert resp.status_code == 200
    assert b"Paste JSON or YAML" in resp.data


def test_preview_shows_plan_without_writing(cfg, client):
    resp = client.post("/projects/import", data={"payload": VALID_YAML})
    assert resp.status_code == 200
    assert b"Blue Widget" in resp.data
    assert b"will create" in resp.data
    conn = db.connect(cfg.db_path)
    assert db.list_projects(conn) == []
    conn.close()


def test_preview_shows_errors_for_bad_document(client):
    resp = client.post("/projects/import", data={"payload": "items: []\n"})
    assert resp.status_code == 200
    assert b"Could not validate import" in resp.data


def test_commit_creates_project_and_items(cfg, client):
    resp = client.post(
        "/projects/import/commit", data={"raw_text": VALID_YAML}, follow_redirects=True
    )
    assert resp.status_code == 200
    assert b"created" in resp.data
    conn = db.connect(cfg.db_path)
    projects = db.list_projects(conn)
    assert len(projects) == 1
    assert projects[0]["name"] == "Widgets"
    conn.close()


def test_commit_dry_run_does_not_write(cfg, client):
    resp = client.post(
        "/projects/import/commit", data={"raw_text": VALID_YAML, "dry_run": "1"}
    )
    assert resp.status_code == 200
    assert b"Dry run" in resp.data
    conn = db.connect(cfg.db_path)
    assert db.list_projects(conn) == []
    conn.close()


def test_commit_revalidates_and_rejects_invalid_document(client):
    resp = client.post("/projects/import/commit", data={"raw_text": "items: []\n"})
    assert resp.status_code == 200
    assert b"could not be validated" in resp.data


def test_file_upload_is_used_over_pasted_text(cfg, client):
    data = {
        "payload": "items: []\n",  # would fail validation if used
        "file": (io.BytesIO(VALID_YAML.encode("utf-8")), "import.yaml"),
    }
    resp = client.post("/projects/import", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200
    assert b"Blue Widget" in resp.data
    assert b"will create" in resp.data


def test_export_downloads_yaml_and_reimports(cfg, client):
    client.post("/projects/import/commit", data={"raw_text": VALID_YAML})
    conn = db.connect(cfg.db_path)
    project_id = db.list_projects(conn)[0]["id"]
    conn.close()

    resp = client.get(f"/projects/{project_id}/export?format=yaml")
    assert resp.status_code == 200
    assert b"Blue Widget" in resp.data
    assert resp.headers["Content-Disposition"].endswith(".yaml\"")

    resp_json = client.get(f"/projects/{project_id}/export?format=json")
    assert resp_json.status_code == 200
    assert b'"name": "Blue Widget"' in resp_json.data

    # Re-importing the export updates the same item rather than duplicating it
    reimport = client.post(
        "/projects/import/commit", data={"raw_text": resp.data.decode("utf-8")}, follow_redirects=True
    )
    assert reimport.status_code == 200
    conn = db.connect(cfg.db_path)
    assert len(db.list_items(conn, project_id=project_id)) == 1
    conn.close()


def test_export_unknown_project_404s(client):
    resp = client.get("/projects/999/export")
    assert resp.status_code == 404
