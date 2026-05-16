"""Tests for invoice extraction normalization and the upload endpoint."""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main as app_module
from invoice_extract import (
    normalize_invoice_extract,
    parse_and_normalize_raw,
    strip_code_fences,
)
from models import Base, User


# ---------- Unit tests: normalize_invoice_extract ----------

def test_normalize_keeps_explicit_rate():
    payload = {
        "clientName": "ACME",
        "invNo": "INV-1",
        "items": [{"desc": "Bricks", "qty": 10, "uom": "MT", "rate": 100}],
    }
    norm, warnings = normalize_invoice_extract(payload)
    assert norm["clientName"] == "ACME"
    assert norm["invNo"] == "INV-1"
    assert norm["items"][0]["rate"] == 100.0
    assert warnings == []


def test_normalize_derives_rate_from_amount_and_qty():
    payload = {"items": [{"desc": "Castable", "qty": 5, "uom": "Bags", "amount": 250}]}
    norm, warnings = normalize_invoice_extract(payload)
    assert norm["items"][0]["rate"] == 50.0
    assert norm["items"][0]["line_amount"] == 250.0
    assert any("derived from line_amount" in w for w in warnings)


def test_normalize_warns_when_rate_missing_and_no_amount():
    payload = {"items": [{"desc": "Mystery item", "qty": 3, "uom": "Nos"}]}
    norm, warnings = normalize_invoice_extract(payload)
    assert norm["items"][0]["rate"] == 0.0
    assert any("missing rate" in w for w in warnings)


def test_normalize_handles_alternate_keys_and_strings():
    payload = {
        "client_name": "Vendor",
        "inv_no": "X1",
        "items": [
            {"description": "Item A", "quantity": "2", "unit": "Nos", "unitRate": "12.5"},
            {"item": "Item B", "qty": "1,000", "uom": "Kg", "price": "Rs. 10.00"},
        ],
    }
    norm, _ = normalize_invoice_extract(payload)
    assert norm["clientName"] == "Vendor"
    assert norm["invNo"] == "X1"
    assert norm["items"][0]["desc"] == "Item A"
    assert norm["items"][0]["qty"] == 2.0
    assert norm["items"][0]["rate"] == 12.5
    assert norm["items"][1]["qty"] == 1000.0
    assert norm["items"][1]["rate"] == 10.0


def test_normalize_skips_non_dict_items_and_handles_bad_payload():
    payload = {"items": ["not-an-object", {"desc": "ok", "qty": 1, "rate": 5}]}
    norm, warnings = normalize_invoice_extract(payload)
    assert len(norm["items"]) == 1
    assert any("was not an object" in w for w in warnings)

    norm2, warnings2 = normalize_invoice_extract("garbage")
    assert norm2["items"] == []
    assert any("was not a JSON object" in w for w in warnings2)


def test_strip_code_fences_handles_markdown_wrappers():
    raw = "```json\n{\"a\": 1}\n```"
    assert strip_code_fences(raw).strip() == '{"a": 1}'


def test_parse_and_normalize_raw_full_flow():
    raw = json.dumps(
        {
            "clientName": "ACME",
            "invNo": "INV-2",
            "items": [
                {"desc": "Brick", "qty": 4, "uom": "MT", "rate": 25},
                {"desc": "Mortar", "qty": 2, "uom": "Bags", "amount": 100},
            ],
        }
    )
    norm, warnings, parse_err = parse_and_normalize_raw(raw)
    assert parse_err is None
    assert norm["items"][0]["rate"] == 25.0
    assert norm["items"][1]["rate"] == 50.0
    assert any("derived from line_amount" in w for w in warnings)


def test_parse_and_normalize_raw_invalid_json():
    norm, warnings, parse_err = parse_and_normalize_raw("not json")
    assert norm is None
    assert parse_err and parse_err.startswith("json_decode_error")


# ---------- API tests: /api/upload-invoice with mocked Gemini ----------

