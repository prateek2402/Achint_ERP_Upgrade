"""Read-only reconciliation checks for the ERP database.

Each check is a small pure function that takes a SQLAlchemy ``Session`` and
returns a list of ``Issue`` dicts. Checks must NEVER mutate the database; they
exist only to surface inconsistencies to a finance reviewer.

Issue shape::

    {
        "entity_type": "invoice" | "payment" | "purchase_order" | "note_allocation",
        "entity_id":   "INV-001" | "12345" | ...,
        "code":        "balance_mismatch" | "over_allocation" | ... ,
        "severity":    "warning" | "error",
        "message":     "Human-readable summary",
        "expected":    <number or dict, optional>,
        "actual":      <number or dict, optional>,
        "context":     {...optional extras...},
    }

The endpoint in ``main.py`` consumes this as a dict of named sections so the
UI can render one card per check with drill-downs.
"""

from __future__ import annotations

from typing import Iterable
from collections import defaultdict

from sqlalchemy.orm import Session

from models import (
    Invoice,
    PaymentHistory,
    PaymentAllocation,
    PurchaseOrder,
    PoBaselineItem,
    InvoiceDispatchItem,
)


MONEY_TOL = 0.05  # rupees of slack for rounding effects
QTY_TOL = 0.01


def _money_close(a: float, b: float) -> bool:
    return abs(float(a or 0.0) - float(b or 0.0)) <= MONEY_TOL


def check_invoice_math(db: Session) -> list[dict]:
    """Persisted invoice fields should follow the documented formulas.

    For each invoice that is NOT a credit/debit note:
      * expected_net_payable = max(0, total - advance_adj - tds_ded)
        (retention_held is informational; it does not reduce net payable or balance.)
      * expected_balance     = max(0, expected_net_payable - paid)

    Tiny residuals (< INR 5) are NOT auto-zeroed in the recalculation engine
    any more; the UI marks them as CLEARED visually but persists the actual
    figure, so this check is straightforward equality within MONEY_TOL.
    """

    issues: list[dict] = []
    invoices = db.query(Invoice).filter(Invoice.is_note.is_(False)).all()
    for inv in invoices:
        total = float(inv.total or 0.0)
        adv = float(inv.advance_adj or 0.0)
        tds = float(inv.tds_ded or 0.0)
        ret = float(inv.retention_held or 0.0)
        paid = float(inv.paid or 0.0)
        np_persisted = float(inv.net_payable or 0.0)
        bal_persisted = float(inv.balance or 0.0)

        np_expected = max(0.0, total - adv - tds)
        bal_expected = max(0.0, np_expected - paid)
        bal_acceptable = _money_close(bal_persisted, bal_expected)

        if not _money_close(np_persisted, np_expected):
            issues.append({
                "entity_type": "invoice",
                "entity_id": inv.invoice_no,
                "code": "net_payable_mismatch",
                "severity": "error",
                "message": (
                    f"Invoice {inv.invoice_no}: net_payable {np_persisted:.2f} "
                    f"differs from expected {np_expected:.2f}"
                ),
                "expected": round(np_expected, 2),
                "actual": round(np_persisted, 2),
                "context": {
                    "client_id": inv.client_id,
                    "total": total, "advance_adj": adv, "tds_ded": tds, "retention_held": ret,
                },
            })
        if not bal_acceptable:
            issues.append({
                "entity_type": "invoice",
                "entity_id": inv.invoice_no,
                "code": "balance_mismatch",
                "severity": "error",
                "message": (
                    f"Invoice {inv.invoice_no}: balance {bal_persisted:.2f} differs from "
                    f"expected {bal_expected:.2f} (paid={paid:.2f})"
                ),
                "expected": round(bal_expected, 2),
                "actual": round(bal_persisted, 2),
                "context": {"client_id": inv.client_id, "paid": paid},
            })
        if paid > np_persisted + MONEY_TOL:
            issues.append({
                "entity_type": "invoice",
                "entity_id": inv.invoice_no,
                "code": "overpaid",
                "severity": "warning",
                "message": (
                    f"Invoice {inv.invoice_no}: paid {paid:.2f} exceeds net_payable {np_persisted:.2f}"
                ),
                "expected": round(np_persisted, 2),
                "actual": round(paid, 2),
                "context": {"client_id": inv.client_id},
            })
    return issues


