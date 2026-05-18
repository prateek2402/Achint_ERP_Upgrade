import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main as app_module
from models import Base, User


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test_erp.sqlite"
    test_engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    Base.metadata.create_all(bind=test_engine)

    monkeypatch.setattr(app_module, "engine", test_engine)
    monkeypatch.setattr(app_module, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(app_module, "DB_FILE_PATH", Path(db_file))
    # Prevent background backup thread spawning during tests.
    monkeypatch.setattr(app_module, "_backup_thread_started", True)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app_module.app.dependency_overrides[app_module.get_db] = override_get_db

    # Seed users for auth/permission checks.
    db = TestingSessionLocal()
    db.add(User(username="admin", hashed_password=app_module.hash_password("Admin@1234"), role="admin"))
    db.add(User(username="user1", hashed_password=app_module.hash_password("User@12345"), role="user"))
    db.add(User(username="logi", hashed_password=app_module.hash_password("Logi@12345"), role="logistics"))
    db.commit()
    db.close()

    with TestClient(app_module.app) as tc:
        yield tc

    app_module.app.dependency_overrides.clear()


def login(client: TestClient, username: str, password: str) -> str:
    resp = client.post("/api/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def create_client_po_invoice(client: TestClient, token: str):
    c = client.post("/api/clients", json={"name": "ACME"}, headers=auth_header(token))
    assert c.status_code == 200, c.text
    client_id = c.json()["id"]

    po_payload = {
        "client_id": client_id,
        "po_no": "PO-001",
        "contact_person": "Ops",
        "project_name": "Kiln",
        "adv_pct": 5.0,
        "ret_pct": 2.0,
        "ret_base": "basic",
        "tds_enabled": True,
        "tds_rate": 0.1,
        "tds_threshold": 5000.0,
        "baseline_items": [
            {"description": "Brick A", "ordered_qty": 100, "inspected_qty": 0, "uom": "Nos", "material_type": "brick"},
            {"description": "Castable B", "ordered_qty": 10, "inspected_qty": 0, "uom": "Bags", "material_type": "castable_mortar"},
        ],
    }
    po = client.post("/api/purchase-orders", json=po_payload, headers=auth_header(token))
    assert po.status_code == 200, po.text

    inv_payload = {
        "client_id": client_id,
        "po_no": "PO-001",
        "invoice_no": "INV-001",
        "sub_entity": "Unit-1",
        "lr_no": "LR-11",
        "inv_date": "2026-04-01",
        "due_date": "2026-04-15",
        "basic": 1000.49,
        "gst": 180.09,
        "total": 1180.51,
        "advance_adj": 0.0,
        "tds_ded": 0.0,
        "retention_held": 0.0,
        "net_payable": 0.0,
        "paid": 1176.31,
        "balance": 4.2,
        "is_note": False,
        "note_type": None,
        "note_reason": None,
        "dispatch_items": [
            {"description": "Brick A", "qty": 5, "inspected_qty": 2, "uom": "Nos"},
            {"description": "Castable B", "qty": 3, "inspected_qty": 0, "uom": "Bags"},
        ],
    }
    inv = client.post("/api/invoices", json=inv_payload, headers=auth_header(token))
    assert inv.status_code == 200, inv.text
    return client_id


def create_simple_po_invoice(client: TestClient, token: str, po_no: str, invoice_no: str, adv_pct: float = 20.0):
    c = client.post("/api/clients", json={"name": f"CLIENT-{invoice_no}"}, headers=auth_header(token))
    assert c.status_code == 200, c.text
    client_id = c.json()["id"]

    po = client.post(
        "/api/purchase-orders",
        json={
            "client_id": client_id,
            "po_no": po_no,
            "contact_person": "Ops",
            "project_name": "Advance cap",
            "adv_pct": adv_pct,
            "ret_pct": 0.0,
            "ret_base": "total",
            "tds_base": "basic",
            "tds_enabled": False,
            "tds_rate": 0.0,
            "tds_threshold": 0.0,
            "baseline_items": [],
        },
        headers=auth_header(token),
    )
    assert po.status_code == 200, po.text

    inv = client.post(
        "/api/invoices",
        json={
            "client_id": client_id,
            "po_no": po_no,
            "invoice_no": invoice_no,
            "sub_entity": "",
            "lr_no": "",
            "inv_date": "2026-04-01",
            "due_date": None,
            "basic": 1000.0,
            "gst": 0.0,
            "total": 1000.0,
            "advance_adj": 0.0,
            "tds_ded": 0.0,
            "retention_held": 0.0,
            "net_payable": 0.0,
            "paid": 0.0,
            "balance": 0.0,
            "is_note": False,
            "note_type": None,
            "note_reason": None,
            "dispatch_items": [],
        },
        headers=auth_header(token),
    )
    assert inv.status_code == 200, inv.text
    return client_id


def test_invoice_ledger_sort_key_orders_by_date_fy_then_sequence():
    import datetime

    rows = [
        (datetime.date(2026, 4, 1), "RS/25-26/12"),
        (datetime.date(2026, 4, 1), "RS/25-26/2"),
        (datetime.date(2026, 4, 1), "RS/26-27/1"),
        (datetime.date(2026, 3, 1), "RS/25-26/99"),
    ]
    ordered = sorted(rows, key=lambda r: app_module.invoice_ledger_sort_key(r[0], r[1]))
    assert [r[1] for r in ordered] == ["RS/25-26/99", "RS/25-26/2", "RS/25-26/12", "RS/26-27/1"]
    assert app_module.invoice_no_ledger_sort_parts("RS/25-26/108")[:3] == (25, 26, 108)


def test_net_payable_balance_ignore_retention_row(client: TestClient):
    """Retention is stored for display; it must not reduce net payable or balance."""
    token = login(client, "admin", "Admin@1234")
    cr = client.post("/api/clients", json={"name": "RETCLIENT"}, headers=auth_header(token))
    assert cr.status_code == 200
    client_id = cr.json()["id"]
    inv_payload = {
        "client_id": client_id,
        "po_no": "UNASSIGNED",
        "invoice_no": "INV-RET-1",
        "sub_entity": "",
        "lr_no": "",
        "inv_date": "2026-04-01",
        "due_date": None,
        "basic": 500.0,
        "gst": 0.0,
        "total": 500.0,
        "advance_adj": 0.0,
        "tds_ded": 0.0,
        "retention_held": 100.0,
        "net_payable": 0.0,
        "paid": 0.0,
        "balance": 0.0,
        "is_note": False,
        "note_type": None,
        "note_reason": None,
        "dispatch_items": [],
    }
    inv = client.post("/api/invoices", json=inv_payload, headers=auth_header(token))
    assert inv.status_code == 200, inv.text
    invs = client.get("/api/invoices", headers=auth_header(token)).json()
    row = next(i for i in invs if i["id"] == "INV-RET-1")
    assert row["retention"] == pytest.approx(100.0)
    assert row["netPayable"] == pytest.approx(500.0)
    assert row["balance"] == pytest.approx(500.0)


def test_auth_and_permission_guards(client: TestClient):
    no_token = client.get("/api/clients")
    assert no_token.status_code in (401, 403)

    admin_token = login(client, "admin", "Admin@1234")
    user_token = login(client, "user1", "User@12345")
    logi_token = login(client, "logi", "Logi@12345")

    # Admin-only endpoint.
    as_user = client.post("/api/clients", json={"name": "X"}, headers=auth_header(user_token))
    assert as_user.status_code == 403

    # Logistics denied from financial client listing.
    as_logi = client.get("/api/clients", headers=auth_header(logi_token))
    assert as_logi.status_code == 403

    # Admin happy path still works.
    as_admin = client.post("/api/clients", json={"name": "OK-CLIENT"}, headers=auth_header(admin_token))
    assert as_admin.status_code == 200


def test_admin_can_create_user_without_server_error(client: TestClient):
    admin_token = login(client, "admin", "Admin@1234")

    created = client.post(
        "/api/users",
        json={"username": "new.finance", "password": "Finance@12345", "role": "user"},
        headers=auth_header(admin_token),
    )
    assert created.status_code == 200, created.text
    assert created.json()["success"] is True

    new_token = login(client, "new.finance", "Finance@12345")
    assert new_token


def test_payment_allocations_use_invid_field(client: TestClient):
    """Locks the /api/payments allocation contract so the SPA Payment Log cell keeps working.

    Backend serializes allocations as ``{type, invId, po, noteId, noteType, amount}``.
    The frontend Payment Log cell looks up matches via ``a.invId === inv.id``; if
    we ever rename ``invId`` back to ``id`` the cell silently shows '-'.
    """
    token = login(client, "admin", "Admin@1234")
    client_id = create_client_po_invoice(client, token)

    pay = client.post("/api/payments/allocate", json={
        "client_id": client_id,
        "id": "PAY-LOG-1",
        "date": "2026-04-10",
        "amount": 100.0,
        "note": "test receipt",
        "mode": "targeted",
        "targets": [{"inv_id": "INV-001", "amount": 100.0}],
        "fund_source": "receipt",
    }, headers=auth_header(token))
    assert pay.status_code in (200, 201), pay.text

    payments = client.get("/api/payments", headers=auth_header(token)).json()
    assert payments, "expected at least one payment"
    inv_allocs = [a for p in payments for a in p.get("allocations", []) if a.get("type") == "invoice"]
    assert inv_allocs, "expected invoice allocations to be present"
    for a in inv_allocs:
        assert "invId" in a, f"allocation missing 'invId' key: {a}"
        assert a["invId"] == "INV-001"
        assert "noteType" in a  # may be None for non-note allocations


def test_invoice_rounding_and_tiny_balance_preserved(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    create_client_po_invoice(client, token)

    invs = client.get("/api/invoices", headers=auth_header(token))
    assert invs.status_code == 200
    inv = next(i for i in invs.json() if i["id"] == "INV-001")
    # Rounded at source.
    assert inv["basic"] == pytest.approx(1000.0)
    assert inv["total"] == pytest.approx(1181.0)
    # Tiny residual (< INR 5) is now PRESERVED, not auto-zeroed. The UI shows
    # it as CLEARED + green while still surfacing the actual outstanding figure.
    assert 0.0 < inv["balance"] < 5.0


def test_duplicate_invoice_rejected(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    client_id = create_client_po_invoice(client, token)
    dup_payload = {
        "client_id": client_id,
        "po_no": "PO-001",
        "invoice_no": "INV-001",
        "sub_entity": "",
        "lr_no": "",
        "inv_date": "2026-04-02",
        "due_date": None,
        "basic": 1,
        "gst": 0,
        "total": 1,
        "advance_adj": 0,
        "tds_ded": 0,
        "retention_held": 0,
        "net_payable": 0,
        "paid": 0,
        "balance": 0,
        "is_note": False,
        "note_type": None,
        "note_reason": None,
        "dispatch_items": [],
    }
    dup = client.post("/api/invoices", json=dup_payload, headers=auth_header(token))
    assert dup.status_code == 400
    assert "already exists" in dup.json()["detail"].lower()


def test_purchase_order_status_update_and_delete_do_not_crash(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    create_client_po_invoice(client, token)

    status = client.put(
        "/api/purchase-orders/PO-001/status",
        json={"is_completed": True, "is_hidden": False},
        headers=auth_header(token),
    )
    assert status.status_code == 200, status.text

    deleted = client.delete("/api/purchase-orders/PO-001", headers=auth_header(token))
    assert deleted.status_code == 200, deleted.text


def test_delete_then_readd_invoice_drops_old_allocations(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    client_id = create_client_po_invoice(client, token)

    alloc_payload = {
        "client_id": client_id,
        "id": "PAY-1",
        "date": datetime.date.today().isoformat(),
        "amount": 500.0,
        "note": "receipt",
        "mode": "targeted",
        "targets": [{"inv_id": "INV-001", "amount": 500.0}],
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
    }
    alloc = client.post("/api/payments/allocate", json=alloc_payload, headers=auth_header(token))
    assert alloc.status_code == 200, alloc.text

    deleted = client.delete("/api/invoices/INV-001", headers=auth_header(token))
    assert deleted.status_code == 200

    ua = client.get(
        "/api/registers/unallocated-payments",
        params={"client_id": client_id},
        headers=auth_header(token),
    )
    assert ua.status_code == 200, ua.text
    rows = ua.json()
    assert rows, "expected unallocated register rows after invoice deletion"
    deletion_rows = [r for r in rows if r.get("source_kind") == "invoice_deleted" and r.get("source_invoice_no") == "INV-001"]
    assert deletion_rows, f"expected invoice_deleted source row, got: {rows}"
    assert deletion_rows[0]["balance"] > 0

    recreate_payload = {
        "client_id": client_id,
        "po_no": "PO-001",
        "invoice_no": "INV-001",
        "sub_entity": "Unit-1",
        "lr_no": "LR-RE",
        "inv_date": "2026-04-03",
        "due_date": "2026-04-20",
        "basic": 1000.0,
        "gst": 180.0,
        "total": 1180.0,
        "advance_adj": 0.0,
        "tds_ded": 0.0,
        "retention_held": 0.0,
        "net_payable": 0.0,
        "paid": 0.0,
        "balance": 0.0,
        "is_note": False,
        "note_type": None,
        "note_reason": None,
        "dispatch_items": [],
    }
    recreate = client.post("/api/invoices", json=recreate_payload, headers=auth_header(token))
    assert recreate.status_code == 200, recreate.text

    payments = client.get("/api/payments", headers=auth_header(token))
    assert payments.status_code == 200
    # No payment allocation should remain linked to INV-001 from old deleted record.
    for p in payments.json():
        for a in p.get("allocations", []):
            assert a.get("invId") != "INV-001"

    invs = client.get("/api/invoices", headers=auth_header(token)).json()
    inv = next(i for i in invs if i["id"] == "INV-001")
    assert inv["paid"] == pytest.approx(0.0)


def test_delete_payment_unallocates_invoices_and_restores_balance(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    cr = client.post("/api/clients", json={"name": "PAY-DEL-CLIENT"}, headers=auth_header(token))
    assert cr.status_code == 200, cr.text
    client_id = cr.json()["id"]
    inv = client.post("/api/invoices", json={
        "client_id": client_id,
        "po_no": "UNASSIGNED",
        "invoice_no": "INV-PAY-DEL-1",
        "sub_entity": "",
        "lr_no": "",
        "inv_date": "2026-04-01",
        "due_date": None,
        "basic": 1000.0,
        "gst": 180.0,
        "total": 1180.0,
        "advance_adj": 0.0,
        "tds_ded": 0.0,
        "retention_held": 0.0,
        "net_payable": 0.0,
        "paid": 0.0,
        "balance": 0.0,
        "is_note": False,
        "note_type": None,
        "note_reason": None,
        "dispatch_items": [],
    }, headers=auth_header(token))
    assert inv.status_code == 200, inv.text

    alloc = client.post("/api/payments/allocate", json={
        "client_id": client_id,
        "id": "PAY-DEL-1",
        "date": datetime.date.today().isoformat(),
        "amount": 300.0,
        "note": "receipt",
        "mode": "targeted",
        "targets": [{"inv_id": "INV-PAY-DEL-1", "amount": 300.0}],
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
    }, headers=auth_header(token))
    assert alloc.status_code == 200, alloc.text

    invs_after_alloc = client.get("/api/invoices", headers=auth_header(token)).json()
    inv_after_alloc = next(i for i in invs_after_alloc if i["id"] == "INV-PAY-DEL-1")
    assert inv_after_alloc["paid"] == pytest.approx(300.0)
    assert inv_after_alloc["balance"] == pytest.approx(inv_after_alloc["netPayable"] - 300.0)

    deleted = client.delete("/api/payments/PAY-DEL-1", headers=auth_header(token))
    assert deleted.status_code == 200, deleted.text

    invs_after_delete = client.get("/api/invoices", headers=auth_header(token)).json()
    inv_after_delete = next(i for i in invs_after_delete if i["id"] == "INV-PAY-DEL-1")
    assert inv_after_delete["paid"] == pytest.approx(0.0)
    assert inv_after_delete["balance"] == pytest.approx(inv_after_delete["netPayable"])


def test_po_terms_update_recalculates_client_ledger(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    client_id = create_client_po_invoice(client, token)

    # Corrupt one invoice row to mimic stale ledger math.
    db = app_module.SessionLocal()
    try:
        inv = db.query(app_module.Invoice).filter(app_module.Invoice.invoice_no == "INV-001").first()
        assert inv is not None
        inv.net_payable = 0.0
        inv.balance = 0.0
        inv.paid = 0.0
        db.commit()
    finally:
        db.close()

    update_po = client.post("/api/purchase-orders", json={
        "client_id": client_id,
        "po_no": "PO-001",
        "contact_person": "Ops-Updated",
        "project_name": "Kiln Rev-2",
        "adv_pct": 8.0,
        "ret_pct": 3.0,
        "ret_base": "total",
        "tds_enabled": True,
        "tds_rate": 0.1,
        "tds_threshold": 5000.0,
        "baseline_items": [
            {"description": "Brick A", "ordered_qty": 100, "inspected_qty": 0, "uom": "Nos", "material_type": "brick"},
        ],
    }, headers=auth_header(token))
    assert update_po.status_code == 200, update_po.text

    invs = client.get("/api/invoices", headers=auth_header(token)).json()
    inv = next(i for i in invs if i["id"] == "INV-001")
    assert inv["netPayable"] == pytest.approx(inv["total"])
    assert inv["balance"] == pytest.approx(inv["netPayable"] - inv["paid"])


def test_po_tds_terms_update_changes_invoice_balance(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    client_id = create_client_po_invoice(client, token)

    # Raise invoice total above threshold so TDS should apply after PO update.
    upd = client.patch(
        "/api/invoices/INV-001/inline",
        json={"total": 10000.0},
        headers=auth_header(token),
    )
    assert upd.status_code == 200, upd.text

    update_po = client.post("/api/purchase-orders", json={
        "client_id": client_id,
        "po_no": "PO-001",
        "contact_person": "Ops",
        "project_name": "Kiln",
        "adv_pct": 5.0,
        "ret_pct": 2.0,
        "ret_base": "total",
        "tds_base": "total",
        "tds_enabled": True,
        "tds_rate": 0.1,
        "tds_threshold": 5000.0,
        "baseline_items": [],
    }, headers=auth_header(token))
    assert update_po.status_code == 200, update_po.text

    invs = client.get("/api/invoices", headers=auth_header(token)).json()
    inv = next(i for i in invs if i["id"] == "INV-001")
    assert inv["tds"] == pytest.approx(10.0)
    assert inv["netPayable"] == pytest.approx(9990.0)
    assert inv["balance"] == pytest.approx(9990.0)


def test_po_tds_on_basic_not_gross(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    client_id = create_client_po_invoice(client, token)

    upd = client.patch(
        "/api/invoices/INV-001/inline",
        json={"total": 10000.0},
        headers=auth_header(token),
    )
    assert upd.status_code == 200, upd.text

    update_po = client.post("/api/purchase-orders", json={
        "client_id": client_id,
        "po_no": "PO-001",
        "contact_person": "Ops",
        "project_name": "Kiln",
        "adv_pct": 5.0,
        "ret_pct": 2.0,
        "ret_base": "total",
        "tds_base": "basic",
        "tds_enabled": True,
        "tds_rate": 0.1,
        "tds_threshold": 500.0,
        "baseline_items": [],
    }, headers=auth_header(token))
    assert update_po.status_code == 200, update_po.text

    invs = client.get("/api/invoices", headers=auth_header(token)).json()
    inv = next(i for i in invs if i["id"] == "INV-001")
    # basic is 1000 (rounded from create payload); TDS 0.1% on basic, not on gross 10000.
    assert inv["basic"] == pytest.approx(1000.0)
    assert inv["tds"] == pytest.approx(1.0)
    assert inv["netPayable"] == pytest.approx(10000.0 - 1.0)


def test_po_advance_auto_apply_existing_and_new_invoices(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    client_id = create_client_po_invoice(client, token)

    inv2_payload = {
        "client_id": client_id,
        "po_no": "PO-001",
        "invoice_no": "INV-ADV-2",
        "sub_entity": "",
        "lr_no": "",
        "inv_date": "2026-04-02",
        "due_date": None,
        "basic": 500.0,
        "gst": 0.0,
        "total": 500.0,
        "advance_adj": 0.0,
        "tds_ded": 0.0,
        "retention_held": 0.0,
        "net_payable": 0.0,
        "paid": 0.0,
        "balance": 0.0,
        "is_note": False,
        "note_type": None,
        "note_reason": None,
        "dispatch_items": [],
    }
    add2 = client.post("/api/invoices", json=inv2_payload, headers=auth_header(token))
    assert add2.status_code == 200, add2.text

    adv_pay = client.post("/api/payments/allocate", json={
        "client_id": client_id,
        "id": "PAY-PO-ADV-1",
        "date": datetime.date.today().isoformat(),
        "amount": 400.0,
        "note": "po advance add",
        "mode": "targeted",
        "targets": [],
        "hold_ret": False,
        "hold_gst": False,
        "only_gst": False,
        "apply_adv": False,
        "advance_only": False,
        "fund_source": "receipt",
        "move_to_po": "PO-001",
        "po_no": "PO-001",
        "clear_po_pool": False,
        "excess_action": "park",
    }, headers=auth_header(token))
    assert adv_pay.status_code == 200, adv_pay.text

    invs = client.get("/api/invoices", headers=auth_header(token)).json()
    inv1 = next(i for i in invs if i["id"] == "INV-001")
    inv2 = next(i for i in invs if i["id"] == "INV-ADV-2")
    assert inv1["advance"] == pytest.approx(50.0, abs=0.01)
    assert inv2["advance"] == pytest.approx(25.0)

    inv3_payload = {
        "client_id": client_id,
        "po_no": "PO-001",
        "invoice_no": "INV-ADV-3",
        "sub_entity": "",
        "lr_no": "",
        "inv_date": "2026-04-03",
        "due_date": None,
        "basic": 800.0,
        "gst": 0.0,
        "total": 800.0,
        "advance_adj": 0.0,
        "tds_ded": 0.0,
        "retention_held": 0.0,
        "net_payable": 0.0,
        "paid": 0.0,
        "balance": 0.0,
        "is_note": False,
        "note_type": None,
        "note_reason": None,
        "dispatch_items": [],
    }
    add3 = client.post("/api/invoices", json=inv3_payload, headers=auth_header(token))
    assert add3.status_code == 200, add3.text

    invs2 = client.get("/api/invoices", headers=auth_header(token)).json()
    inv3 = next(i for i in invs2 if i["id"] == "INV-ADV-3")
    assert inv3["advance"] == pytest.approx(40.0, abs=0.05)


def test_po_advance_auto_apply_cannot_create_implicit_invoice_credit(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    client_id = create_simple_po_invoice(client, token, "PO-ADV-CAP-AUTO", "INV-ADV-CAP-AUTO", adv_pct=20.0)

    receipt = client.post("/api/payments/allocate", json={
        "client_id": client_id,
        "id": "PAY-ADV-CAP-RECEIPT",
        "date": datetime.date.today().isoformat(),
        "amount": 950.0,
        "note": "almost full receipt",
        "mode": "targeted",
        "targets": [{"inv_id": "INV-ADV-CAP-AUTO", "amount": 950.0}],
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
    }, headers=auth_header(token))
    assert receipt.status_code == 200, receipt.text

    advance = client.post("/api/payments/allocate", json={
        "client_id": client_id,
        "id": "PAY-ADV-CAP-POOL",
        "date": datetime.date.today().isoformat(),
        "amount": 200.0,
        "note": "po advance add",
        "mode": "targeted",
        "targets": [],
        "hold_ret": False,
        "hold_gst": False,
        "only_gst": False,
        "apply_adv": False,
        "advance_only": False,
        "fund_source": "receipt",
        "move_to_po": "PO-ADV-CAP-AUTO",
        "po_no": "PO-ADV-CAP-AUTO",
        "clear_po_pool": False,
        "excess_action": "park",
    }, headers=auth_header(token))
    assert advance.status_code == 200, advance.text

    invs = client.get("/api/invoices", headers=auth_header(token)).json()
    inv = next(i for i in invs if i["id"] == "INV-ADV-CAP-AUTO")
    assert inv["advance"] == pytest.approx(50.0)
    assert inv["paid"] == pytest.approx(950.0)
    assert inv["balance"] == pytest.approx(0.0)


def test_apply_adv_receipt_allocation_uses_post_advance_balance(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    client_id = create_simple_po_invoice(client, token, "PO-ADV-CAP-MANUAL", "INV-ADV-CAP-MANUAL", adv_pct=20.0)

    db = app_module.SessionLocal()
    try:
        db.add(app_module.PaymentHistory(
            id="PAY-SEEDED-ADV",
            client_id=client_id,
            date=datetime.date.today(),
            type="RECEIPT",
            amount=100.0,
            details="seeded unapplied PO advance",
        ))
        db.flush()
        db.add(app_module.PaymentAllocation(
            payment_id="PAY-SEEDED-ADV",
            alloc_type="po_advance",
            target_inv_id=None,
            target_po_no="PO-ADV-CAP-MANUAL",
            note_id=None,
            amount=100.0,
        ))
        db.commit()
    finally:
        db.close()

    alloc = client.post("/api/payments/allocate", json={
        "client_id": client_id,
        "id": "PAY-ADV-CAP-MANUAL",
        "date": datetime.date.today().isoformat(),
        "amount": 1000.0,
        "note": "apply advance plus receipt",
        "mode": "targeted",
        "targets": [{"inv_id": "INV-ADV-CAP-MANUAL", "amount": 1000.0}],
        "hold_ret": False,
        "hold_gst": False,
        "only_gst": False,
        "apply_adv": True,
        "advance_only": False,
        "fund_source": "receipt",
        "move_to_po": None,
        "po_no": "PO-ADV-CAP-MANUAL",
        "clear_po_pool": False,
        "excess_action": "park",
    }, headers=auth_header(token))
    assert alloc.status_code == 200, alloc.text

    invs = client.get("/api/invoices", headers=auth_header(token)).json()
    inv = next(i for i in invs if i["id"] == "INV-ADV-CAP-MANUAL")
    assert inv["advance"] == pytest.approx(100.0)
    assert inv["paid"] == pytest.approx(900.0)
    assert inv["balance"] == pytest.approx(0.0)


def test_po_advance_pools_api_and_manual_recalculate(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    client_id = create_client_po_invoice(client, token)

    pools = client.get(f"/api/clients/{client_id}/po-advance-pools", headers=auth_header(token))
    assert pools.status_code == 200, pools.text
    body = pools.json()
    assert "pools" in body
    po_entry = next((p for p in body["pools"] if p.get("po_no") == "PO-001"), None)
    assert po_entry is not None

    rec = client.post(f"/api/clients/{client_id}/ledger/recalculate", headers=auth_header(token))
    assert rec.status_code == 200, rec.text

    apply = client.post(
        f"/api/clients/{client_id}/po-advance/manual-apply",
        json={"po_no": "PO-001"},
        headers=auth_header(token),
    )
    assert apply.status_code == 200, apply.text
    assert "pool_remaining" in apply.json()


def test_delete_po_advance_payment_deallocates_from_invoices(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    client_id = create_client_po_invoice(client, token)

    adv_pay = client.post("/api/payments/allocate", json={
        "client_id": client_id,
        "id": "PAY-PO-ADV-DEL-1",
        "date": datetime.date.today().isoformat(),
        "amount": 200.0,
        "note": "po advance add",
        "mode": "targeted",
        "targets": [],
        "hold_ret": False,
        "hold_gst": False,
        "only_gst": False,
        "apply_adv": False,
        "advance_only": False,
        "fund_source": "receipt",
        "move_to_po": "PO-001",
        "po_no": "PO-001",
        "clear_po_pool": False,
        "excess_action": "park",
    }, headers=auth_header(token))
    assert adv_pay.status_code == 200, adv_pay.text

    invs_before = client.get("/api/invoices", headers=auth_header(token)).json()
    inv_before = next(i for i in invs_before if i["id"] == "INV-001")
    assert inv_before["advance"] > 0

    deleted = client.delete("/api/payments/PAY-PO-ADV-DEL-1", headers=auth_header(token))
    assert deleted.status_code == 200, deleted.text

    invs_after = client.get("/api/invoices", headers=auth_header(token)).json()
    inv_after = next(i for i in invs_after if i["id"] == "INV-001")
    assert inv_after["advance"] == pytest.approx(0.0)


def test_invoice_delete_returns_po_advance_to_pool_for_other_invoices(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    client_id = create_client_po_invoice(client, token)

    adv_pay = client.post("/api/payments/allocate", json={
        "client_id": client_id,
        "id": "PAY-PO-ADV-POOL-1",
        "date": datetime.date.today().isoformat(),
        "amount": 100.0,
        "note": "po advance add",
        "mode": "targeted",
        "targets": [],
        "hold_ret": False,
        "hold_gst": False,
        "only_gst": False,
        "apply_adv": False,
        "advance_only": False,
        "fund_source": "receipt",
        "move_to_po": "PO-001",
        "po_no": "PO-001",
        "clear_po_pool": False,
        "excess_action": "park",
    }, headers=auth_header(token))
    assert adv_pay.status_code == 200, adv_pay.text

    deleted = client.delete("/api/invoices/INV-001", headers=auth_header(token))
    assert deleted.status_code == 200, deleted.text

    payments = client.get("/api/payments", headers=auth_header(token))
    assert payments.status_code == 200, payments.text
    target_pay = next(p for p in payments.json() if p["id"] == "PAY-PO-ADV-POOL-1")
    assert any(a.get("type") == "po_advance" and a.get("po") == "PO-001" for a in target_pay.get("allocations", []))
    assert not any(a.get("type") == "po_advance_applied" and a.get("invId") == "INV-001" for a in target_pay.get("allocations", []))


def test_note_issue_inherits_invoice_po_and_target_link(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    client_id = create_client_po_invoice(client, token)

    note_payload = {
        "client_id": client_id,
        "note_no": "CN-001",
        "date": "2026-04-10",
        "note_type": "CN",
        "amount": 50.0,
        "reason": "quality discount",
        "target_invoice_id": "INV-001",
    }
    note = client.post("/api/notes/issue", json=note_payload, headers=auth_header(token))
    assert note.status_code == 200, note.text

    invs = client.get("/api/invoices", headers=auth_header(token))
    assert invs.status_code == 200
    cn = next(i for i in invs.json() if i["id"] == "CN-001")
    assert cn["isNote"] is True
    assert cn["poNo"] == "PO-001"
    assert cn["noteTargetInvoice"] == "INV-001"


def test_dispatch_column_rename_merges_duplicates(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    client_id = create_client_po_invoice(client, token)

    # Create second invoice with variant dispatch desc under same PO.
    inv2_payload = {
        "client_id": client_id,
        "po_no": "PO-001",
        "invoice_no": "INV-002",
        "sub_entity": "Unit-1",
        "lr_no": "LR-12",
        "inv_date": "2026-04-02",
        "due_date": "2026-04-16",
        "basic": 500.0,
        "gst": 90.0,
        "total": 590.0,
        "advance_adj": 0.0,
        "tds_ded": 0.0,
        "retention_held": 0.0,
        "net_payable": 0.0,
        "paid": 0.0,
        "balance": 0.0,
        "is_note": False,
        "note_type": None,
        "note_reason": None,
        "dispatch_items": [{"description": "Brick-A", "qty": 7, "inspected_qty": 0, "uom": "Nos"}],
    }
    add2 = client.post("/api/invoices", json=inv2_payload, headers=auth_header(token))
    assert add2.status_code == 200, add2.text

    rename = client.post(
        "/api/dispatch-columns/rename",
        json={
            "client_id": client_id,
            "po_no": "PO-001",
            "old_description": "Brick-A",
            "old_uom": "Nos",
            "new_description": "Brick A",
            "new_uom": "Nos",
        },
        headers=auth_header(token),
    )
    assert rename.status_code == 200, rename.text
    data = rename.json()
    assert data["success"] is True
    assert data["dispatch_renamed"] >= 0


def test_payment_allocate_validation_edge_case(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    client_id = create_client_po_invoice(client, token)

    bad_payload = {
        "client_id": client_id,
        "id": "PAY-EDGE",
        "date": datetime.date.today().isoformat(),
        "amount": 0.0,
        "note": "invalid",
        "mode": "targeted",
        "targets": [],
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
    }
    res = client.post("/api/payments/allocate", json=bad_payload, headers=auth_header(token))
    assert res.status_code == 400
    assert "greater than zero" in res.json()["detail"].lower()
