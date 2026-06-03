import json
import sqlite3
from pathlib import Path

import pytest

import main as app_module
import migrate_sqlite as mig
from models import Client, Invoice, PurchaseOrder, User


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


def _legacy_invoice(no: str, po_no: str = "UNASSIGNED", total: float = 100.0) -> dict:
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


def _session_for_target(tmp_path: Path, monkeypatch):
    target_db = tmp_path / "erp.sqlite"
    monkeypatch.setenv("APP_DATABASE_URL", f"sqlite:///{target_db.as_posix()}")
    monkeypatch.chdir(tmp_path)
    _, SessionLocal = mig.open_target_session()
    return SessionLocal


def test_startup_legacy_import_is_opt_in(tmp_path, monkeypatch):
    legacy_db = tmp_path / "old_erp.sqlite"
    legacy_db.write_bytes(b"placeholder")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LEGACY_DB_PATH", str(legacy_db))
    monkeypatch.delenv("LEGACY_AUTO_IMPORT", raising=False)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("startup import should not run without LEGACY_AUTO_IMPORT=1")

    monkeypatch.setattr(mig, "run_import", fail_if_called)

    app_module._maybe_run_legacy_import()


def test_failed_replace_import_rolls_back_existing_database(tmp_path, monkeypatch):
    SessionLocal = _session_for_target(tmp_path, monkeypatch)
    legacy_db = tmp_path / "old.sqlite"
    _write_legacy_db(
        legacy_db,
        {
            "_settings": {"exchangeRate": 83.0, "customColumns": []},
            "ClientA": {"active": True, "invoices": [], "paymentHistory": [], "poTerms": {"PO-DUP": {}}},
            "ClientB": {"active": True, "invoices": [], "paymentHistory": [], "poTerms": {"PO-DUP": {}}},
        },
    )

    db = SessionLocal()
    try:
        existing = Client(name="ExistingClient", active=True, excess_funds=0.0)
        db.add(User(username="localadmin", hashed_password=mig.hash_password("secret"), role="admin"))
        db.add(existing)
        db.flush()
        db.add(
            Invoice(
                client_id=existing.id,
                invoice_no="INV-EXISTING",
                basic=10.0,
                total=10.0,
                net_payable=10.0,
                balance=10.0,
            )
        )
        db.commit()
    finally:
        db.close()

    with pytest.raises(Exception):
        mig.run_import(mode="replace", legacy_path=str(legacy_db))

    db = SessionLocal()
    try:
        assert db.query(Client).filter(Client.name == "ExistingClient").count() == 1
        assert db.query(Invoice).filter(Invoice.invoice_no == "INV-EXISTING").count() == 1
        assert db.query(User).filter(User.username == "localadmin").count() == 1
    finally:
        db.close()


def test_failed_merge_reimport_rolls_back_deleted_client(tmp_path, monkeypatch):
    SessionLocal = _session_for_target(tmp_path, monkeypatch)
    legacy_db = tmp_path / "old.sqlite"
    _write_legacy_db(
        legacy_db,
        {
            "_settings": {"exchangeRate": 83.0, "customColumns": []},
            "ClientA": {
                "active": True,
                "invoices": [_legacy_invoice("INV-DUP"), _legacy_invoice("INV-DUP")],
                "paymentHistory": [],
                "poTerms": {},
            },
        },
    )

    db = SessionLocal()
    try:
        client = Client(name="ClientA", active=True, excess_funds=0.0)
        db.add(client)
        db.flush()
        db.add(
            Invoice(
                client_id=client.id,
                invoice_no="INV-A-OLD",
                basic=10.0,
                total=10.0,
                net_payable=10.0,
                balance=10.0,
            )
        )
        db.commit()
    finally:
        db.close()

    with pytest.raises(Exception):
        mig.run_import(mode="merge", clients="ClientA", legacy_path=str(legacy_db))

    db = SessionLocal()
    try:
        assert db.query(Client).filter(Client.name == "ClientA").count() == 1
        assert db.query(Invoice).filter(Invoice.invoice_no == "INV-A-OLD").count() == 1
        assert db.query(Invoice).filter(Invoice.invoice_no == "INV-DUP").count() == 0
    finally:
        db.close()


def test_merge_preflight_rejects_invoice_po_collision_before_delete(tmp_path, monkeypatch):
    SessionLocal = _session_for_target(tmp_path, monkeypatch)
    legacy_db = tmp_path / "old.sqlite"
    _write_legacy_db(
        legacy_db,
        {
            "_settings": {"exchangeRate": 83.0, "customColumns": []},
            "ClientA": {
                "active": True,
                "invoices": [_legacy_invoice("INV-A-NEW", po_no="PO-SHARED")],
                "paymentHistory": [],
                "poTerms": {},
            },
        },
    )

    db = SessionLocal()
    try:
        client_a = Client(name="ClientA", active=True, excess_funds=0.0)
        client_b = Client(name="ClientB", active=True, excess_funds=0.0)
        db.add_all([client_a, client_b])
        db.flush()
        db.add(PurchaseOrder(client_id=client_b.id, po_no="PO-SHARED"))
        db.add(
            Invoice(
                client_id=client_a.id,
                invoice_no="INV-A-OLD",
                basic=10.0,
                total=10.0,
                net_payable=10.0,
                balance=10.0,
            )
        )
        db.commit()
    finally:
        db.close()

    with pytest.raises(ValueError, match="PO number"):
        mig.run_import(mode="merge", clients="ClientA", legacy_path=str(legacy_db))

    db = SessionLocal()
    try:
        assert db.query(Client).filter(Client.name == "ClientA").count() == 1
        assert db.query(Invoice).filter(Invoice.invoice_no == "INV-A-OLD").count() == 1
        assert db.query(Invoice).filter(Invoice.invoice_no == "INV-A-NEW").count() == 0
    finally:
        db.close()
