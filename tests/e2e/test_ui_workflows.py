import pytest
import requests
from playwright.sync_api import expect

from tests.e2e.helpers_browser import ui_login_admin


@pytest.mark.e2e
def test_login_and_create_client_via_ui(page, live_server):
    ui_login_admin(page, live_server)
    page.fill("#newClientName", "E2E Client")
    page.click("button:has-text('Establish Account')")

    page.wait_for_timeout(1000)
    expect_target = page.locator("text=E2E Client")
    expect_target.first.wait_for(timeout=10000)


@pytest.mark.e2e
def test_ui_reflects_invoice_created_by_api(page, live_server):
    # Login via API first and create records for a realistic user journey.
    login = requests.post(
        f"{live_server}/api/login",
        json={"username": "admin", "password": "Admin@1234"},
        timeout=10,
    )
    assert login.status_code == 200, login.text
    token = login.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    c = requests.post(f"{live_server}/api/clients", json={"name": "UI Sync Co"}, headers=headers, timeout=10)
    assert c.status_code == 200, c.text
    client_id = c.json()["id"]

    po_payload = {
        "client_id": client_id,
        "po_no": "PO-E2E-1",
        "contact_person": "E2E",
        "project_name": "Browser Sync",
        "adv_pct": 0.0,
        "ret_pct": 0.0,
        "ret_base": "total",
        "tds_enabled": False,
        "tds_rate": 0.0,
        "tds_threshold": 0.0,
        "baseline_items": [{"description": "Brick A", "ordered_qty": 2, "inspected_qty": 0, "uom": "Nos", "material_type": "brick"}],
    }
    po = requests.post(f"{live_server}/api/purchase-orders", json=po_payload, headers=headers, timeout=10)
    assert po.status_code == 200, po.text

    inv_payload = {
        "client_id": client_id,
        "po_no": "PO-E2E-1",
        "invoice_no": "INV-E2E-1",
        "sub_entity": "Plant",
        "lr_no": "LR-E2E",
        "inv_date": "2026-04-01",
        "due_date": "2026-04-10",
        "basic": 500,
        "gst": 90,
        "total": 590,
        "advance_adj": 0,
        "tds_ded": 0,
        "retention_held": 0,
        "net_payable": 0,
        "paid": 0,
        "balance": 0,
        "is_note": False,
        "note_type": None,
        "note_reason": None,
        "dispatch_items": [{"description": "Brick A", "qty": 2, "inspected_qty": 0, "uom": "Nos"}],
    }
    inv = requests.post(f"{live_server}/api/invoices", json=inv_payload, headers=headers, timeout=10)
    assert inv.status_code == 200, inv.text

    ui_login_admin(page, live_server)
    page.locator("text=UI Sync Co").first.wait_for(timeout=15000)
    page.click("text=UI Sync Co")
    page.wait_for_timeout(1000)
    assert "UI Sync Co" in page.locator("#topbarTitle").inner_text()


def _amount_from_ui(text: str) -> float:
    cleaned = "".join(ch for ch in (text or "") if ch.isdigit() or ch in ".-")
    return float(cleaned or 0.0)


def _set_po_filter(page, po_values: list[str]) -> None:
    page.evaluate(
        """(vals) => {
            const checked = new Set((vals || []).map(String));
            document.querySelectorAll('#poSelectOptions input[type="checkbox"]').forEach((cb) => {
                const should = checked.has(String(cb.value || ''));
                cb.checked = should;
                cb.dispatchEvent(new Event('change', { bubbles: true }));
            });
            if (window.triggerUpdate) window.triggerUpdate();
        }""",
        po_values,
    )


