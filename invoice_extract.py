"""Helpers for AI-extracted invoice payloads.

Responsibilities:
  * Strip accidental Markdown fences from model output.
  * Validate the parsed structure with Pydantic.
  * Normalize the result so every line item has a canonical numeric ``rate``.

Kept deliberately framework-light so it is easy to unit test without spinning
up FastAPI or a database.
"""

from __future__ import annotations

import json
import re
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field


_FENCE_RE = re.compile(r"```(?:json)?", re.IGNORECASE)


# Synonyms commonly emitted by LLMs for the same logical field.
_RATE_KEYS = ("rate", "unitRate", "unit_rate", "rate_per_uom", "ratePerUom", "price")
_LINE_AMOUNT_KEYS = (
    "line_amount",
    "lineAmount",
    "amount",
    "taxable_value",
    "taxableValue",
    "value",
)
_QTY_KEYS = ("qty", "quantity", "qnty", "qty_uom", "qtyUom")
_DESC_KEYS = ("desc", "description", "particulars", "item", "name")
_UOM_KEYS = ("uom", "unit", "units")


class ExtractedInvoiceItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    desc: str = ""
    qty: float = 0.0
    uom: str = "Nos"
    rate: float = 0.0
    line_amount: Optional[float] = Field(default=None)


class ExtractedInvoice(BaseModel):
    model_config = ConfigDict(extra="ignore")

    clientName: str = ""
    invNo: str = ""
    poNo: str = ""
    lrNo: str = ""
    date: str = ""
    basic: float = 0.0
    items: List[ExtractedInvoiceItem] = Field(default_factory=list)


def strip_code_fences(raw: str) -> str:
    """Remove ``` or ```json fences a model may emit despite JSON-mode."""

    if not raw:
        return ""
    cleaned = _FENCE_RE.sub("", raw).strip()
    return cleaned


_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?")


def _coerce_float(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return 0.0
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        # Pull out the first number-shaped substring; tolerates "Rs. 1,234.50",
        # "INR 10.00", "10.5 / MT", etc.
        match = _NUMBER_RE.search(text)
        if not match:
            return 0.0
        try:
            return float(match.group(0).replace(",", ""))
        except Exception:
            return 0.0
    return 0.0


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _pick_first(d: dict, keys: tuple[str, ...], coercer) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return coercer(d[k])
    return coercer(None) if coercer is _coerce_float else ""


def normalize_invoice_extract(payload: Any) -> tuple[dict, list[str]]:
    """Return ``(normalized_dict, warnings)``.

    The normalized dict has exactly the shape the rest of the app expects:
    ``clientName``, ``invNo``, ``poNo``, ``lrNo``, ``date``, ``basic`` and an
    ``items`` list whose entries always include ``desc``, ``qty``, ``uom``,
    ``rate`` (numeric) and optional ``line_amount``.

    ``rate`` is filled using this priority order:
      1. Explicit rate / unitRate / rate_per_uom in the item.
      2. ``line_amount / qty`` when both are usable.
      3. Otherwise ``0.0`` and a warning is emitted (we never invent rates).
    """

    warnings: list[str] = []

    if not isinstance(payload, dict):
        warnings.append("payload was not a JSON object; coerced to empty invoice")
        payload = {}

    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        if raw_items is not None:
            warnings.append("items was not a list; defaulted to empty list")
        raw_items = []

    norm_items: list[dict] = []
    for idx, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict):
            warnings.append(f"item[{idx}] was not an object; skipped")
            continue

        desc = _pick_first(raw_item, _DESC_KEYS, _coerce_str)
        qty = _pick_first(raw_item, _QTY_KEYS, _coerce_float)
        uom = _pick_first(raw_item, _UOM_KEYS, _coerce_str) or "Nos"
        rate = _pick_first(raw_item, _RATE_KEYS, _coerce_float)
        line_amount_val = _pick_first(raw_item, _LINE_AMOUNT_KEYS, _coerce_float)

        if rate <= 0 and line_amount_val > 0 and qty > 0:
            rate = line_amount_val / qty
            warnings.append(
                f"item[{idx}] rate derived from line_amount/qty (no explicit rate)"
            )
        elif rate <= 0 and (qty > 0 or desc):
            warnings.append(
                f"item[{idx}] missing rate; left as 0 to avoid inventing a value"
            )

        norm_items.append(
            {
                "desc": desc,
                "qty": qty,
                "uom": uom,
                "rate": float(rate or 0.0),
                "line_amount": float(line_amount_val) if line_amount_val else None,
            }
        )

    normalized = {
        "clientName": _coerce_str(payload.get("clientName") or payload.get("client_name")),
        "invNo": _coerce_str(payload.get("invNo") or payload.get("inv_no") or payload.get("invoice_no")),
        "poNo": _coerce_str(payload.get("poNo") or payload.get("po_no")),
        "lrNo": _coerce_str(payload.get("lrNo") or payload.get("lr_no")),
        "date": _coerce_str(payload.get("date") or payload.get("inv_date") or payload.get("invoiceDate")),
        "basic": _coerce_float(payload.get("basic") or payload.get("taxable_total")),
        "items": norm_items,
    }

    return normalized, warnings


def parse_and_normalize_raw(raw_text: str) -> tuple[Optional[dict], list[str], Optional[str]]:
    """Parse model output into a normalized invoice dict.

    Returns ``(normalized_or_None, warnings, parse_error_or_None)``. The
    function never raises for malformed payloads; the error string is returned
    so callers can decide whether to surface it to the user / log it.
    """

    cleaned = strip_code_fences(raw_text or "")
    if not cleaned:
        return None, [], "empty model response"

    try:
        loaded = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        return None, [], f"json_decode_error: {exc.msg}"

    # Normalize directly from the raw loaded JSON so alternate keys (e.g. amount,
    # price, unitRate) are recognized. Pydantic is then run as a soft sanity
    # check on the canonical shape and is non-fatal.
    normalized, warnings = normalize_invoice_extract(loaded if isinstance(loaded, dict) else {})

    try:
        ExtractedInvoice.model_validate(normalized)
    except Exception as exc:
        warnings.append(f"strict_validation_warning: {exc}")

    return normalized, warnings, None
