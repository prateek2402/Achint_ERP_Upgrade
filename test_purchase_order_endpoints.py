import unittest

from fastapi.testclient import TestClient
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

        def override_get_db():
            db = self.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        def override_current_user():
            return User(id=1, username="admin", role="admin")

        main.app.dependency_overrides[main.get_db] = override_get_db
        main.app.dependency_overrides[main.get_current_user] = override_current_user
        self.api = TestClient(main.app)

    def tearDown(self):
        main.app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_update_purchase_order_status_does_not_crash(self):
        response = self.api.put(
            "/api/purchase-orders/PO%2F123/status",
            json={"is_completed": True, "is_hidden": False},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"success": True})

        with self.SessionLocal() as db:
            po = db.query(PurchaseOrder).filter(PurchaseOrder.po_no == "PO/123").one()
            self.assertTrue(po.is_completed)
            self.assertIsNotNone(po.completed_at)

    def test_delete_purchase_order_does_not_crash(self):
        response = self.api.delete("/api/purchase-orders/PO%2F123")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"success": True})

        with self.SessionLocal() as db:
            self.assertIsNone(db.query(PurchaseOrder).filter(PurchaseOrder.po_no == "PO/123").first())


if __name__ == "__main__":
    unittest.main()
