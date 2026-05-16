"""Tests for the reconciliation report API and individual checks."""

from __future__ import annotations

import datetime
import pytest
from fastapi.testclient import TestClient

from tests.test_regression_api import (  # noqa: F401
    client,
    login,
    auth_header,
    create_client_po_invoice,
)
import main as app_module
from models import Invoice, PaymentHistory, PaymentAllocation, PoBaselineItem, InvoiceDispatchItem


def _get_report(c: TestClient, token: str) -> dict:
    resp = c.get("/api/reconciliation/summary", headers=auth_header(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _section(report: dict, key: str) -> dict:
    matches = [s for s in report["sections"] if s["key"] == key]
    assert matches, f"section {key} missing from report"
    return matches[0]


def test_reconciliation_requires_admin(client: TestClient):
    user_token = login(client, "user1", "User@12345")
    resp = client.get("/api/reconciliation/summary", headers=auth_header(user_token))
    assert resp.status_code == 403


def test_reconciliation_clean_ledger_has_no_errors(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    create_client_po_invoice(client, token)

    report = _get_report(client, token)
    assert report["overall_status"] in ("ok", "warnings")
    assert report["errors"] == 0
    # Each known check must be present.
    for key in ("invoice_math", "payment_allocations", "dispatch_coverage", "note_linkage"):
        assert _section(report, key)["errors"] == 0


def _open_session():
    """Helper: open a session against the test DB to inject bad data directly."""
    return app_module.SessionLocal()


def test_invoice_math_check_detects_balance_drift(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    create_client_po_invoice(client, token)

    db = _open_session()
    try:
        inv = db.query(Invoice).filter(Invoice.invoice_no == "INV-001").first()
        # Force inconsistent persisted balance.
        inv.balance = 9999.99
        inv.net_payable = 9999.99
        db.commit()
    finally:
        db.close()

    report = _get_report(client, token)
    section = _section(report, "invoice_math")
    assert section["errors"] >= 1
    codes = {iss["code"] for iss in section["issues"]}
    assert "balance_mismatch" in codes or "net_payable_mismatch" in codes


def test_payment_allocation_check_detects_orphan_target(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    client_id = create_client_po_invoice(client, token)

    db = _open_session()
    try:
        pay = PaymentHistory(
            id="TEST-PAY-ORPHAN",
            client_id=client_id,
            date=datetime.date.today(),
            type="RECEIPT",
            amount=100.0,
            details="orphan test",
        )
        db.add(pay)
        db.flush()
        db.add(PaymentAllocation(
            payment_id=pay.id,
            alloc_type="invoice",
            target_inv_id="INV-DOES-NOT-EXIST",
            amount=100.0,
        ))
        db.commit()
    finally:
        db.close()

    report = _get_report(client, token)
    section = _section(report, "payment_allocations")
    assert section["errors"] >= 1
    codes = {iss["code"] for iss in section["issues"]}
    assert "invoice_target_missing" in codes


def test_payment_allocation_check_detects_over_allocation(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    client_id = create_client_po_invoice(client, token)

    db = _open_session()
    try:
        pay = PaymentHistory(
            id="TEST-PAY-OVER",
            client_id=client_id,
            date=datetime.date.today(),
            type="RECEIPT",
            amount=50.0,
            details="over alloc",
        )
        db.add(pay)
        db.flush()
        # Allocate more than the payment amount.
        db.add(PaymentAllocation(
            payment_id=pay.id,
            alloc_type="invoice",
            target_inv_id="INV-001",
            amount=80.0,
        ))
        db.commit()
    finally:
        db.close()

    report = _get_report(client, token)
    section = _section(report, "payment_allocations")
    codes = {iss["code"] for iss in section["issues"]}
    assert "over_allocation" in codes


def test_dispatch_coverage_check_detects_overdispatch(client: TestClient):
    token = login(client, "admin", "Admin@1234")
    create_client_po_invoice(client, token)

    db = _open_session()
    try:
        # Inflate a dispatch quantity well beyond the baseline ordered_qty.
        item = (
            db.query(InvoiceDispatchItem)
            .filter(InvoiceDispatchItem.description == "Brick A")
            .first()
        )
        if item:
            item.dispatched_qty = 9999.0
            db.commit()
    finally:
        db.close()

    report = _get_report(client, token)
    section = _section(report, "dispatch_coverage")
    codes = {iss["code"] for iss in section["issues"]}
    assert "over_dispatched" in codes
