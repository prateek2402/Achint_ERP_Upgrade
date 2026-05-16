"""Shared Playwright helpers for ERP UI tests."""

from __future__ import annotations


def ui_login_admin(page, base_url: str, username: str = "admin", password: str = "Admin@1234") -> None:
    page.goto(f"{base_url}/")
    page.fill("#loginUser", username)
    page.fill("#loginPass", password)
    page.click("button:has-text('Secure Sign In')")
    page.wait_for_selector("#loginOverlay", state="hidden", timeout=20_000)
