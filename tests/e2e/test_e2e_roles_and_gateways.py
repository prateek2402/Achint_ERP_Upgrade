"""Logistics dashboard + standard-user permission smoke tests."""

from __future__ import annotations

import uuid

import pytest
import requests
from playwright.sync_api import expect

def ui_login_any(page, base_url: str, username: str, password: str) -> None:
    page.goto(f"{base_url}/")
    page.fill("#loginUser", username)
    page.fill("#loginPass", password)
    page.click("button:has-text('Secure Sign In')")
    page.wait_for_selector("#loginOverlay", state="hidden", timeout=20_000)


def _seed_dispatch_row(base_url: str, headers: dict) -> str:
    """Create one client/po/invoice so logistics dispatch-summary has a row. Returns client name."""
    cid = uuid.uuid4().hex[:10]
    cname = f"E2E Logi Cli {cid}"
    c = requests.post(f"{base_url}/api/clients", json={"name": cname}, headers=headers, timeout=15)
    assert c.status_code == 200, c.text
    oid = c.json()["id"]
    po_no = f"PO-LOG-{cid}"
    payload_po = {
        "client_id": oid,
        "po_no": po_no,
        "contact_person": "Q",
        "project_name": "P",
        "adv_pct": 0.0,
        "ret_pct": 0.0,
        "ret_base": "total",
        "tds_enabled": False,
        "tds_rate": 0.0,
        "tds_threshold": 0.0,
        "baseline_items": [
            {
                "description": "Logistics Brick",
                "ordered_qty": 50,
                "inspected_qty": 0,
                "uom": "Nos",
                "material_type": "brick",
            },
        ],
    }
    requests.post(f"{base_url}/api/purchase-orders", json=payload_po, headers=headers, timeout=15).raise_for_status()
    inv_payload = {
        "client_id": oid,
        "po_no": po_no,
        "invoice_no": f"INV-LOG-{cid}",
        "sub_entity": "U",
        "lr_no": "LR-L",
        "inv_date": "2026-04-06",
        "due_date": "2026-04-18",
        "basic": 200,
        "gst": 36,
        "total": 236,
        "advance_adj": 0,
        "tds_ded": 0,
        "retention_held": 0,
        "net_payable": 0,
        "paid": 100,
        "balance": 0,
        "is_note": False,
        "note_type": None,
        "note_reason": None,
        "dispatch_items": [
            {"description": "Logistics Brick", "qty": 5, "inspected_qty": 0, "uom": "Nos"},
        ],
    }
    requests.post(f"{base_url}/api/invoices", json=inv_payload, headers=headers, timeout=15).raise_for_status()
    return cname


@pytest.mark.e2e
def test_login_ui_shows_error_for_bad_credentials(page, live_server: str):
    page.goto(f"{live_server}/")
    page.fill("#loginUser", "admin")
    page.fill("#loginPass", "DefinitelyNotTheBootstrapPassword!")
    page.click("button:has-text('Secure Sign In')")
    page.wait_for_timeout(1200)
    expect(page.locator("#loginOverlay")).to_be_visible()
    expect(page.locator("#loginError")).not_to_have_text("")


@pytest.mark.e2e
def test_standard_user_sidebar_hides_admin_items(page, live_server: str, e2e_extra_users):
    ui_login_any(page, live_server, "e2e_user", "User@12345")

    sidebar = page.locator("#mainSidebar")
    expect(sidebar.get_by_text("Identity Access Mgt.", exact=False)).to_have_count(0)
    expect(sidebar.get_by_text("Audit Log", exact=False)).to_have_count(0)
    expect(sidebar.get_by_text("Reconciliation", exact=False)).to_have_count(0)
    expect(sidebar.get_by_text("Global Master Ledger", exact=False)).to_have_count(1)


@pytest.mark.e2e
def test_logistics_role_sees_dispatch_console_and_rows(
    page, live_server: str, e2e_extra_users, e2e_api_admin_headers: dict
):
    cname = _seed_dispatch_row(live_server, e2e_api_admin_headers)

    ui_login_any(page, live_server, "e2e_logi", "Logi@12345")

    expect(page.locator("#logisticsLandingView")).to_be_visible(timeout=15000)
    expect(page.locator("#topbarTitle")).to_contain_text("Logistics")

    expect(page.locator("#mainSidebar").get_by_text("Dispatch Dashboard", exact=False)).to_have_count(1)
    page.wait_for_timeout(1500)
    expect(page.locator("#globalDispatchBody")).to_contain_text(cname, timeout=25_000)
