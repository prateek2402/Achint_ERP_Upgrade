"""HTTP smoke tests against the ephemeral E2E uvicorn server (no Playwright browser)."""

import pytest
import requests


@pytest.mark.e2e
def test_live_server_delivers_shell_and_healthz(live_server: str):
    r = requests.get(f"{live_server}/", timeout=10)
    assert r.status_code == 200
    assert "Achint Chemicals" in r.text or "loginOverlay" in r.text

    h = requests.get(f"{live_server}/api/healthz", timeout=10)
    assert h.status_code == 200
    body = h.json()
    assert body.get("status") == "ok"
    assert body.get("db", {}).get("ok") is True


@pytest.mark.e2e
def test_login_api_accepts_admin_bootstrap_credentials(live_server: str):
    r = requests.post(
        f"{live_server}/api/login",
        json={"username": "admin", "password": "Admin@1234"},
        timeout=10,
    )
    assert r.status_code == 200
    data = r.json()
    assert "token" in data and data["token"]
    assert data.get("role") == "admin"


@pytest.mark.e2e
def test_login_api_rejects_invalid_password(live_server: str):
    r = requests.post(
        f"{live_server}/api/login",
        json={"username": "admin", "password": "not-the-password"},
        timeout=10,
    )
    assert r.status_code != 200
