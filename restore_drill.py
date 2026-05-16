"""Backup restore drill.

Validates that the most recent (or a chosen) database backup is actually
restorable AND that the restored copy can serve a few critical read paths.

Usage::

    # Latest backup, no auth, console + db_backups/drill_report_*.json:
    python restore_drill.py

    # Specific backup file:
    python restore_drill.py --backup db_backups/erp_database_20260502_055321.sqlite

    # Authenticated run (validates login + protected endpoints too):
    python restore_drill.py --user admin --password "Admin@1234"

    # Custom sandbox + report dir:
    python restore_drill.py --sandbox-dir tmp/restore --report-dir tmp/reports

Exit codes::

    0  drill PASSED (every required check ok)
    1  drill FAILED (at least one required check raised or returned non-OK)
    2  invalid invocation / no backup found

The drill never writes to the production DB. It copies the backup to a
sandbox path, points a fresh FastAPI ``TestClient`` at that copy via
``APP_DATABASE_URL`` + a SQLAlchemy override, and runs the checks there.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sqlite3
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Optional


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_BACKUP_DIR = REPO_ROOT / "db_backups"
DEFAULT_SANDBOX_DIR = REPO_ROOT / "db_backups" / "_drill_sandbox"
DEFAULT_REPORT_DIR = REPO_ROOT / "db_backups"


# ---------------------------------------------------------------------------
# Backup discovery + restore
# ---------------------------------------------------------------------------

def find_latest_backup(backup_dir: Path) -> Optional[Path]:
    if not backup_dir.exists():
        return None
    candidates = sorted(
        (p for p in backup_dir.glob("erp_database_*.sqlite") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def stage_sandbox_copy(backup_path: Path, sandbox_dir: Path) -> Path:
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d_%H%M%S")
    sandbox_db = sandbox_dir / f"restore_{ts}_{backup_path.name}"
    shutil.copy2(backup_path, sandbox_db)
    return sandbox_db


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

REQUIRED_TABLES = (
    "clients", "purchase_orders", "invoices", "payment_history",
    "payment_allocations", "users",
)


def check_sqlite_openable(db_path: Path) -> dict:
    """Open the restored file as a plain SQLite DB; integrity check + table list."""
    started = time.time()
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA integrity_check")
            integrity = (cur.fetchone() or [None])[0]

            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            present_tables = {row[0] for row in cur.fetchall()}
            missing = [t for t in REQUIRED_TABLES if t not in present_tables]

            counts: dict[str, int] = {}
            for tbl in REQUIRED_TABLES:
                if tbl in present_tables:
                    cur.execute(f"SELECT COUNT(*) FROM {tbl}")
                    counts[tbl] = int(cur.fetchone()[0])
        finally:
            conn.close()
    except Exception as exc:
        return {
            "name": "sqlite_open_and_integrity",
            "status": "fail",
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": int((time.time() - started) * 1000),
        }

    if integrity != "ok":
        return {
            "name": "sqlite_open_and_integrity",
            "status": "fail",
            "error": f"PRAGMA integrity_check returned: {integrity!r}",
            "elapsed_ms": int((time.time() - started) * 1000),
        }
    if missing:
        return {
            "name": "sqlite_open_and_integrity",
            "status": "fail",
            "error": f"Missing core tables: {missing}",
            "tables_present": sorted(present_tables),
            "row_counts": counts,
            "elapsed_ms": int((time.time() - started) * 1000),
        }
    return {
        "name": "sqlite_open_and_integrity",
        "status": "ok",
        "row_counts": counts,
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def _import_app_against_sandbox(sandbox_db: Path):
    """Set env, then import main + override SessionLocal/engine to point at sandbox."""
    os.environ["APP_DATABASE_URL"] = f"sqlite:///{sandbox_db.as_posix()}"
    for module_name in ("routers.audit", "routers.reconciliation", "routers.health", "main"):
        sys.modules.pop(module_name, None)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import main as app_module
    from models import Base

    sandbox_engine = create_engine(
        f"sqlite:///{sandbox_db.as_posix()}", connect_args={"check_same_thread": False}
    )
    SandboxSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=sandbox_engine)
    app_module.engine = sandbox_engine
    app_module.SessionLocal = SandboxSessionLocal
    app_module.DB_FILE_PATH = sandbox_db
    app_module._backup_thread_started = True

    # Defensive: make sure the sandbox has every model-known table.
    Base.metadata.create_all(bind=sandbox_engine)

    return app_module


def check_app_boots(sandbox_db: Path) -> tuple[dict, Optional[Any]]:
    started = time.time()
    try:
        app_module = _import_app_against_sandbox(sandbox_db)
        from fastapi.testclient import TestClient
        tc = TestClient(app_module.app)
        tc.__enter__()
    except Exception as exc:
        return ({
            "name": "fastapi_app_boots",
            "status": "fail",
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc(limit=4),
            "elapsed_ms": int((time.time() - started) * 1000),
        }, None)
    return ({
        "name": "fastapi_app_boots",
        "status": "ok",
        "elapsed_ms": int((time.time() - started) * 1000),
    }, tc)


def _expect_ok(name: str, fn) -> dict:
    started = time.time()
    try:
        resp = fn()
    except Exception as exc:
        return {
            "name": name,
            "status": "fail",
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": int((time.time() - started) * 1000),
        }
    elapsed = int((time.time() - started) * 1000)
    if resp.status_code != 200:
        return {
            "name": name,
            "status": "fail",
            "http_status": resp.status_code,
            "body": resp.text[:500],
            "elapsed_ms": elapsed,
        }
    try:
        payload = resp.json()
    except Exception:
        payload = None
    summary: dict[str, Any] = {"name": name, "status": "ok", "elapsed_ms": elapsed}
    if isinstance(payload, list):
        summary["items"] = len(payload)
    elif isinstance(payload, dict):
        keys = list(payload.keys())[:5]
        summary["payload_keys"] = keys
    return summary


def run_authenticated_checks(tc, user: str, password: str) -> list[dict]:
    results: list[dict] = []
    started = time.time()
    try:
        login_resp = tc.post("/api/login", json={"username": user, "password": password})
    except Exception as exc:
        results.append({
            "name": "login",
            "status": "fail",
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": int((time.time() - started) * 1000),
        })
        return results

    if login_resp.status_code != 200:
        results.append({
            "name": "login",
            "status": "fail",
            "http_status": login_resp.status_code,
            "body": login_resp.text[:500],
            "elapsed_ms": int((time.time() - started) * 1000),
        })
        return results

    token = login_resp.json().get("token")
    if not token:
        results.append({
            "name": "login",
            "status": "fail",
            "error": "no token in /api/login response",
            "elapsed_ms": int((time.time() - started) * 1000),
        })
        return results

    headers = {"Authorization": f"Bearer {token}"}
    results.append({
        "name": "login", "status": "ok",
        "user": user, "elapsed_ms": int((time.time() - started) * 1000),
    })
    results.append(_expect_ok("read_clients",
                              lambda: tc.get("/api/clients", headers=headers)))
    results.append(_expect_ok("read_purchase_orders",
                              lambda: tc.get("/api/purchase-orders", headers=headers)))
    results.append(_expect_ok("read_invoices",
                              lambda: tc.get("/api/invoices", headers=headers)))
    results.append(_expect_ok("read_payments",
                              lambda: tc.get("/api/payments", headers=headers)))
    results.append(_expect_ok("reconciliation_summary",
                              lambda: tc.get("/api/reconciliation/summary", headers=headers)))
    return results


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_drill(
    backup_path: Optional[Path],
    backup_dir: Path,
    sandbox_dir: Path,
    report_dir: Path,
    user: Optional[str],
    password: Optional[str],
) -> dict:
    overall_started = time.time()
    report: dict[str, Any] = {
        "started_at_utc": datetime.datetime.now(datetime.UTC).isoformat(),
        "checks": [],
    }

    if backup_path is None:
        backup_path = find_latest_backup(backup_dir)
    if backup_path is None or not backup_path.exists():
        report["status"] = "fail"
        report["error"] = f"no usable backup found (looked in {backup_dir})"
        report["elapsed_ms"] = int((time.time() - overall_started) * 1000)
        return report

    report["backup_path"] = str(backup_path)
    report["backup_size_bytes"] = backup_path.stat().st_size
    report["backup_mtime_utc"] = datetime.datetime.fromtimestamp(
        backup_path.stat().st_mtime, datetime.UTC
    ).isoformat()

    sandbox_db = stage_sandbox_copy(backup_path, sandbox_dir)
    report["sandbox_db"] = str(sandbox_db)

    sqlite_check = check_sqlite_openable(sandbox_db)
    report["checks"].append(sqlite_check)
    if sqlite_check["status"] != "ok":
        report["status"] = "fail"
        report["elapsed_ms"] = int((time.time() - overall_started) * 1000)
        return report

    app_check, tc = check_app_boots(sandbox_db)
    report["checks"].append(app_check)
    if app_check["status"] != "ok" or tc is None:
        report["status"] = "fail"
        report["elapsed_ms"] = int((time.time() - overall_started) * 1000)
        return report

    try:
        if user and password:
            report["checks"].extend(run_authenticated_checks(tc, user, password))
        else:
            report["checks"].append({
                "name": "auth_checks",
                "status": "skipped",
                "reason": "no --user/--password supplied; protected reads not validated",
            })
    finally:
        try:
            tc.__exit__(None, None, None)
        except Exception:
            pass

    failed = [c for c in report["checks"] if c.get("status") == "fail"]
    report["status"] = "fail" if failed else "ok"
    report["failed_check_count"] = len(failed)
    report["elapsed_ms"] = int((time.time() - overall_started) * 1000)

    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"drill_report_{datetime.datetime.now(datetime.UTC).strftime('%Y%m%d_%H%M%S')}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def _print_summary(report: dict) -> None:
    status = report.get("status", "?")
    badge = "PASS" if status == "ok" else ("FAIL" if status == "fail" else status.upper())
    print(f"\n=== Restore drill {badge} ===")
    print(f"Backup:       {report.get('backup_path', '(none)')}")
    if "backup_mtime_utc" in report:
        print(f"Backup mtime: {report['backup_mtime_utc']}")
    if "sandbox_db" in report:
        print(f"Sandbox DB:   {report['sandbox_db']}")
    if "report_path" in report:
        print(f"JSON report:  {report['report_path']}")
    print(f"Elapsed:      {report.get('elapsed_ms', 0)} ms")
    print("Checks:")
    for c in report.get("checks", []):
        line = f"  - {c.get('status', '?').upper():7} {c.get('name', '?')}"
        if "elapsed_ms" in c:
            line += f"  ({c['elapsed_ms']} ms)"
        if c.get("status") == "fail":
            err = c.get("error") or c.get("body") or c.get("http_status")
            if err is not None:
                line += f"  -- {err}"
        print(line)
    if status == "fail" and report.get("error"):
        print(f"Error: {report['error']}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate that a database backup is restorable + readable.",
    )
    parser.add_argument("--backup", type=Path, default=None,
                        help="Specific backup file to restore (default: latest in --backup-dir).")
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR,
                        help="Directory holding erp_database_*.sqlite backups.")
    parser.add_argument("--sandbox-dir", type=Path, default=DEFAULT_SANDBOX_DIR,
                        help="Directory to stage the restored copy in.")
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR,
                        help="Directory to write the JSON drill report into.")
    parser.add_argument("--user", default=os.getenv("DRILL_USER"),
                        help="Username for the auth + protected-endpoint checks (env: DRILL_USER).")
    parser.add_argument("--password", default=os.getenv("DRILL_PASSWORD"),
                        help="Password for --user (env: DRILL_PASSWORD).")
    args = parser.parse_args(argv)

    if args.backup is not None and not args.backup.exists():
        print(f"[ERROR] --backup path does not exist: {args.backup}", file=sys.stderr)
        return 2

    report = run_drill(
        backup_path=args.backup,
        backup_dir=args.backup_dir,
        sandbox_dir=args.sandbox_dir,
        report_dir=args.report_dir,
        user=args.user,
        password=args.password,
    )
    _print_summary(report)
    return 0 if report.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
