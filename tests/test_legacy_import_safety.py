import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main as app_module
import migrate_sqlite as mig
from models import Base, Client, Invoice, User


def _legacy_invoice(invoice_no: str, total: float = 100.0) -> dict:
    return {
        "id": invoice_no,
        "poNo": "UNASSIGNED",
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


def _write_legacy_db(path: Path, app_data: dict) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password TEXT, role TEXT)")
        conn.execute("CREATE TABLE erp_data (id INTEGER PRIMARY KEY, json_data TEXT)")
        conn.execute("INSERT INTO users VALUES (1, 'legacyadmin', 'pass', 'admin')")
        conn.execute("INSERT INTO erp_data VALUES (1, ?)", (json.dumps(app_data),))
        conn.commit()
    finally:
        conn.close()


def _seed_target(SessionLocal, client_name: str = "LiveClient", invoice_no: str = "LIVE-1") -> None:
    db = SessionLocal()
    try:
        db.add(User(username="localadmin", hashed_password=mig.hash_password("secret"), role="admin"))
        client = Client(name=client_name, active=True, excess_funds=0.0)
        db.add(client)
        db.flush()
        db.add(
            Invoice(
                client_id=client.id,
                invoice_no=invoice_no,
                basic=10.0,
                total=10.0,
                net_payable=10.0,
                balance=10.0,
            )
        )
        db.commit()
    finally:
        db.close()


def test_replace_import_failure_rolls_back_truncate(tmp_path, monkeypatch):
    target_db = tmp_path / "erp.sqlite"
    legacy_db = tmp_path / "old.sqlite"
    monkeypatch.setenv("APP_DATABASE_URL", f"sqlite:///{target_db.as_posix()}")
    monkeypatch.chdir(tmp_path)

    _write_legacy_db(
        legacy_db,
        {
            "_settings": {"exchangeRate": 83.0, "customColumns": []},
            "BadClientA": {"invoices": [_legacy_invoice("DUP-INV")], "paymentHistory": [], "poTerms": {}},
            "BadClientB": {"invoices": [_legacy_invoice("DUP-INV")], "paymentHistory": [], "poTerms": {}},
        },
    )
    _, SessionLocal = mig.open_target_session()
    _seed_target(SessionLocal)

    with pytest.raises(Exception):
        mig.run_import(mode="replace", legacy_path=str(legacy_db), force=True)

    db = SessionLocal()
    try:
        assert db.query(User).filter(User.username == "localadmin").count() == 1
        assert db.query(Client).filter(Client.name == "LiveClient").count() == 1
        assert db.query(Invoice).filter(Invoice.invoice_no == "LIVE-1").count() == 1
        assert db.query(Client).filter(Client.name.like("BadClient%")).count() == 0
    finally:
        db.close()


def test_merge_import_failure_rolls_back_existing_client_delete(tmp_path, monkeypatch):
    target_db = tmp_path / "erp.sqlite"
    legacy_db = tmp_path / "old.sqlite"
    monkeypatch.setenv("APP_DATABASE_URL", f"sqlite:///{target_db.as_posix()}")
    monkeypatch.chdir(tmp_path)

    _write_legacy_db(
        legacy_db,
        {
            "_settings": {"exchangeRate": 83.0, "customColumns": []},
            "ClientA": {
                "invoices": [_legacy_invoice("DUP-MERGE"), _legacy_invoice("DUP-MERGE")],
                "paymentHistory": [],
                "poTerms": {},
            },
        },
    )
    _, SessionLocal = mig.open_target_session()
    _seed_target(SessionLocal, client_name="ClientA", invoice_no="CLIENT-A-OLD")

    with pytest.raises(Exception):
        mig.run_import(mode="merge", clients="ClientA", legacy_path=str(legacy_db))

    db = SessionLocal()
    try:
        client = db.query(Client).filter(Client.name == "ClientA").one()
        assert db.query(Invoice).filter(Invoice.client_id == client.id, Invoice.invoice_no == "CLIENT-A-OLD").count() == 1
        assert db.query(Invoice).filter(Invoice.invoice_no == "DUP-MERGE").count() == 0
        assert db.query(User).filter(User.username == "localadmin").count() == 1
    finally:
        db.close()


def test_startup_legacy_auto_import_skips_populated_target(tmp_path, monkeypatch):
    target_db = tmp_path / "erp.sqlite"
    test_engine = create_engine(f"sqlite:///{target_db.as_posix()}", connect_args={"check_same_thread": False})
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    Base.metadata.create_all(bind=test_engine)
    _seed_target(TestingSessionLocal)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(app_module, "SessionLocal", TestingSessionLocal)
    (tmp_path / "old_erp.sqlite").write_bytes(b"placeholder")

    called = False

    def fake_run_import(**kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(mig, "run_import", fake_run_import)

    app_module._maybe_run_legacy_import()

    assert called is False
