"""Admin auth, public reports, and gated /load."""
from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    reports_file = tmp_path / "reports.json"
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("REPORTS_FILE", str(reports_file))
    monkeypatch.setenv("SERVE_FRONTEND", "false")

    import admin_auth
    import config
    import reports

    monkeypatch.setattr(config, "ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setattr(config, "REPORTS_FILE", str(reports_file))
    monkeypatch.setattr(admin_auth, "ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setattr(reports, "REPORTS_FILE", str(reports_file))

    import app as backend_app

    return TestClient(backend_app.app)


def test_expected_admin_and_report_routes():
    import app as backend_app

    paths = {route.path for route in backend_app.app.routes}
    assert {"/reports", "/admin/status", "/admin/reports", "/admin", "/load"}.issubset(paths)


def test_public_report_create_and_admin_list(client):
    res = client.post(
        "/reports",
        json={
            "kind": "incorrect_location",
            "details": "Map pin is two blocks west of the real site.",
            "application_id": "DOS2022-E016",
            "location": "Corkscrew Road",
            "current_value": "123 Fake St",
            "suggested_value": "456 Real Ave",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["kind"] == "incorrect_location"
    assert body["status"] == "open"
    assert body["id"]

    denied = client.get("/admin/reports")
    assert denied.status_code == 401

    listed = client.get(
        "/admin/reports",
        headers={"Authorization": "Bearer test-admin-key"},
    )
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["application_id"] == "DOS2022-E016"


def test_admin_status_requires_key(client):
    assert client.get("/admin/status").status_code == 401
    ok = client.get("/admin/status", headers={"Authorization": "Bearer test-admin-key"})
    assert ok.status_code == 200
    data = ok.json()
    assert data["admin_configured"] is True
    assert "reports" in data


def test_load_requires_admin(client):
    files = {"file": ("upload.csv", "a,b\n1,2\n", "text/csv")}
    assert client.post("/load", files=files).status_code == 401


def test_update_report_status(client):
    created = client.post(
        "/reports",
        json={"kind": "suggest_change", "details": "Status should be Continued, not Approved."},
    ).json()
    report_id = created["id"]
    patched = client.patch(
        f"/admin/reports/{report_id}",
        headers={"Authorization": "Bearer test-admin-key"},
        json={"status": "resolved", "admin_note": "Fixed in silver layer"},
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "resolved"
    assert patched.json()["admin_note"] == "Fixed in silver layer"
