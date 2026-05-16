"""Admin UI navigation across major workspaces (portfolio, ledger, reports, IAM, audit, recon)."""

import pytest
from playwright.sync_api import expect

from tests.e2e.helpers_browser import ui_login_admin


@pytest.mark.e2e
def test_admin_portfolio_and_global_ledger(page, live_server: str):
    ui_login_admin(page, live_server)
    expect(page.locator("#landingView")).to_be_visible()
    expect(page.locator("#landingSectionTitle")).to_be_visible()
    assert "account" in page.locator("#landingSectionTitle").inner_text().lower()
    expect(page.locator("#topbarTitle")).to_contain_text("Portfolio")

    page.get_by_text("Global Master Ledger", exact=True).click()
    page.wait_for_timeout(500)
    expect(page.locator("#topbarTitle")).to_contain_text("Global Master Ledger")
    expect(page.locator("#clientView")).to_be_visible()


@pytest.mark.e2e
def test_admin_analytics_reports_panel(page, live_server: str):
    ui_login_admin(page, live_server)
    page.get_by_text("Analytics & Reports", exact=True).click()
    page.wait_for_timeout(500)
    expect(page.locator("#reportsView")).to_be_visible()
    expect(page.locator("#topbarTitle")).to_contain_text("Executive Analytics")


@pytest.mark.e2e
def test_admin_identity_access_panel(page, live_server: str):
    ui_login_admin(page, live_server)
    page.get_by_text("Identity Access Mgt.", exact=True).click()
    page.wait_for_timeout(500)
    expect(page.locator("#adminView")).to_be_visible()
    expect(page.get_by_role("heading", name="Provision New Access")).to_be_visible()


@pytest.mark.e2e
def test_admin_audit_log_view(page, live_server: str):
    ui_login_admin(page, live_server)
    page.get_by_text("Audit Log", exact=True).click()
    page.wait_for_timeout(500)
    expect(page.locator("#auditLogView")).to_be_visible()
    expect(page.get_by_role("heading", name="Audit Log Filters")).to_be_visible()


@pytest.mark.e2e
def test_admin_reconciliation_view(page, live_server: str):
    ui_login_admin(page, live_server)
    page.get_by_text("Reconciliation", exact=True).click()
    page.wait_for_timeout(500)
    expect(page.locator("#reconciliationView")).to_be_visible()
    expect(page.locator("#reconciliationView").locator("h3.section-title").first).to_contain_text("Financial Reconciliation")