@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    db_file = tmp_path / "extract_test.sqlite"
    test_engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    Base.metadata.create_all(bind=test_engine)

    monkeypatch.setattr(app_module, "engine", test_engine)
    monkeypatch.setattr(app_module, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(app_module, "DB_FILE_PATH", Path(db_file))
    monkeypatch.setattr(app_module, "_backup_thread_started", True)
    monkeypatch.setattr(app_module, "GEMINI_API_KEY", "test-key", raising=False)

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


def _login(client: TestClient, username: str = "admin", password: str = "Admin@1234") -> str:
    resp = client.post("/api/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _make_pdf_bytes(label: str = "stub") -> bytes:
    # Minimal valid-ish PDF header so MIME sniffing passes; content is irrelevant
    # because Gemini is mocked.
    return b"%PDF-1.4\n%" + label.encode() + b"\n%%EOF\n"


def test_upload_invoice_returns_parsed_and_warnings(monkeypatch, api_client):
    token = _login(api_client)

    fake_payload = {
        "clientName": "ACME",
        "invNo": "INV-API-1",
        "poNo": "PO-1",
        "lrNo": "LR-9",
        "date": "2026-04-01",
        "basic": 350.0,
        "items": [
            {"desc": "Bricks A", "qty": 10, "uom": "MT", "rate": 25},
            {"desc": "Bricks B", "qty": 4, "uom": "MT", "amount": 100},
        ],
    }
    fake_response = MagicMock()
    fake_response.text = json.dumps(fake_payload)

    fake_model = MagicMock()
    fake_model.generate_content.return_value = fake_response

    monkeypatch.setattr(app_module.genai, "GenerativeModel", lambda *a, **k: fake_model)

    files = {"invoice_pdf": ("test1.pdf", io.BytesIO(_make_pdf_bytes("a")), "application/pdf")}
    resp = api_client.post(
        "/api/upload-invoice",
        files=files,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert len(body["results"]) == 1
    result = body["results"][0]
    assert result["success"] is True
    assert result["raw_data"]
    assert result["parsed"] is not None
    parsed = result["parsed"]
    assert parsed["invNo"] == "INV-API-1"
    assert parsed["items"][0]["rate"] == 25.0
    # Second item rate derived from amount/qty.
    assert parsed["items"][1]["rate"] == 25.0
    assert any("derived from line_amount" in w for w in result["warnings"])


def test_upload_invoice_handles_invalid_json_payload(monkeypatch, api_client):
    token = _login(api_client)

    fake_response = MagicMock()
    fake_response.text = "this is not json"

    fake_model = MagicMock()
    fake_model.generate_content.return_value = fake_response

    monkeypatch.setattr(app_module.genai, "GenerativeModel", lambda *a, **k: fake_model)
    monkeypatch.setattr(app_module, "GEMINI_INVOICE_JSON_RETRIES", 0, raising=False)

    files = {"invoice_pdf": ("bad.pdf", io.BytesIO(_make_pdf_bytes("b")), "application/pdf")}
    resp = api_client.post(
        "/api/upload-invoice",
        files=files,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()["results"][0]
    assert result["success"] is False
    assert result["parsed"] is None
    assert result["parse_error"].startswith("json_decode_error")


def test_upload_invoice_uses_concurrency_for_multiple_files(monkeypatch, api_client):
    token = _login(api_client)

    fake_response = MagicMock()
    fake_response.text = json.dumps({"invNo": "X", "items": []})

    fake_model = MagicMock()
    fake_model.generate_content.return_value = fake_response

    monkeypatch.setattr(app_module.genai, "GenerativeModel", lambda *a, **k: fake_model)

    files = [
        ("invoice_pdf", ("a.pdf", io.BytesIO(_make_pdf_bytes("a")), "application/pdf")),
        ("invoice_pdf", ("b.pdf", io.BytesIO(_make_pdf_bytes("b")), "application/pdf")),
        ("invoice_pdf", ("c.pdf", io.BytesIO(_make_pdf_bytes("c")), "application/pdf")),
    ]
    resp = api_client.post(
        "/api/upload-invoice",
        files=files,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["results"]) == 3
    # Each file should have produced a parsed payload.
    assert all(r["parsed"] is not None for r in body["results"])
    # Gemini should have been called exactly once per file.
    assert fake_model.generate_content.call_count == 3


def test_upload_invoice_skips_gemini_on_duplicate_hash(monkeypatch, api_client):
    """Re-uploading the SAME PDF bytes must use the cache, not call Gemini again."""
    token = _login(api_client)

    fake_payload = {
        "clientName": "ACME",
        "invNo": "INV-DUP-1",
        "items": [{"desc": "Item", "qty": 1, "uom": "Nos", "rate": 10}],
    }
    fake_response = MagicMock()
    fake_response.text = json.dumps(fake_payload)

    fake_model = MagicMock()
    fake_model.generate_content.return_value = fake_response

    monkeypatch.setattr(app_module.genai, "GenerativeModel", lambda *a, **k: fake_model)

    pdf_bytes = _make_pdf_bytes("dup")

    # First upload: cold cache, Gemini should be called exactly once.
    resp1 = api_client.post(
        "/api/upload-invoice",
        files={"invoice_pdf": ("dup.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp1.status_code == 200, resp1.text
    body1 = resp1.json()
    assert body1["results"][0]["duplicate"] is False
    assert body1["results"][0]["parsed"]["invNo"] == "INV-DUP-1"
    assert fake_model.generate_content.call_count == 1

    # Second upload of identical bytes: must hit cache, NOT call Gemini.
    resp2 = api_client.post(
        "/api/upload-invoice",
        files={"invoice_pdf": ("dup.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    cached_result = body2["results"][0]
    assert cached_result["duplicate"] is True
    assert cached_result["parsed"]["invNo"] == "INV-DUP-1"
    assert "duplicate_upload_cache_hit" in cached_result["warnings"]
    assert cached_result["sha256"] == body1["results"][0]["sha256"]
    # Crucially, Gemini call count did NOT increase.
    assert fake_model.generate_content.call_count == 1


def test_upload_invoice_mixed_batch_only_calls_gemini_for_new_files(monkeypatch, api_client):
    """Batch with one cached file + one new file must call Gemini exactly once."""
    token = _login(api_client)

    fake_response = MagicMock()
    fake_response.text = json.dumps({"invNo": "MIX", "items": []})

    fake_model = MagicMock()
    fake_model.generate_content.return_value = fake_response

    monkeypatch.setattr(app_module.genai, "GenerativeModel", lambda *a, **k: fake_model)

    pdf_a = _make_pdf_bytes("aaa")
    pdf_b = _make_pdf_bytes("bbb")

    # Seed the cache with file A.
    api_client.post(
        "/api/upload-invoice",
        files={"invoice_pdf": ("a.pdf", io.BytesIO(pdf_a), "application/pdf")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert fake_model.generate_content.call_count == 1

    # Mixed batch: A (cached) + B (new). Only B should hit Gemini.
    resp = api_client.post(
        "/api/upload-invoice",
        files=[
            ("invoice_pdf", ("a.pdf", io.BytesIO(pdf_a), "application/pdf")),
            ("invoice_pdf", ("b.pdf", io.BytesIO(pdf_b), "application/pdf")),
        ],
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["results"]) == 2
    duplicates = [r for r in body["results"] if r.get("duplicate")]
    fresh = [r for r in body["results"] if not r.get("duplicate")]
    assert len(duplicates) == 1
    assert len(fresh) == 1
    assert fake_model.generate_content.call_count == 2  # one prior + one new
