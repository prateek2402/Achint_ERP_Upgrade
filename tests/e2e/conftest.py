import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def live_server(tmp_path_factory):
    root = Path(__file__).resolve().parents[2]
    db_file = tmp_path_factory.mktemp("e2e-db") / "e2e.sqlite"
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["APP_DATABASE_URL"] = f"sqlite:///{db_file.as_posix()}"
    env["BOOTSTRAP_ADMIN_USERNAME"] = "admin"
    env["BOOTSTRAP_ADMIN_PASSWORD"] = "Admin@1234"
    env["DB_BACKUP_INTERVAL_SECONDS"] = "99999999"

    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    proc = subprocess.Popen(cmd, cwd=str(root), env=env)

    deadline = time.time() + 60
    last_err = None
    while time.time() < deadline:
        try:
            r = requests.get(f"{base_url}/", timeout=2)
            if r.status_code == 200:
                break
        except Exception as exc:
            last_err = exc
        time.sleep(0.5)
    else:
        proc.terminate()
        raise RuntimeError(f"E2E server failed to start: {last_err}")

    yield base_url

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def e2e_extra_users(live_server):
    """Create logistics + standard users on the ephemeral E2E database (admin exists from bootstrap)."""
    login = requests.post(
        f"{live_server}/api/login",
        json={"username": "admin", "password": "Admin@1234"},
        timeout=10,
    )
    assert login.status_code == 200, login.text
    token = login.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    specs = [
        ("e2e_logi", "Logi@12345", "logistics"),
        ("e2e_user", "User@12345", "user"),
    ]
    for uname, pw, role in specs:
        r = requests.post(
            f"{live_server}/api/users",
            json={"username": uname, "password": pw, "role": role},
            headers=headers,
            timeout=10,
        )
        assert r.status_code in (200, 400), r.text


@pytest.fixture(scope="session")
def e2e_api_admin_headers(live_server):
    login = requests.post(
        f"{live_server}/api/login",
        json={"username": "admin", "password": "Admin@1234"},
        timeout=10,
    )
    assert login.status_code == 200
    tok = login.json()["token"]
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