# Allocation types whose amounts should be considered "consumed" against the payment.
_CONSUMING_ALLOC_TYPES = (
    "invoice", "po_advance", "po_advance_applied", "note_allocation", "unallocated_reversal",
)


def check_payment_allocations(db: Session) -> list[dict]:
    """Surface payment-vs-allocation problems.

    1. Sum of consuming allocations must not exceed the payment amount.
    2. Allocations with ``alloc_type='invoice'`` must point at a real invoice.
    3. Allocations with ``alloc_type='note_allocation'`` must reference an
       existing note (invoice_no with ``is_note=True``) via ``note_id``.
    """

    issues: list[dict] = []
    payments = db.query(PaymentHistory).all()
    invoices = db.query(Invoice).all()
    inv_no_set = {i.invoice_no for i in invoices}
    note_no_set = {i.invoice_no for i in invoices if bool(i.is_note)}

    for pay in payments:
        allocs = db.query(PaymentAllocation).filter(PaymentAllocation.payment_id == pay.id).all()
        consumed = sum(float(a.amount or 0.0) for a in allocs if a.alloc_type in _CONSUMING_ALLOC_TYPES)
        amount = float(pay.amount or 0.0)
        if consumed > amount + MONEY_TOL:
            issues.append({
                "entity_type": "payment",
                "entity_id": pay.id,
                "code": "over_allocation",
                "severity": "error",
                "message": (
                    f"Payment {pay.id}: allocations total {consumed:.2f} exceed payment amount {amount:.2f}"
                ),
                "expected": round(amount, 2),
                "actual": round(consumed, 2),
                "context": {"client_id": pay.client_id, "type": pay.type},
            })
        for al in allocs:
            if al.alloc_type == "invoice" and al.target_inv_id and al.target_inv_id not in inv_no_set:
                issues.append({
                    "entity_type": "payment_allocation",
                    "entity_id": str(al.id),
                    "code": "invoice_target_missing",
                    "severity": "error",
                    "message": (
                        f"Allocation {al.id} on payment {pay.id} targets unknown invoice {al.target_inv_id}"
                    ),
                    "context": {"payment_id": pay.id, "target_inv_id": al.target_inv_id},
                })
            if al.alloc_type == "note_allocation" and al.note_id and al.note_id not in note_no_set:
                issues.append({
                    "entity_type": "payment_allocation",
                    "entity_id": str(al.id),
                    "code": "note_target_missing",
                    "severity": "error",
                    "message": (
                        f"Note allocation {al.id} on payment {pay.id} references unknown note {al.note_id}"
                    ),
                    "context": {"payment_id": pay.id, "note_id": al.note_id},
                })
    return issues


