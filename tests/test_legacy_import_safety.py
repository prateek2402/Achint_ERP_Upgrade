import json
import sqlite3
from pathlib import Path

import pytest

import main as app_module


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


def _legacy_invoice(no: str, total: float = 100.0, advance: float = 0.0, tds: float = 0.0) -> dict:
    net = max(0.0, total - advance - tds)
    return {
        "id": no,
        "poNo": "UNASSIGNED",
        "invDate": "2026-01-01",
        "dueDate": "2026-02-01",
        "basic": total,
        "gst": 0.0,
        "total": total,
        "advance": advance,
        "tds": tds,
        "retention": 0.0,
        "netPayable": net,
        "paid": 0.0,
        "balance": net,
    }


def _configure_import(tmp_path, monkeypatch, target_name: str = "erp.sqlite"):
    import migrate_sqlite as mig

    target_db = tmp_path / target_name
    monkeypatch.setenv("APP_DATABASE_URL", f"sqlite:///{target_db.as_posix()}")
    monkeypatch.chdir(tmp_path)
    return mig


def test_startup_legacy_import_requires_explicit_opt_in(tmp_path, monkeypatch):
    import migrate_sqlite as mig

    legacy_db = tmp_path / "old_erp.sqlite"
    legacy_db.write_bytes(b"present")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LEGACY_DB_PATH", str(legacy_db))
    monkeypatch.delenv("LEGACY_AUTO_IMPORT", raising=False)

    calls = []
    monkeypatch.setattr(mig, "run_import", lambda **kwargs: calls.append(kwargs))

    app_module._maybe_run_legacy_import()

    assert calls == []


def test_replace_import_refuses_populated_target_without_force(tmp_path, monkeypatch):
    mig = _configure_import(tmp_path, monkeypatch)
    from models import Client, Invoice

    legacy_db = tmp_path / "old.sqlite"
    _write_legacy_db(
        legacy_db,
        {
            "_settings": {"exchangeRate": 83.0, "customColumns": []},
            "Imported": {"active": True, "invoices": [_legacy_invoice("IMP-1")], "paymentHistory": [], "poTerms": {}},
        },
    )

    _, SessionLocal = mig.open_target_session()
    db = SessionLocal()
    try:
        existing = Client(name="Existing", active=True, excess_funds=0.0)
        db.add(existing)
        db.flush()
        db.add(Invoice(client_id=existing.id, invoice_no="KEEP-1", total=100.0, net_payable=100.0, balance=100.0))
        db.commit()
    finally:
        db.close()

    with pytest.raises(RuntimeError, match="not empty"):
        mig.run_import(mode="replace", legacy_path=str(legacy_db))

    db = SessionLocal()
    try:
        assert db.query(Client).filter(Client.name == "Existing").count() == 1
        assert db.query(Invoice).filter(Invoice.invoice_no == "KEEP-1").count() == 1
        assert db.query(Client).filter(Client.name == "Imported").count() == 0
    finally:
        db.close()


def test_replace_import_rolls_back_target_when_import_fails_after_truncate(tmp_path, monkeypatch):
    mig = _configure_import(tmp_path, monkeypatch)
    from models import Client, Invoice

    legacy_db = tmp_path / "old.sqlite"
    _write_legacy_db(
        legacy_db,
        {
            "_settings": {"exchangeRate": 83.0, "customColumns": []},
            "ClientA": {"active": True, "invoices": [_legacy_invoice("DUP-1")], "paymentHistory": [], "poTerms": {}},
            "ClientB": {"active": True, "invoices": [_legacy_invoice("DUP-1")], "paymentHistory": [], "poTerms": {}},
        },
    )

    _, SessionLocal = mig.open_target_session()
    db = SessionLocal()
    try:
        existing = Client(name="Existing", active=True, excess_funds=0.0)
        db.add(existing)
        db.flush()
        db.add(Invoice(client_id=existing.id, invoice_no="KEEP-1", total=100.0, net_payable=100.0, balance=100.0))
        db.commit()
    finally:
        db.close()

    with pytest.raises(Exception):
        mig.run_import(mode="replace", legacy_path=str(legacy_db), force=True)

    db = SessionLocal()
    try:
        assert db.query(Client).filter(Client.name == "Existing").count() == 1
        assert db.query(Invoice).filter(Invoice.invoice_no == "KEEP-1").count() == 1
        assert db.query(Client).filter(Client.name.in_(["ClientA", "ClientB"])).count() == 0
    finally:
        db.close()


def test_legacy_import_recalculation_preserves_tds_in_open_balance(tmp_path, monkeypatch):
    mig = _configure_import(tmp_path, monkeypatch)
    from models import Invoice

    legacy_db = tmp_path / "old.sqlite"
    _write_legacy_db(
        legacy_db,
        {
            "_settings": {"exchangeRate": 83.0, "customColumns": []},
            "TDSClient": {
                "active": True,
                "invoices": [_legacy_invoice("TDS-1", total=1000.0, advance=100.0, tds=50.0)],
                "paymentHistory": [],
                "poTerms": {},
            },
        },
    )

    mig.run_import(mode="replace", legacy_path=str(legacy_db))

    _, SessionLocal = mig.open_target_session()
    db = SessionLocal()
    try:
        inv = db.query(Invoice).filter(Invoice.invoice_no == "TDS-1").one()
        assert inv.tds_ded == pytest.approx(50.0)
        assert inv.net_payable == pytest.approx(850.0)
        assert inv.balance == pytest.approx(850.0)
    finally:
        db.close()
