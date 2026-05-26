import sqlite3

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, User


def _columns(db_path, table):
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in cur.fetchall()}
    finally:
        conn.close()


def test_legacy_import_schema_bootstrap_adds_upgrade_columns(tmp_path, monkeypatch):
    db_path = tmp_path / "erp_database.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE clients ("
            "id INTEGER PRIMARY KEY, name TEXT, active INTEGER, excess_funds REAL)"
        )
        cur.execute(
            "CREATE TABLE purchase_orders ("
            "id INTEGER PRIMARY KEY, client_id INTEGER, po_no TEXT, adv_pct REAL, "
            "ret_pct REAL, ret_base TEXT, tds_enabled INTEGER, tds_rate REAL, "
            "tds_threshold REAL, advance_pool REAL)"
        )
        cur.execute(
            "CREATE TABLE po_baseline_items ("
            "id INTEGER PRIMARY KEY, po_id INTEGER, description TEXT, "
            "ordered_qty REAL, inspected_qty REAL, uom TEXT)"
        )
        cur.execute(
            "CREATE TABLE invoice_dispatch_items ("
            "id INTEGER PRIMARY KEY, invoice_id INTEGER, description TEXT, "
            "dispatched_qty REAL, uom TEXT, inspected_qty REAL)"
        )
        cur.execute(
            "CREATE TABLE system_settings ("
            "id INTEGER PRIMARY KEY, exchange_rate REAL, custom_columns TEXT)"
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.chdir(tmp_path)
    import migrate_sqlite

    migrate_sqlite.ensure_target_schema()

    assert {"display_currency", "exchange_rate"} <= _columns(db_path, "clients")
    assert {
        "contact_person",
        "project_name",
        "is_completed",
        "is_hidden",
        "completed_at",
        "tds_base",
    } <= _columns(db_path, "purchase_orders")
    assert {
        "material_type",
        "dispatch_alias",
        "dispatch_rate",
    } <= _columns(db_path, "po_baseline_items")
    assert "rate_per_uom" in _columns(db_path, "invoice_dispatch_items")
    assert {"fy_start_month", "fy_start_day"} <= _columns(db_path, "system_settings")


def test_target_truncate_participates_in_caller_rollback(tmp_path):
    import migrate_sqlite

    db_path = tmp_path / "target.sqlite"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    db = SessionLocal()
    try:
        db.add(User(username="existing", hashed_password="hash", role="admin"))
        db.commit()

        migrate_sqlite.truncate_target_tables(db)
        db.rollback()

        assert db.query(User).filter(User.username == "existing").count() == 1
    finally:
        db.close()
