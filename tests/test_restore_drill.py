"""End-to-end test for the backup restore drill.

Builds a small SQLite DB on disk that looks like a real backup, points the
drill at it, and asserts the report comes out clean. This guards two things:
  1. The drill script keeps running when models or endpoints evolve.
  2. The smoke checks remain meaningful (login + protected reads + recon).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, User
import main as app_module
import restore_drill


def _seed_backup_db(db_path: Path) -> None:
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionLocal()
    try:
        db.add(User(
            username="drill",
            hashed_password=app_module.hash_password("Drill@1234"),
            role="admin",
        ))
        db.commit()
    finally:
        db.close()


def test_restore_drill_passes_on_seeded_backup(tmp_path: Path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    backup_file = backup_dir / "erp_database_20260101_010101.sqlite"
    _seed_backup_db(backup_file)

    sandbox_dir = tmp_path / "sandbox"
    report_dir = tmp_path / "reports"

    report = restore_drill.run_drill(
        backup_path=None,  # auto-discover -> picks the seeded file
        backup_dir=backup_dir,
        sandbox_dir=sandbox_dir,
        report_dir=report_dir,
        user="drill",
        password="Drill@1234",
    )

    assert report["status"] == "ok", json.dumps(report, indent=2)
    names = {c["name"]: c for c in report["checks"]}
    for required in (
        "sqlite_open_and_integrity",
        "fastapi_app_boots",
        "login",
        "read_clients",
        "read_purchase_orders",
        "read_invoices",
        "read_payments",
        "reconciliation_summary",
    ):
        assert required in names, f"missing check {required}: {report}"
        assert names[required]["status"] == "ok", f"{required} -> {names[required]}"

    # Report file written and parseable.
    assert "report_path" in report
    on_disk = json.loads(Path(report["report_path"]).read_text(encoding="utf-8"))
    assert on_disk["status"] == "ok"


def test_restore_drill_skips_auth_when_no_credentials(tmp_path: Path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    backup_file = backup_dir / "erp_database_20260101_010101.sqlite"
    _seed_backup_db(backup_file)

    report = restore_drill.run_drill(
        backup_path=backup_file,
        backup_dir=backup_dir,
        sandbox_dir=tmp_path / "sandbox",
        report_dir=tmp_path / "reports",
        user=None,
        password=None,
    )
    assert report["status"] == "ok"
    auth = next((c for c in report["checks"] if c["name"] == "auth_checks"), None)
    assert auth is not None
    assert auth["status"] == "skipped"


def test_restore_drill_fails_when_no_backup_found(tmp_path: Path):
    empty_dir = tmp_path / "no_backups"
    empty_dir.mkdir()
    report = restore_drill.run_drill(
        backup_path=None,
        backup_dir=empty_dir,
        sandbox_dir=tmp_path / "sandbox",
        report_dir=tmp_path / "reports",
        user=None,
        password=None,
    )
    assert report["status"] == "fail"
    assert "no usable backup" in report.get("error", "")


def test_restore_drill_detects_corrupt_backup(tmp_path: Path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    bad = backup_dir / "erp_database_99999999_999999.sqlite"
    bad.write_bytes(b"this is not a sqlite database, it is just bytes")

    report = restore_drill.run_drill(
        backup_path=bad,
        backup_dir=backup_dir,
        sandbox_dir=tmp_path / "sandbox",
        report_dir=tmp_path / "reports",
        user=None,
        password=None,
    )
    assert report["status"] == "fail"
    sqlite_check = next((c for c in report["checks"] if c["name"] == "sqlite_open_and_integrity"), None)
    assert sqlite_check is not None
    assert sqlite_check["status"] == "fail"
