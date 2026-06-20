import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main as app_module
import migrate_sqlite as mig
from models import Base, Client, Invoice, User


def legacy_invoice(no: str, total: float = 100.0, po_no: str = "UNASSIGNED") -> dict:
    return {
        "id": no,
        "poNo": po_no,
        "invDate": "2026-01-01",
        "dueDate": "2026-02-01",
        "basic": total,
        "gst": 0.0,
        "total": total,
        "advance": 0.0,
        "tds": 0.0,
        "retention": 0.0,
        "netPayable": total,
        "paid": 0.0,
        "balance": total,
    }


def write_legacy_db(path: Path, app_data: dict, users: list[tuple] | None = None) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password TEXT, role TEXT)")
        conn.execute("CREATE TABLE erp_data (id INTEGER PRIMARY KEY, json_data TEXT)")
        for row in users or [(1, "legacyadmin", "pass", "admin")]:
            conn.execute("INSERT INTO users VALUES (?, ?, ?, ?)", row)
        conn.execute("INSERT INTO erp_data VALUES (1, ?)", (json.dumps(app_data),))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def import_env(tmp_path, monkeypatch):
    target_db = tmp_path / "erp.sqlite"
    legacy_db = tmp_path / "old.sqlite"
    marker_path = tmp_path / ".legacy_import_once.marker"
    status_path = tmp_path / "legacy_import_status.json"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_DATABASE_URL", f"sqlite:///{target_db.as_posix()}")
    monkeypatch.setenv("LEGACY_DB_PATH", str(legacy_db))
    monkeypatch.setattr(mig, "RUN_MARKER_PATH", marker_path)
    monkeypatch.setattr(mig, "STATUS_PATH", status_path)

    engine = create_engine(f"sqlite:///{target_db}", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    monkeypatch.setattr(app_module, "engine", engine)
    monkeypatch.setattr(app_module, "SessionLocal", SessionLocal)
    monkeypatch.setattr(app_module, "DB_FILE_PATH", target_db)

    return legacy_db, marker_path, SessionLocal


def test_startup_auto_import_skips_populated_target(import_env):
    legacy_db, marker_path, SessionLocal = import_env
    write_legacy_db(
        legacy_db,
        {
            "_settings": {"exchangeRate": 83.0, "customColumns": []},
            "LegacyClient": {
                "active": True,
                "excess": 0.0,
                "invoices": [legacy_invoice("LEGACY-INV-1")],
                "paymentHistory": [],
                "poTerms": {},
            },
        },
    )

    db = SessionLocal()
    db.add(User(username="localadmin", hashed_password=mig.hash_password("secret"), role="admin"))
    db.add(Client(name="LiveClient", active=True, excess_funds=0.0))
    db.commit()
    db.close()

    app_module._maybe_run_legacy_import()

    db = SessionLocal()
    try:
        names = sorted(row.name for row in db.query(Client).all())
        assert names == ["LiveClient"]
        assert db.query(User).filter(User.username == "localadmin").count() == 1
        assert marker_path.exists()
    finally:
        db.close()


def test_replace_import_rolls_back_target_data_when_import_fails(import_env):
    legacy_db, _, SessionLocal = import_env
    write_legacy_db(
        legacy_db,
        {
            "_settings": {"exchangeRate": 83.0, "customColumns": []},
            "LegacyA": {
                "active": True,
                "excess": 0.0,
                "invoices": [legacy_invoice("DUP-INV")],
                "paymentHistory": [],
                "poTerms": {},
            },
            "LegacyB": {
                "active": True,
                "excess": 0.0,
                "invoices": [legacy_invoice("DUP-INV")],
                "paymentHistory": [],
                "poTerms": {},
            },
        },
    )

    db = SessionLocal()
    live_client = Client(name="LiveClient", active=True, excess_funds=0.0)
    db.add(live_client)
    db.flush()
    db.add(Invoice(client_id=live_client.id, invoice_no="LIVE-INV", total=50.0, net_payable=50.0, balance=50.0))
    db.commit()
    db.close()

    with pytest.raises(Exception):
        mig.run_import(mode="replace", force=True, legacy_path=str(legacy_db))

    db = SessionLocal()
    try:
        assert db.query(Client).filter(Client.name == "LiveClient").count() == 1
        assert db.query(Invoice).filter(Invoice.invoice_no == "LIVE-INV").count() == 1
        assert db.query(Client).filter(Client.name == "LegacyA").count() == 0
    finally:
        db.close()


def test_merge_reimport_rolls_back_existing_client_delete_when_import_fails(import_env):
    legacy_db, _, SessionLocal = import_env
    write_legacy_db(
        legacy_db,
        {
            "_settings": {"exchangeRate": 83.0, "customColumns": []},
            "ClientA": {
                "active": True,
                "excess": 0.0,
                "invoices": [legacy_invoice("DUP-MERGE"), legacy_invoice("DUP-MERGE")],
                "paymentHistory": [],
                "poTerms": {},
            },
        },
    )

    db = SessionLocal()
    existing = Client(name="ClientA", active=True, excess_funds=0.0)
    db.add(existing)
    db.flush()
    db.add(Invoice(client_id=existing.id, invoice_no="OLD-INV", total=25.0, net_payable=25.0, balance=25.0))
    db.commit()
    db.close()

    with pytest.raises(Exception):
        mig.run_import(mode="merge", clients="ClientA", legacy_path=str(legacy_db))

    db = SessionLocal()
    try:
        assert db.query(Client).filter(Client.name == "ClientA").count() == 1
        assert db.query(Invoice).filter(Invoice.invoice_no == "OLD-INV").count() == 1
        assert db.query(Invoice).filter(Invoice.invoice_no == "DUP-MERGE").count() == 0
    finally:
        db.close()


def test_merge_import_users_preserves_local_accounts(import_env):
    legacy_db, _, SessionLocal = import_env
    write_legacy_db(
        legacy_db,
        {
            "_settings": {"exchangeRate": 83.0, "customColumns": []},
            "ClientA": {
                "active": True,
                "excess": 0.0,
                "invoices": [],
                "paymentHistory": [],
                "poTerms": {},
            },
        },
        users=[
            (1, "localadmin", "legacy-pass", "admin"),
            (2, "legacyadmin", "pass", "admin"),
        ],
    )

    db = SessionLocal()
    db.add(User(username="localadmin", hashed_password=mig.hash_password("secret"), role="admin"))
    db.commit()
    db.close()

    mig.run_import(mode="merge", clients="ClientA", import_users=True, legacy_path=str(legacy_db))

    db = SessionLocal()
    try:
        assert db.query(User).filter(User.username == "localadmin").count() == 1
        assert db.query(User).filter(User.username == "legacyadmin").count() == 1
    finally:
        db.close()
