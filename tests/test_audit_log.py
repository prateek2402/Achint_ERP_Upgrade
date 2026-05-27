"""Tests for the append-only AuditLog and the admin /api/audit-log endpoint."""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

# Re-use the regression test fixture so we get a temp DB and seeded users.
from tests.test_regression_api import (  # noqa: F401  (fixture is consumed by name)
    client,
    login,
    auth_header,
    create_client_po_invoice,
)


def _audit_query(c: TestClient, token: str, **params) -> dict:
    resp = c.get("/api/audit-log", params=params, headers=auth_header(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_audit_log_requires_admin(client: TestClient):
    user_token = login(client, "user1", "User@12345")
    resp = client.get("/api/audit-log", headers=auth_header(user_token))
    assert resp.status_code == 403


def test_invoice_create_writes_audit_entry(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    create_client_po_invoice(client, token)

    body = _audit_query(client, token, entity_type="invoice", action="create")
    assert body["total"] >= 1
    matches = [e for e in body["items"] if e["entity_id"] == "INV-001"]
    assert matches, "expected an audit entry for INV-001 create"
    entry = matches[0]
    assert entry["username"] == "admin"
    assert entry["role"] == "admin"
    assert entry["action"] == "create"
    assert entry["entity_type"] == "invoice"
    assert entry["details"]["dispatch_item_count"] == 2
    # PO create should also be audited.
    po_entries = _audit_query(client, token, entity_type="purchase_order")
    assert any(e["entity_id"] == "PO-001" for e in po_entries["items"])


def test_audit_failure_does_not_rollback_business_transaction(client: TestClient, monkeypatch):
    import main as app_module

    token = login(client, "admin", "Admin@1234")

    created = client.post("/api/clients", json={"name": "AUDIT-SAFE"}, headers=auth_header(token))
    assert created.status_code == 200, created.text
    client_id = created.json()["id"]

    def fail_audit_write(entries):
        raise RuntimeError("simulated audit sink failure")

    monkeypatch.setattr(app_module, "_write_audit_entries", fail_audit_write)

    po_resp = client.post("/api/purchase-orders", json={
        "client_id": client_id,
        "po_no": "PO-AUDIT-SAFE",
        "contact_person": "Ops",
        "project_name": "Kiln",
        "adv_pct": 5.0,
        "ret_pct": 2.0,
        "ret_base": "basic",
        "tds_enabled": True,
        "tds_rate": 0.1,
        "tds_threshold": 5000.0,
        "baseline_items": [
            {"description": "Brick A", "ordered_qty": 10, "inspected_qty": 0, "uom": "Nos", "material_type": "brick"},
        ],
    }, headers=auth_header(token))
    assert po_resp.status_code == 200, po_resp.text

    db = app_module.SessionLocal()
    try:
        po = db.query(app_module.PurchaseOrder).filter(app_module.PurchaseOrder.po_no == "PO-AUDIT-SAFE").first()
        assert po is not None
        assert po.client_id == client_id
        assert len(po.baseline_items) == 1
    finally:
        db.close()


def test_invoice_update_records_before_after(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    client_id = create_client_po_invoice(client, token)

    update_payload = {
        "client_id": client_id,
        "po_no": "PO-001",
        "invoice_no": "INV-001",
        "sub_entity": "Unit-1",
        "lr_no": "LR-11",
        "inv_date": "2026-04-01",
        "due_date": "2026-04-15",
        "basic": 1500.0,
        "gst": 270.0,
        "total": 1770.0,
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
    resp = client.put("/api/invoices/INV-001", json=update_payload, headers=auth_header(token))
    assert resp.status_code == 200, resp.text

    body = _audit_query(client, token, entity_type="invoice", action="update", entity_id="INV-001")
    assert body["total"] >= 1
    entry = body["items"][0]
    details = entry["details"]
    assert "before" in details and "after" in details
    assert details["before"]["total"] != details["after"]["total"]
    assert details["after"]["total"] == pytest.approx(1770.0)


def test_invoice_delete_records_snapshot(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    create_client_po_invoice(client, token)

    resp = client.delete("/api/invoices/INV-001", headers=auth_header(token))
    assert resp.status_code == 200

    body = _audit_query(client, token, entity_type="invoice", action="delete", entity_id="INV-001")
    assert body["total"] >= 1
    entry = body["items"][0]
    assert entry["details"]["client_id"] is not None


def test_audit_log_filters_and_pagination(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    create_client_po_invoice(client, token)

    # All entries for the seeded admin should include both PO + invoice creates.
    body = _audit_query(client, token, username="admin", limit=50)
    actions = {e["action"] for e in body["items"]}
    assert "create" in actions

    # Filtering by a nonexistent entity returns an empty page but valid envelope.
    body_empty = _audit_query(client, token, entity_type="nonexistent")
    assert body_empty["total"] == 0
    assert body_empty["items"] == []

    # Pagination shape.
    page = _audit_query(client, token, limit=1, offset=0)
    assert page["limit"] == 1
    assert len(page["items"]) <= 1
