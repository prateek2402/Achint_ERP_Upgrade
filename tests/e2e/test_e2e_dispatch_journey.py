"""End-to-end: seed dispatch data via API, open dispatch reconciliation from ledger."""

from __future__ import annotations

import uuid

import pytest
import requests
from playwright.sync_api import expect

from tests.e2e.helpers_browser import ui_login_admin


def _seed_client_po_invoice(base_url: str, headers: dict) -> tuple[str, str, str]:
    """Return (client_name, po_no, invoice_no)."""
    slug = uuid.uuid4().hex[:10]
    cname = f"E2E DSP {slug}"
    c = requests.post(f"{base_url}/api/clients", json={"name": cname}, headers=headers, timeout=15)
    assert c.status_code == 200, c.text
    oid = c.json()["id"]
    po_no = f"PO-DSP-{slug}"
    invoice_no = f"INV-E2E-DSP-{slug}"

    payload_po = {
        "client_id": oid,
        "po_no": po_no,
        "contact_person": "DSP",
        "project_name": "Kiln A",
        "adv_pct": 0.0,
        "ret_pct": 0.0,
        "ret_base": "total",
        "tds_enabled": False,
        "tds_rate": 0.0,
        "tds_threshold": 0.0,
        "baseline_items": [
            {
                "description": "E2E Brick Line",
                "ordered_qty": 100,
                "inspected_qty": 0,
                "uom": "Nos",
                "material_type": "brick",
            },
        ],
    }
    po = requests.post(f"{base_url}/api/purchase-orders", json=payload_po, headers=headers, timeout=15)
    assert po.status_code == 200, po.text

    inv_payload = {
        "client_id": oid,
        "po_no": po_no,
        "invoice_no": invoice_no,
        "sub_entity": "Unit-1",
        "lr_no": "LR-DSP",
        "inv_date": "2026-04-05",
        "due_date": "2026-04-20",
        "basic": 1000,
        "gst": 180,
        "total": 1180,
        "advance_adj": 0,
        "tds_ded": 0,
        "retention_held": 0,
        "net_payable": 0,
        "paid": 500,
        "balance": 0,
        "is_note": False,
        "note_type": None,
        "note_reason": None,
        "dispatch_items": [
            {"description": "E2E Brick Line", "qty": 12.5, "inspected_qty": 0, "uom": "Nos"},
        ],
    }
    inv = requests.post(f"{base_url}/api/invoices", json=inv_payload, headers=headers, timeout=15)
    assert inv.status_code == 200, inv.text
    return cname, po_no, invoice_no


@pytest.mark.e2e
def test_admin_dispatch_statement_via_client_ledger(page, live_server: str, e2e_api_admin_headers: dict):
    client_name, po_no, invoice_no = _seed_client_po_invoice(live_server, e2e_api_admin_headers)

    ui_login_admin(page, live_server)

    client_row = page.locator(".nav-item.sub-nav-item", has_text=client_name).first
    client_row.click()
    page.wait_for_timeout(800)
    expect(page.locator("#clientView")).to_be_visible()
    expect(page.locator("#topbarTitle")).to_contain_text(client_name)

    page.get_by_role("button", name="Open Dispatch Statement").click()

    page.wait_for_selector("#logisticsClientPageView", state="visible", timeout=15000)
    page.wait_for_timeout(600)
    expect(page.locator("#logisticsClientPageView")).to_be_visible()

    page.get_by_role("button", name="Open Statement").first.click(timeout=15000)

    page.wait_for_selector("#dispatchView", state="visible", timeout=20000)
    expect(page.locator("#dispatchView")).to_be_visible()
    expect(page.locator("#dispatchPoSelect")).to_contain_text(po_no)
    page.wait_for_timeout(1200)

    expect(page.locator("#dispatchView")).to_contain_text(invoice_no, timeout=25000)
    expect(page.locator("#dispatchStatInvoices")).to_have_text("1")


@pytest.mark.e2e
def test_dispatch_export_excel_picker_can_cancel(page, live_server: str, e2e_api_admin_headers: dict):
    client_name, _, _invoice_no = _seed_client_po_invoice(live_server, e2e_api_admin_headers)

    ui_login_admin(page, live_server)

    page.locator(".nav-item.sub-nav-item", has_text=client_name).first.click()
    page.wait_for_timeout(800)
    page.get_by_role("button", name="Open Dispatch Statement").click()
    page.wait_for_selector("#logisticsClientPageView", state="visible", timeout=15000)
    page.wait_for_timeout(400)
    page.get_by_role("button", name="Open Statement").first.click(timeout=15000)
    page.wait_for_selector("#dispatchView", state="visible", timeout=20000)

    page.get_by_role("button", name="Export Excel").click()
    page.wait_for_selector("#customAppletOverlay", state="visible", timeout=5000)
    expect(page.locator("#appletTitle")).to_contain_text("Export dispatch")
    page.locator("#appletCancel").click()
    expect(page.locator("#customAppletOverlay")).to_be_hidden(timeout=5000)


@pytest.mark.e2e
def test_dispatch_export_xlsx_layouts_smoke_http(live_server: str, e2e_api_admin_headers: dict):
    client_name, po_no, _ = _seed_client_po_invoice(live_server, e2e_api_admin_headers)
    headers = {"Authorization": e2e_api_admin_headers["Authorization"]}

    for layout in ("consolidated", "invoice_wise"):
        params = {"client": client_name, "po_no": po_no, "layout": layout}
        r = requests.get(f"{live_server}/api/dispatch/export-xlsx", headers=headers, params=params, timeout=30)
        assert r.status_code == 200, r.text
        ct = r.headers.get("content-type") or ""
        assert "spreadsheetml" in ct.lower() or "octet-stream" in ct.lower() or ".sheet" in ct
        assert len(r.content) > 300, len(r.content)
