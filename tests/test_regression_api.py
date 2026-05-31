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


def test_reverse_unallocated_applied_restores_register_for_applet(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    cr = client.post("/api/clients", json={"name": "UNALLOC-REV-CLIENT"}, headers=auth_header(token))
    assert cr.status_code == 200, cr.text
    client_id = cr.json()["id"]

    inv = client.post(
        "/api/invoices",
        json={
            "client_id": client_id,
            "po_no": "PO-UR-1",
            "invoice_no": "INV-UR-1",
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

    seed = client.post(
        "/api/payments/allocate",
        json={
            "client_id": client_id,
            "id": "PAY-UR-SEED",
            "date": datetime.date.today().isoformat(),
            "amount": 500.0,
            "note": "seed receipt",
            "mode": "targeted",
            "targets": [{"inv_id": "INV-UR-1", "amount": 200.0}],
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
        headers=auth_header(token),
    )
    assert seed.status_code == 200, seed.text

    regs_before = client.get(
        f"/api/registers/unallocated-payments?client_id={client_id}",
        headers=auth_header(token),
    ).json()
    open_before = [r for r in regs_before if r.get("status") == "open" and float(r.get("balance") or 0) > 0]
    assert sum(float(r["balance"]) for r in open_before) == pytest.approx(300.0, abs=0.02)

    applied = client.post(
        "/api/payments/allocate",
        json={
            "client_id": client_id,
            "id": "PAY-UR-APPLY",
            "date": datetime.date.today().isoformat(),
            "amount": 150.0,
            "note": "from pool",
            "mode": "targeted",
            "targets": [{"inv_id": "INV-UR-1", "amount": 150.0}],
            "hold_ret": False,
            "hold_gst": False,
            "only_gst": False,
            "apply_adv": False,
            "advance_only": False,
            "fund_source": "unallocated",
            "move_to_po": None,
            "po_no": None,
            "clear_po_pool": False,
            "excess_action": "park",
        },
        headers=auth_header(token),
    )
    assert applied.status_code == 200, applied.text

    regs_after_apply = client.get(
        f"/api/registers/unallocated-payments?client_id={client_id}",
        headers=auth_header(token),
    ).json()
    open_after_apply = [r for r in regs_after_apply if r.get("status") == "open" and float(r.get("balance") or 0) > 0]
    assert sum(float(r["balance"]) for r in open_after_apply) == pytest.approx(150.0, abs=0.02)

    deleted = client.delete("/api/payments/PAY-UR-APPLY", headers=auth_header(token))
    assert deleted.status_code == 200, deleted.text

    regs_after_reverse = client.get(
        f"/api/registers/unallocated-payments?client_id={client_id}",
        headers=auth_header(token),
    ).json()
    open_after_reverse = [r for r in regs_after_reverse if r.get("status") == "open" and float(r.get("balance") or 0) > 0]
    assert sum(float(r["balance"]) for r in open_after_reverse) == pytest.approx(300.0, abs=0.02)


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


def test_manual_tds_persists_after_po_recalculate(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    client_id = create_client_po_invoice(client, token)

    recalc = client.post(f"/api/clients/{client_id}/ledger/recalculate", headers=auth_header(token))
    assert recalc.status_code == 200, recalc.text

    manual_tds = 42.5
    upd = client.put(
        "/api/invoices/INV-001",
        json={
            "client_id": client_id,
            "po_no": "PO-001",
            "invoice_no": "INV-001",
            "sub_entity": "Unit-1",
            "lr_no": "LR-11",
            "inv_date": "2026-04-01",
            "due_date": "2026-04-15",
            "basic": 1000.0,
            "gst": 180.0,
            "total": 1180.0,
            "advance_adj": 0.0,
            "tds_ded": manual_tds,
            "retention_held": 0.0,
            "net_payable": 0.0,
            "paid": 0.0,
            "balance": 0.0,
            "is_note": False,
            "dispatch_items": [],
        },
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
        "tds_rate": 10.0,
        "tds_threshold": 0.0,
        "baseline_items": [],
    }, headers=auth_header(token))
    assert update_po.status_code == 200, update_po.text

    invs = client.get("/api/invoices", headers=auth_header(token)).json()
    inv = next(i for i in invs if i["id"] == "INV-001")
    assert inv["tds"] == pytest.approx(manual_tds, abs=0.01)
    assert inv["netPayable"] == pytest.approx(1180.0 - manual_tds, abs=0.01)


def test_manual_tds_zero_when_po_tds_enabled_but_not_entered(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    client_id = create_client_po_invoice(client, token)

    client.patch(
        "/api/invoices/INV-001/inline",
        json={"total": 10000.0},
        headers=auth_header(token),
    )

    client.post("/api/purchase-orders", json={
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

    invs = client.get("/api/invoices", headers=auth_header(token)).json()
    inv = next(i for i in invs if i["id"] == "INV-001")
    assert inv["tds"] == pytest.approx(0.0, abs=0.01)
    assert inv["netPayable"] == pytest.approx(10000.0, abs=0.01)


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

    # Re-open ledger so paid is allocation-driven (fixture seeds a manual paid figure).
    recalc = client.post(f"/api/clients/{client_id}/ledger/recalculate", headers=auth_header(token))
    assert recalc.status_code == 200, recalc.text

    invs_before = client.get("/api/invoices", headers=auth_header(token))
    assert invs_before.status_code == 200
    inv_before = next(i for i in invs_before.json() if i["id"] == "INV-001")
    balance_before = float(inv_before["balance"])

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
    assert float(cn["balance"]) == pytest.approx(-50.0, abs=0.01)
    # Target invoice due is unchanged; CN effect is on the note row and ledger totals only.

    inv_after = next(i for i in invs.json() if i["id"] == "INV-001")
    assert float(inv_after["balance"]) == pytest.approx(balance_before, abs=0.01)
    assert float(inv_after["paid"]) == pytest.approx(float(inv_before["paid"]), abs=0.01)
    assert float(inv_after["netPayable"]) == pytest.approx(float(inv_before["netPayable"]), abs=0.01)


def test_debit_note_does_not_change_target_invoice_balance(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    client_id = create_client_po_invoice(client, token)

    recalc = client.post(f"/api/clients/{client_id}/ledger/recalculate", headers=auth_header(token))
    assert recalc.status_code == 200, recalc.text

    invs_before = client.get("/api/invoices", headers=auth_header(token))
    inv_before = next(i for i in invs_before.json() if i["id"] == "INV-001")
    balance_before = float(inv_before["balance"])

    dn_payload = {
        "client_id": client_id,
        "note_no": "DN-001",
        "date": "2026-04-11",
        "note_type": "DN",
        "amount": 25.0,
        "reason": "extra charge",
        "target_invoice_id": "INV-001",
    }
    dn = client.post("/api/notes/issue", json=dn_payload, headers=auth_header(token))
    assert dn.status_code == 200, dn.text

    invs = client.get("/api/invoices", headers=auth_header(token))
    inv_after = next(i for i in invs.json() if i["id"] == "INV-001")
    assert float(inv_after["balance"]) == pytest.approx(balance_before, abs=0.01)
    dn_row = next(i for i in invs.json() if i["id"] == "DN-001")
    assert dn_row["poNo"] == "PO-001"
    assert float(dn_row["balance"]) == pytest.approx(25.0, abs=0.01)


def test_payment_allocation_applies_to_debit_note(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    client_id = create_client_po_invoice(client, token)

    recalc = client.post(f"/api/clients/{client_id}/ledger/recalculate", headers=auth_header(token))
    assert recalc.status_code == 200, recalc.text

    dn = client.post("/api/notes/issue", json={
        "client_id": client_id,
        "note_no": "DN-PAY-001",
        "date": "2026-04-13",
        "note_type": "DN",
        "amount": 40.0,
        "reason": "extra freight",
        "target_invoice_id": "INV-001",
    }, headers=auth_header(token))
    assert dn.status_code == 200, dn.text

    alloc = client.post("/api/payments/allocate", json={
        "client_id": client_id,
        "id": "PAY-DN-001",
        "date": "2026-04-14",
        "amount": 15.0,
        "note": "partial on DN",
        "mode": "targeted",
        "targets": [{"inv_id": "DN-PAY-001", "amount": 15.0}],
        "hold_ret": False,
        "hold_gst": False,
        "only_gst": False,
        "apply_adv": False,
        "advance_only": False,
        "fund_source": "receipt",
        "excess_action": "park",
    }, headers=auth_header(token))
    assert alloc.status_code == 200, alloc.text

    invs = client.get("/api/invoices", headers=auth_header(token)).json()
    inv = next(i for i in invs if i["id"] == "INV-001")
    dn_row = next(i for i in invs if i["id"] == "DN-PAY-001")
    assert float(dn_row["paid"]) == pytest.approx(15.0, abs=0.01)
    assert float(dn_row["balance"]) == pytest.approx(25.0, abs=0.01)
    # Target invoice must remain independent of DN payment.
    assert float(inv["balance"]) == pytest.approx(float(inv["netPayable"]) - float(inv["paid"]), abs=0.01)


def test_debit_note_allocation_with_only_gst_option_enabled(client: TestClient):
    """DN rows must allocate even when Only GST / holds are checked (no GST on notes)."""
    token = login(client, "admin", "Admin@1234")
    client_id = create_client_po_invoice(client, token)

    client.post(f"/api/clients/{client_id}/ledger/recalculate", headers=auth_header(token))
    dn = client.post("/api/notes/issue", json={
        "client_id": client_id,
        "note_no": "DN-GST-001",
        "date": "2026-04-15",
        "note_type": "DN",
        "amount": 30.0,
        "reason": "charge",
        "target_invoice_id": "INV-001",
    }, headers=auth_header(token))
    assert dn.status_code == 200, dn.text

    alloc = client.post("/api/payments/allocate", json={
        "client_id": client_id,
        "id": "PAY-DN-GST-001",
        "date": "2026-04-16",
        "amount": 12.0,
        "note": "pay DN with only_gst on",
        "mode": "targeted",
        "targets": [{"inv_id": "DN-GST-001", "amount": 12.0}],
        "hold_ret": True,
        "hold_gst": True,
        "only_gst": True,
        "apply_adv": False,
        "advance_only": False,
        "fund_source": "receipt",
        "excess_action": "park",
    }, headers=auth_header(token))
    assert alloc.status_code == 200, alloc.text
    assert alloc.json().get("allocation_count", 0) >= 1

    invs = client.get("/api/invoices", headers=auth_header(token)).json()
    dn_row = next(i for i in invs if i["id"] == "DN-GST-001")
    assert float(dn_row["paid"]) == pytest.approx(12.0, abs=0.01)
    assert float(dn_row["balance"]) == pytest.approx(18.0, abs=0.01)


def test_debit_note_api_balance_reflects_payments(client: TestClient):
    """GET /api/invoices balance for DN must decrease after allocation (not stick at gross)."""
    token = login(client, "admin", "Admin@1234")
    client_id = create_client_po_invoice(client, token)
    client.post(f"/api/clients/{client_id}/ledger/recalculate", headers=auth_header(token))
    client.post("/api/notes/issue", json={
        "client_id": client_id,
        "note_no": "DN-API-BAL",
        "date": "2026-04-17",
        "note_type": "DN",
        "amount": 100.0,
        "reason": "test",
        "target_invoice_id": "INV-001",
    }, headers=auth_header(token))

    before = next(i for i in client.get("/api/invoices", headers=auth_header(token)).json() if i["id"] == "DN-API-BAL")
    assert float(before["balance"]) == pytest.approx(100.0, abs=0.01)

    client.post("/api/payments/allocate", json={
        "client_id": client_id,
        "id": "PAY-DN-API-BAL",
        "date": "2026-04-18",
        "amount": 40.0,
        "mode": "targeted",
        "targets": [{"inv_id": "DN-API-BAL", "amount": 40.0}],
        "hold_ret": False,
        "hold_gst": False,
        "only_gst": False,
        "apply_adv": False,
        "advance_only": False,
        "fund_source": "receipt",
        "excess_action": "park",
    }, headers=auth_header(token))

    after = next(i for i in client.get("/api/invoices", headers=auth_header(token)).json() if i["id"] == "DN-API-BAL")
    assert float(after["paid"]) == pytest.approx(40.0, abs=0.01)
    assert float(after["balance"]) == pytest.approx(60.0, abs=0.01)


def test_note_issue_trims_target_invoice_id_for_linking(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    client_id = create_client_po_invoice(client, token)

    recalc = client.post(f"/api/clients/{client_id}/ledger/recalculate", headers=auth_header(token))
    assert recalc.status_code == 200, recalc.text

    note_payload = {
        "client_id": client_id,
        "note_no": "CN-TRIM-001",
        "date": "2026-04-12",
        "note_type": "CN",
        "amount": 10.0,
        "reason": "trim check",
        "target_invoice_id": "  INV-001  ",
    }
    note = client.post("/api/notes/issue", json=note_payload, headers=auth_header(token))
    assert note.status_code == 200, note.text

    invs = client.get("/api/invoices", headers=auth_header(token))
    assert invs.status_code == 200
    cn = next(i for i in invs.json() if i["id"] == "CN-TRIM-001")
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


def test_merge_import_one_client_preserves_others(tmp_path, monkeypatch):
    import json
    import sqlite3

    import migrate_sqlite as mig
    from models import Client, Invoice, User

    target_db = tmp_path / "erp.sqlite"
    legacy_db = tmp_path / "old.sqlite"
    monkeypatch.setenv("APP_DATABASE_URL", f"sqlite:///{target_db.as_posix()}")
    monkeypatch.chdir(tmp_path)

    def _legacy_invoice(no: str, total: float = 100.0) -> dict:
        return {
            "id": no,
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

    app_data = {
        "_settings": {"exchangeRate": 83.0, "customColumns": []},
        "ClientA": {
            "active": True,
            "excess": 0.0,
            "invoices": [_legacy_invoice("INV-A-NEW", 500.0)],
            "paymentHistory": [],
            "poTerms": {},
        },
        "ClientB": {
            "active": True,
            "excess": 0.0,
            "invoices": [_legacy_invoice("INV-B-1", 200.0)],
            "paymentHistory": [],
            "poTerms": {},
        },
    }
    conn = sqlite3.connect(str(legacy_db))
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password TEXT, role TEXT)")
    conn.execute("CREATE TABLE erp_data (id INTEGER PRIMARY KEY, json_data TEXT)")
    conn.execute("INSERT INTO users VALUES (1, 'legacyadmin', 'pass', 'admin')")
    conn.execute("INSERT INTO erp_data VALUES (1, ?)", (json.dumps(app_data),))
    conn.commit()
    conn.close()

    _, SessionLocal = mig.open_target_session()
    db = SessionLocal()
    db.add(User(username="localadmin", hashed_password=mig.hash_password("secret"), role="admin"))
    client_a = Client(name="ClientA", active=True, excess_funds=0.0)
    client_c = Client(name="ClientC", active=True, excess_funds=0.0)
    db.add(client_a)
    db.add(client_c)
    db.flush()
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
    db.add(
        Invoice(
            client_id=client_c.id,
            invoice_no="INV-C-1",
            basic=50.0,
            total=50.0,
            net_payable=50.0,
            balance=50.0,
        )
    )
    db.commit()
    db.close()

    mig.run_import(mode="merge", clients="ClientA", legacy_path=str(legacy_db))

    db = SessionLocal()
    names = sorted(c.name for c in db.query(Client).all())
    assert names == ["ClientA", "ClientC"]

    client_a_row = db.query(Client).filter(Client.name == "ClientA").one()
    inv_nos = {inv.invoice_no for inv in db.query(Invoice).filter(Invoice.client_id == client_a_row.id).all()}
    assert "INV-A-NEW" in inv_nos
    assert "INV-A-OLD" not in inv_nos

    assert db.query(User).filter(User.username == "localadmin").count() == 1
    assert db.query(User).filter(User.username == "legacyadmin").count() == 0
    db.close()


def test_startup_legacy_import_does_not_replace_non_empty_database(tmp_path, monkeypatch):
    import json
    import sqlite3

    from models import Client, User

    target_db = tmp_path / "erp.sqlite"
    legacy_db = tmp_path / "old_erp.sqlite"
    monkeypatch.setenv("APP_DATABASE_URL", f"sqlite:///{target_db.as_posix()}")
    monkeypatch.setenv("LEGACY_DB_PATH", str(legacy_db))
    monkeypatch.chdir(tmp_path)

    legacy_data = {
        "_settings": {"exchangeRate": 83.0, "customColumns": []},
        "LEGACY-CLIENT": {
            "active": True,
            "excess": 0.0,
            "invoices": [],
            "paymentHistory": [],
            "poTerms": {},
        },
    }
    conn = sqlite3.connect(str(legacy_db))
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password TEXT, role TEXT)")
    conn.execute("CREATE TABLE erp_data (id INTEGER PRIMARY KEY, json_data TEXT)")
    conn.execute("INSERT INTO users VALUES (1, 'legacyadmin', 'pass', 'admin')")
    conn.execute("INSERT INTO erp_data VALUES (1, ?)", (json.dumps(legacy_data),))
    conn.commit()
    conn.close()

    test_engine = create_engine(f"sqlite:///{target_db}", connect_args={"check_same_thread": False})
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    Base.metadata.create_all(bind=test_engine)
    monkeypatch.setattr(app_module, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(app_module, "DB_FILE_PATH", Path(target_db))

    db = TestingSessionLocal()
    db.add(User(username="liveadmin", hashed_password=app_module.hash_password("secret"), role="admin"))
    db.add(Client(name="LIVE-CLIENT", active=True, excess_funds=0.0))
    db.commit()
    db.close()

    app_module._maybe_run_legacy_import()

    db = TestingSessionLocal()
    assert db.query(Client).filter(Client.name == "LIVE-CLIENT").count() == 1
    assert db.query(User).filter(User.username == "liveadmin").count() == 1
    assert db.query(Client).filter(Client.name == "LEGACY-CLIENT").count() == 0
    assert db.query(User).filter(User.username == "legacyadmin").count() == 0
    db.close()
    assert not (tmp_path / ".legacy_import_once.marker").exists()
