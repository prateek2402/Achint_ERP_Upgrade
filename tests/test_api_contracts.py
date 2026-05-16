import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main as app_module
from models import Base, User


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    db_file = tmp_path / "contracts.sqlite"
    test_engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    Base.metadata.create_all(bind=test_engine)

    monkeypatch.setattr(app_module, "engine", test_engine)
    monkeypatch.setattr(app_module, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(app_module, "DB_FILE_PATH", Path(db_file))
    monkeypatch.setattr(app_module, "_backup_thread_started", True)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app_module.app.dependency_overrides[app_module.get_db] = override_get_db

    db = TestingSessionLocal()
    db.add(User(username="admin", hashed_password=app_module.hash_password("Admin@1234"), role="admin"))
    db.commit()
    db.close()

    with TestClient(app_module.app) as tc:
        yield tc
    app_module.app.dependency_overrides.clear()


def _login(api_client: TestClient) -> str:
    res = api_client.post("/api/login", json={"username": "admin", "password": "Admin@1234"})
    assert res.status_code == 200, res.text
    return res.json()["token"]


def _h(token: str):
    return {"Authorization": f"Bearer {token}"}


def test_contract_clients_purchase_orders_invoices_payments(api_client: TestClient):
    token = _login(api_client)

    c = api_client.post("/api/clients", json={"name": "ContractCo"}, headers=_h(token))
    assert c.status_code == 200, c.text
    c_json = c.json()
    assert {"success", "id", "name"}.issubset(c_json.keys())
    client_id = c_json["id"]

    po = api_client.post(
        "/api/purchase-orders",
        json={
            "client_id": client_id,
            "po_no": "PO-C-1",
            "contact_person": "QA",
            "project_name": "Contract Validation",
            "adv_pct": 0.0,
            "ret_pct": 0.0,
            "ret_base": "total",
            "tds_enabled": False,
            "tds_rate": 0.0,
            "tds_threshold": 0.0,
            "baseline_items": [{"description": "Item A", "ordered_qty": 1, "inspected_qty": 0, "uom": "Nos", "material_type": "brick", "dispatch_alias": "ITEM-A-ALT"}],
        },
        headers=_h(token),
    )
    assert po.status_code == 200, po.text
    po_list = api_client.get("/api/purchase-orders", headers=_h(token))
    assert po_list.status_code == 200
    po_obj = next(p for p in po_list.json() if p["po_no"] == "PO-C-1")
    assert {"id", "client_id", "po_no", "adv_pct", "ret_pct", "baseline_items"}.issubset(po_obj.keys())
    assert po_obj["baseline_items"][0].get("dispatch_alias") == "ITEM-A-ALT"

    inv = api_client.post(
        "/api/invoices",
        json={
            "client_id": client_id,
            "po_no": "PO-C-1",
            "invoice_no": "INV-C-1",
            "sub_entity": "S1",
            "lr_no": "LR-C1",
            "inv_date": "2026-04-01",
            "due_date": "2026-04-10",
            "basic": 100,
            "gst": 18,
            "total": 118,
            "advance_adj": 0,
            "tds_ded": 0,
            "retention_held": 0,
            "net_payable": 0,
            "paid": 0,
            "balance": 0,
            "is_note": False,
            "note_type": None,
            "note_reason": None,
            "dispatch_items": [{"description": "Item A", "qty": 1, "inspected_qty": 0, "uom": "Nos"}],
        },
        headers=_h(token),
    )
    assert inv.status_code == 200, inv.text

    invs = api_client.get("/api/invoices", headers=_h(token))
    assert invs.status_code == 200
    inv_obj = next(i for i in invs.json() if i["id"] == "INV-C-1")
    assert {"id", "client_id", "poNo", "basic", "gst", "total", "paid", "balance", "dispatchItems"}.issubset(inv_obj.keys())

    alloc = api_client.post(
        "/api/payments/allocate",
        json={
            "client_id": client_id,
            "id": "PAY-C-1",
            "date": datetime.date.today().isoformat(),
            "amount": 100.0,
            "note": "contract payment",
            "mode": "targeted",
            "targets": [{"inv_id": "INV-C-1", "amount": 100.0}],
            "hold_ret": False,
            "hold_gst": False,
            "only_gst": False,
            "apply_adv": False,
            "advance_only": False,
            "fund_source": "receipt",
            "move_to_po": None,
            "po_no": None,
            "clear_po_pool": False,
            "excess_action": "park",
        },
        headers=_h(token),
    )
    assert alloc.status_code == 200, alloc.text

    pays = api_client.get("/api/payments", headers=_h(token))
    assert pays.status_code == 200
    assert isinstance(pays.json(), list)
    assert any({"id", "client_id", "type", "amount"}.issubset(p.keys()) for p in pays.json())