def _norm_desc(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def check_dispatch_coverage(db: Session) -> list[dict]:
    """Flag POs where total dispatched quantity exceeds the ordered baseline.

    Matches dispatch items to baseline rows by normalized description; honours
    ``dispatch_alias`` (when a baseline row defines an alias, dispatch items
    sharing the alias also count toward that baseline's coverage).
    """

    issues: list[dict] = []
    pos = db.query(PurchaseOrder).all()
    for po in pos:
        baselines = db.query(PoBaselineItem).filter(PoBaselineItem.po_id == po.id).all()
        if not baselines:
            continue
        # Build a mapping from a "canonical key" -> baseline row.
        baseline_by_key: dict[str, PoBaselineItem] = {}
        for b in baselines:
            baseline_by_key[_norm_desc(b.description)] = b
            if b.dispatch_alias:
                baseline_by_key[_norm_desc(b.dispatch_alias)] = b

        # Sum dispatched quantities per canonical key across all invoices for this PO.
        sums: dict[str, float] = defaultdict(float)
        invoices = db.query(Invoice).filter(Invoice.po_id == po.id).all()
        for inv in invoices:
            for d in db.query(InvoiceDispatchItem).filter(InvoiceDispatchItem.invoice_id == inv.id).all():
                key = _norm_desc(d.description)
                if key in baseline_by_key:
                    sums[key] += float(d.dispatched_qty or 0.0)
                else:
                    issues.append({
                        "entity_type": "purchase_order",
                        "entity_id": po.po_no,
                        "code": "dispatch_without_baseline",
                        "severity": "warning",
                        "message": (
                            f"PO {po.po_no}: dispatch line '{d.description}' on invoice "
                            f"{inv.invoice_no} has no matching baseline row"
                        ),
                        "context": {
                            "invoice_no": inv.invoice_no,
                            "qty": float(d.dispatched_qty or 0.0),
                            "uom": d.uom,
                        },
                    })

        for key, baseline in baseline_by_key.items():
            ordered = float(baseline.ordered_qty or 0.0)
            dispatched = sums.get(key, 0.0)
            if ordered > 0 and dispatched > ordered + QTY_TOL:
                issues.append({
                    "entity_type": "purchase_order",
                    "entity_id": po.po_no,
                    "code": "over_dispatched",
                    "severity": "error",
                    "message": (
                        f"PO {po.po_no} / '{baseline.description}': dispatched {dispatched:.2f} "
                        f"exceeds ordered {ordered:.2f}"
                    ),
                    "expected": round(ordered, 2),
                    "actual": round(dispatched, 2),
                    "context": {
                        "po_no": po.po_no,
                        "description": baseline.description,
                        "uom": baseline.uom,
                    },
                })
    return issues


def check_note_linkage(db: Session) -> list[dict]:
    """Surface notes whose linkage looks suspicious.

    For each invoice that IS a note (``is_note=True``):
      * Notes are allowed to be standalone (no target). We do not flag those.
      * If multiple ``note_allocation`` rows exist with the same ``note_id``
        we flag duplicate linkage.
    """

    issues: list[dict] = []
    notes = db.query(Invoice).filter(Invoice.is_note.is_(True)).all()
    for note in notes:
        allocs = (
            db.query(PaymentAllocation)
            .filter(PaymentAllocation.note_id == note.invoice_no)
            .all()
        )
        if len(allocs) > 1:
            issues.append({
                "entity_type": "note",
                "entity_id": note.invoice_no,
                "code": "duplicate_note_allocation",
                "severity": "warning",
                "message": (
                    f"Note {note.invoice_no} has {len(allocs)} note_allocation entries "
                    "(only one is expected)"
                ),
                "actual": len(allocs),
                "context": {
                    "client_id": note.client_id,
                    "note_type": note.note_type,
                    "allocation_ids": [a.id for a in allocs],
                },
            })
    return issues


CHECKS = [
    ("invoice_math", "Invoice math integrity", check_invoice_math),
    ("payment_allocations", "Payment / allocation consistency", check_payment_allocations),
    ("dispatch_coverage", "Dispatch coverage vs PO baseline", check_dispatch_coverage),
    ("note_linkage", "Credit / debit note linkage", check_note_linkage),
]


def _summarize(issues: list[dict]) -> dict:
    errors = sum(1 for i in issues if i.get("severity") == "error")
    warnings = sum(1 for i in issues if i.get("severity") == "warning")
    if errors:
        status = "errors"
    elif warnings:
        status = "warnings"
    else:
        status = "ok"
    return {"status": status, "errors": errors, "warnings": warnings, "total": len(issues)}


def run_reconciliation(db: Session) -> dict:
    """Run every registered check and assemble a report payload."""

    sections: list[dict] = []
    grand_errors = 0
    grand_warnings = 0
    for key, label, fn in CHECKS:
        try:
            issues = list(fn(db) or [])
        except Exception as exc:
            sections.append({
                "key": key,
                "label": label,
                "status": "errors",
                "errors": 1,
                "warnings": 0,
                "total": 1,
                "issues": [{
                    "entity_type": "system",
                    "entity_id": key,
                    "code": "check_crashed",
                    "severity": "error",
                    "message": f"Reconciliation check '{key}' crashed: {exc}",
                }],
            })
            grand_errors += 1
            continue
        summary = _summarize(issues)
        grand_errors += summary["errors"]
        grand_warnings += summary["warnings"]
        sections.append({
            "key": key,
            "label": label,
            "status": summary["status"],
            "errors": summary["errors"],
            "warnings": summary["warnings"],
            "total": summary["total"],
            "issues": issues,
        })

    overall = "ok"
    if grand_errors:
        overall = "errors"
    elif grand_warnings:
        overall = "warnings"

    return {
        "overall_status": overall,
        "errors": grand_errors,
        "warnings": grand_warnings,
        "sections": sections,
    }


__all__: Iterable[str] = (
    "check_invoice_math",
    "check_payment_allocations",
    "check_dispatch_coverage",
    "check_note_linkage",
    "run_reconciliation",
)
