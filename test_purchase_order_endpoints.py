import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import main
from models import Base, Client, PurchaseOrder, User


class PurchaseOrderEndpointTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        with self.SessionLocal() as db:
            client = Client(name="Acme")
            db.add(client)
            db.flush()
            db.add(PurchaseOrder(client_id=client.id, po_no="PO/123"))
            db.commit()
            self.client_id = client.id

        self.admin = User(id=1, username="admin", role="admin")

    def tearDown(self):
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_update_purchase_order_status_does_not_crash(self):
        with self.SessionLocal() as db:
            result = main.update_purchase_order_status(
                "PO/123",
                main.POStatusSchema(is_completed=True, is_hidden=False),
                current_user=self.admin,
                db=db,
            )
            self.assertEqual(result, {"success": True})

            po = db.query(PurchaseOrder).filter(PurchaseOrder.po_no == "PO/123").one()
            self.assertTrue(po.is_completed)
            self.assertIsNotNone(po.completed_at)

    def test_delete_purchase_order_does_not_crash(self):
        with self.SessionLocal() as db:
            result = main.delete_purchase_order("PO/123", current_user=self.admin, db=db)
            self.assertEqual(result, {"success": True})

            self.assertIsNone(db.query(PurchaseOrder).filter(PurchaseOrder.po_no == "PO/123").first())


if __name__ == "__main__":
    unittest.main()