@pytest.mark.e2e
def test_unallocated_applet_delete_updates_po_scoped_panel(page, live_server, e2e_api_admin_headers):
    client_name = "UA Panel Co"
    c = requests.post(
        f"{live_server}/api/clients",
        json={"name": client_name},
        headers=e2e_api_admin_headers,
        timeout=10,
    )
    assert c.status_code == 200, c.text
    client_id = c.json()["id"]

    for po_no in ("PO-UA-1", "PO-UA-2"):
        po_payload = {
            "client_id": client_id,
            "po_no": po_no,
            "contact_person": "E2E",
            "project_name": f"Proj-{po_no}",
            "adv_pct": 0.0,
            "ret_pct": 0.0,
            "ret_base": "total",
            "tds_enabled": False,
            "tds_rate": 0.0,
            "tds_threshold": 0.0,
            "baseline_items": [{"description": "Brick A", "ordered_qty": 2, "inspected_qty": 0, "uom": "Nos", "material_type": "brick"}],
        }
        po = requests.post(f"{live_server}/api/purchase-orders", json=po_payload, headers=e2e_api_admin_headers, timeout=10)
        assert po.status_code == 200, po.text

    inv_seed = [
        ("INV-UA-1", "PO-UA-1", 1000),
        ("INV-UA-2", "PO-UA-2", 900),
    ]
    for inv_no, po_no, total in inv_seed:
        inv_payload = {
            "client_id": client_id,
            "po_no": po_no,
            "invoice_no": inv_no,
            "sub_entity": "Plant",
            "lr_no": f"LR-{inv_no}",
            "inv_date": "2026-04-01",
            "due_date": "2026-04-10",
            "basic": total,
            "gst": 0,
            "total": total,
            "advance_adj": 0,
            "tds_ded": 0,
            "retention_held": 0,
            "net_payable": 0,
            "paid": 0,
            "balance": 0,
            "is_note": False,
            "note_type": None,
            "note_reason": None,
            "dispatch_items": [{"description": "Brick A", "qty": 2, "inspected_qty": 0, "uom": "Nos"}],
        }
        inv = requests.post(f"{live_server}/api/invoices", json=inv_payload, headers=e2e_api_admin_headers, timeout=10)
        assert inv.status_code == 200, inv.text

    # Create two receipts with unallocated tails: 200 for PO-UA-1, 150 for PO-UA-2.
    payments = [
        ("PAY-UA-1", "INV-UA-1", 300, 100),
        ("PAY-UA-2", "INV-UA-2", 250, 100),
    ]
    for pay_id, inv_no, amount, alloc_amt in payments:
        alloc_payload = {
            "client_id": client_id,
            "id": pay_id,
            "date": "2026-04-12",
            "amount": amount,
            "note": "e2e unallocated seed",
            "mode": "targeted",
            "targets": [{"inv_id": inv_no, "amount": alloc_amt}],
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
        pay = requests.post(f"{live_server}/api/payments/allocate", json=alloc_payload, headers=e2e_api_admin_headers, timeout=10)
        assert pay.status_code == 200, pay.text

    ui_login_admin(page, live_server)
    page.locator(f"text={client_name}").first.click()
    page.wait_for_timeout(1200)

    # Filter to PO-UA-1 and confirm unallocated panel shows scoped amount (200).
    _set_po_filter(page, ["PO-UA-1"])
    panel = page.locator("#unallocatedUI")
    expect(panel).to_be_visible(timeout=15000)
    bal_before = _amount_from_ui(page.locator("#lblUnallocatedBal").inner_text())
    assert bal_before == pytest.approx(200.0, abs=0.01)

    # Remove one unallocated entry from applet and verify panel updates.
    page.click("button:has-text('Unallocated Applet')")
    expect(page.locator("#unallocatedAppletModal")).to_be_visible(timeout=10000)
    page.select_option("#unallocatedFilter", "PO-UA-1")
    page.locator("#unallocatedAppletBody button:has-text('Remove')").first.click()
    page.click("#appletConfirm")
    page.wait_for_timeout(1200)

    # Re-apply PO-UA-1 filter and verify the scoped panel is hidden (no remaining unallocated for PO-UA-1).
    _set_po_filter(page, ["PO-UA-1"])
    page.wait_for_timeout(800)
    expect(panel).to_be_hidden(timeout=10000)

    # Switch to PO-UA-2 and verify panel still shows remaining unallocated amount (150).
    _set_po_filter(page, ["PO-UA-2"])
    expect(panel).to_be_visible(timeout=10000)
    bal_po2 = _amount_from_ui(page.locator("#lblUnallocatedBal").inner_text())
    assert bal_po2 == pytest.approx(150.0, abs=0.01)

