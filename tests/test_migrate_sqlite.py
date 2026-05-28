import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import migrate_sqlite
from models import Base, Client, SystemSettings, UnallocatedPaymentRegister, UploadedDocument, User


def test_truncate_target_tables_is_rollback_safe_and_clears_registers(tmp_path):
    db_file = tmp_path / "target.sqlite"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    db = SessionLocal()
    try:
        client = Client(name="Legacy Client")
        db.add(client)
        db.flush()
        db.add(User(username="legacy", hashed_password="x", role="user"))
        db.add(SystemSettings(exchange_rate=83.0, custom_columns="[]"))
        db.add(UnallocatedPaymentRegister(client_id=client.id, amount=100.0, balance=100.0, status="open"))
        db.add(UploadedDocument(
            sha256="abc",
            kind="invoice",
            uploaded_at=datetime.datetime(2026, 4, 1, tzinfo=datetime.timezone.utc),
            status="extracted",
        ))
        db.commit()

        migrate_sqlite.truncate_target_tables(db)
        assert db.query(Client).count() == 0
        assert db.query(UnallocatedPaymentRegister).count() == 0
        assert db.query(UploadedDocument).count() == 0

        db.rollback()
        assert db.query(Client).count() == 1
        assert db.query(UnallocatedPaymentRegister).count() == 1
        assert db.query(UploadedDocument).count() == 1
    finally:
        db.close()
