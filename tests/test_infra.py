"""Tier 1 infra tests: healthz / metrics / request_id / backup rotation / lifespan.

These tests guard the small but high-leverage additions made by Workstream
"Tier 1 - quick wins". They share the same TestClient fixture from
test_regression_api so they exercise the actual app, not a stub.
"""

from __future__ import annotations

import datetime
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_regression_api import (  # noqa: F401  (fixture re-export)
    client,
    login,
    auth_header,
)
import main as app_module


# ---------------------------------------------------------------------------
# /api/healthz + /api/metrics
# ---------------------------------------------------------------------------

def test_healthz_returns_200_and_db_ok(client: TestClient):
    r = client.get("/api/healthz")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["db"]["ok"] is True
    assert body["db"]["error"] is None
    assert isinstance(body["uptime_seconds"], int)
    assert "now_utc" in body
    assert "latest_backup" in body
    # Should not require auth.
    assert "X-Request-Id" in r.headers


def test_metrics_returns_counts(client: TestClient):
    r = client.get("/api/metrics")
    assert r.status_code == 200
    body = r.json()
    assert "counts" in body
    counts = body["counts"]
    for required in ("clients", "purchase_orders", "invoices", "payments", "users"):
        assert required in counts
        assert isinstance(counts[required], int)
        assert counts[required] >= 0
    # Users includes the seeded admin / user1 / logi.
    assert counts["users"] >= 3


# ---------------------------------------------------------------------------
# Request id middleware
# ---------------------------------------------------------------------------

def test_request_id_is_generated_when_absent(client: TestClient):
    r = client.get("/api/healthz")
    rid = r.headers.get("X-Request-Id")
    assert rid and len(rid) >= 8


def test_request_id_is_echoed_when_caller_provides_one(client: TestClient):
    r = client.get("/api/healthz", headers={"X-Request-Id": "test-rid-1234"})
    assert r.headers.get("X-Request-Id") == "test-rid-1234"


def test_request_id_overlong_caller_value_is_replaced(client: TestClient):
    long = "x" * 200
    r = client.get("/api/healthz", headers={"X-Request-Id": long})
    rid = r.headers.get("X-Request-Id")
    assert rid != long
    assert rid and len(rid) <= 64


# ---------------------------------------------------------------------------
# Backup rotation
# ---------------------------------------------------------------------------

def test_prune_old_backups_removes_expired(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "BACKUP_DIR", tmp_path)
    keep_days = 7
    cutoff = time.time() - (keep_days + 1) * 86400

    fresh = tmp_path / "erp_database_fresh.sqlite"
    old = tmp_path / "erp_database_old.sqlite"
    foreign = tmp_path / "manual_dump.sqlite"  # different name pattern, should be left alone
    fresh.write_bytes(b"fresh")
    old.write_bytes(b"old")
    foreign.write_bytes(b"foreign")

    import os as _os
    _os.utime(old, (cutoff, cutoff))
    _os.utime(foreign, (cutoff, cutoff))

    removed = app_module.prune_old_backups(keep_days)
    assert removed == 1
    assert fresh.exists()
    assert not old.exists()
    assert foreign.exists(), "rotation must only touch erp_database_*.sqlite"


def test_prune_disabled_when_keep_days_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "BACKUP_DIR", tmp_path)
    f = tmp_path / "erp_database_old.sqlite"
    f.write_bytes(b"old")
    import os as _os
    _os.utime(f, (0, 0))
    assert app_module.prune_old_backups(0) == 0
    assert f.exists()


def test_database_backup_uses_fresh_backup_mtime(tmp_path, monkeypatch):
    db_file = tmp_path / "erp_database.sqlite"
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO marker (value) VALUES ('ok')")
        conn.commit()
    finally:
        conn.close()

    old = time.time() - (31 * 86400)
    import os as _os
    _os.utime(db_file, (old, old))

    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(app_module, "DB_FILE_PATH", db_file)
    monkeypatch.setattr(app_module, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(app_module, "KEEP_BACKUPS_DAYS", 30)

    app_module.perform_database_backup()

    backups = list(backup_dir.glob("erp_database_*.sqlite"))
    assert len(backups) == 1
    assert backups[0].stat().st_mtime > time.time() - 60
    copied = sqlite3.connect(str(backups[0]))
    try:
        assert copied.execute("SELECT value FROM marker").fetchone()[0] == "ok"
    finally:
        copied.close()


def test_schema_bootstrap_adds_dispatch_inspected_columns(tmp_path, monkeypatch):
    db_file = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute("CREATE TABLE clients (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE purchase_orders (id INTEGER PRIMARY KEY, po_no TEXT)")
        conn.execute("CREATE TABLE po_baseline_items (id INTEGER PRIMARY KEY, dispatch_rate REAL)")
        conn.execute("CREATE TABLE invoice_dispatch_items (id INTEGER PRIMARY KEY, rate_per_uom REAL)")
        conn.execute("CREATE TABLE system_settings (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(app_module, "DB_FILE_PATH", db_file)
    app_module.ensure_schema_columns()

    conn = sqlite3.connect(str(db_file))
    try:
        baseline_cols = {row[1] for row in conn.execute("PRAGMA table_info(po_baseline_items)").fetchall()}
        dispatch_cols = {row[1] for row in conn.execute("PRAGMA table_info(invoice_dispatch_items)").fetchall()}
    finally:
        conn.close()
    assert "inspected_qty" in baseline_cols
    assert "inspected_qty" in dispatch_cols


# ---------------------------------------------------------------------------
# Lifespan smoke (no on_event reliance) + structured logging entry-point
# ---------------------------------------------------------------------------

def test_lifespan_runs_schema_bootstrap_when_app_starts(client: TestClient):
    # If the lifespan handler ran, ensure_schema_columns must have been invoked
    # and the audit_log table will exist (created defensively there).
    import sqlite3
    conn = sqlite3.connect(str(app_module.DB_FILE_PATH))
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in cur.fetchall()}
    finally:
        conn.close()
    assert "audit_log" in tables
    assert "uploaded_documents" in tables


def test_login_exposes_expiry_fields(client: TestClient):
    """The SPA caches credentials in localStorage and expires them at exactly
    the same instant the backend stops accepting the JWT. /api/login must
    therefore advertise the absolute expiry timestamp + a seconds delta.
    """
    resp = client.post("/api/login", json={"username": "admin", "password": "Admin@1234"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "token" in body and body["token"]
    assert "expires_at" in body and body["expires_at"]
    assert "expires_in_seconds" in body and isinstance(body["expires_in_seconds"], int)
    # Default JWT_EXPIRY_HOURS=24 -> 86_400 seconds.
    assert body["expires_in_seconds"] == app_module.JWT_EXPIRY_HOURS * 3600
    # expires_at parses as a future ISO timestamp.
    parsed = datetime.datetime.fromisoformat(body["expires_at"])
    delta = (parsed - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    assert delta > 0
    assert delta <= app_module.JWT_EXPIRY_HOURS * 3600 + 5  # allow a tiny slop


def test_app_logging_module_emits_request_id(caplog):
    from app_logging import REQUEST_ID, configure_logging, get_logger
    configure_logging()
    logger = get_logger("test.req")
    token = REQUEST_ID.set("abcdef123456")
    try:
        with caplog.at_level("INFO", logger="test.req"):
            logger.info("hello world")
    finally:
        REQUEST_ID.reset(token)
    assert any(rec.message == "hello world" for rec in caplog.records)
    rec = next(r for r in caplog.records if r.message == "hello world")
    assert getattr(rec, "request_id", None) == "abcdef123456"
