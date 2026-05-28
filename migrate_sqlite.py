import argparse
import base64
import datetime
import hashlib
import json
import secrets
import sqlite3
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import (
    Base,
    Client,
    Invoice,
    InvoiceDispatchItem,
    PaymentAllocation,
    PaymentHistory,
    PoBaselineItem,
    PurchaseOrder,
    SystemSettings,
    UnallocatedAdvanceRegister,
    UnallocatedPaymentRegister,
    UploadedDocument,
    User,
)


SQLALCHEMY_DATABASE_URL = "sqlite:///./erp_database.sqlite"
LEGACY_DB_PATH = Path("old_erp.sqlite")
STATUS_PATH = Path("legacy_import_status.json")
RUN_MARKER_PATH = Path(".legacy_import_once.marker")
PBKDF2_ROUNDS = 210000

engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base.metadata.create_all(bind=engine)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def parse_date(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.datetime.strptime(raw.split("T")[0], "%Y-%m-%d").date()
    except Exception:
        return None


def to_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def normalize_role(role: str) -> str:
    role_val = str(role or "user").strip().lower()
    if role_val not in {"admin", "user", "logistics"}:
        return "user"
    return role_val


def recalculate_client_ledger(client_id: int, db):
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        return

    invoices = db.query(Invoice).filter(Invoice.client_id == client_id).all()
    inv_map = {inv.invoice_no: inv for inv in invoices}

    for inv in invoices:
        inv.net_payable = (inv.total or 0.0) - (inv.advance_adj or 0.0)
        inv.paid = 0.0
        inv.balance = inv.net_payable

    payments = db.query(PaymentHistory).filter(PaymentHistory.client_id == client_id).all()
    total_excess = 0.0

    for pay in payments:
        allocations = db.query(PaymentAllocation).filter(PaymentAllocation.payment_id == pay.id).all()
        alloc_sum = 0.0
        for al in allocations:
            if al.alloc_type == "invoice" and al.target_inv_id in inv_map:
                inv = inv_map[al.target_inv_id]
                inv.paid += al.amount
                inv.balance -= al.amount
            if al.alloc_type in ("invoice", "po_advance", "po_advance_applied", "note_allocation"):
                alloc_sum += al.amount

        if pay.type == "RECEIPT":
            unallocated = pay.amount - alloc_sum
            if unallocated > 0:
                total_excess += unallocated
        elif pay.type == "UNALLOCATED_APPLIED":
            total_excess -= alloc_sum

    client.excess_funds = max(0.0, total_excess)
    db.flush()


def truncate_target_tables(db):
    db.query(UploadedDocument).delete()
    db.query(UnallocatedAdvanceRegister).delete()
    db.query(UnallocatedPaymentRegister).delete()
    db.query(PaymentAllocation).delete()
    db.query(PaymentHistory).delete()
    db.query(InvoiceDispatchItem).delete()
    db.query(Invoice).delete()
    db.query(PoBaselineItem).delete()
    db.query(PurchaseOrder).delete()
    db.query(Client).delete()
    db.query(User).delete()
    db.query(SystemSettings).delete()
    db.flush()


def read_legacy_snapshot():
    if not LEGACY_DB_PATH.exists():
        raise FileNotFoundError(f"Legacy database not found: {LEGACY_DB_PATH}")
    old_conn = sqlite3.connect(str(LEGACY_DB_PATH))
    try:
        cursor = old_conn.cursor()
        cursor.execute("SELECT id, username, password, role FROM users")
        users = cursor.fetchall()
        cursor.execute("SELECT id, json_data FROM erp_data ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        if not row or not row[1]:
            raise ValueError("No JSON payload found in legacy erp_data table.")
        payload_id = row[0]
        app_data = json.loads(row[1])
        return users, payload_id, app_data
    finally:
        old_conn.close()


def ensure_unique_payment_id(raw_id: str, used_ids: set[str]) -> str:
    base = str(raw_id or "").strip() or str(int(datetime.datetime.now().timestamp() * 1000))
    candidate = base
    suffix = 1
    while candidate in used_ids:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used_ids.add(candidate)
    return candidate


def run_import(force: bool = False):
    conn = sqlite3.connect("erp_database.sqlite")
    cur = conn.cursor()
    try:
        cur.execute("PRAGMA table_info(purchase_orders)")
        cols = {row[1] for row in cur.fetchall()}
        if "contact_person" not in cols:
            cur.execute("ALTER TABLE purchase_orders ADD COLUMN contact_person TEXT")
        if "project_name" not in cols:
            cur.execute("ALTER TABLE purchase_orders ADD COLUMN project_name TEXT")
        conn.commit()
    finally:
        conn.close()

    if RUN_MARKER_PATH.exists() and not force:
        raise RuntimeError("One-time import already completed. Re-run with --force to import again.")

    users_rows, payload_id, app_data = read_legacy_snapshot()
    db = SessionLocal()
    report = {
        "started_at": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
        "source_db": str(LEGACY_DB_PATH),
        "source_payload_id": payload_id,
        "mode": "one_time_full_replace",
        "counts": {
            "users": {"read": len(users_rows), "inserted": 0, "skipped": 0},
            "clients": {"read": 0, "inserted": 0, "skipped": 0},
            "purchase_orders": {"read": 0, "inserted": 0, "skipped": 0},
            "po_baseline_items": {"read": 0, "inserted": 0, "skipped": 0},
            "invoices": {"read": 0, "inserted": 0, "skipped": 0},
            "dispatch_items": {"read": 0, "inserted": 0, "skipped": 0},
            "payments": {"read": 0, "inserted": 0, "skipped": 0},
            "allocations": {"read": 0, "inserted": 0, "skipped": 0},
        },
        "warnings": [],
        "client_totals": [],
        "integrity": {},
        "success": False,
    }

    try:
        truncate_target_tables(db)

        settings_data = app_data.get("_settings", {})
        sys_settings = SystemSettings(
            exchange_rate=to_float(settings_data.get("exchangeRate", 83.0), 83.0),
            custom_columns=json.dumps(settings_data.get("customColumns", [])),
        )
        db.add(sys_settings)
        db.flush()

        for _, username, password, role in users_rows:
            if not str(username or "").strip():
                report["counts"]["users"]["skipped"] += 1
                report["warnings"].append("Skipped user with empty username.")
                continue
            db.add(
                User(
                    username=str(username).strip(),
                    hashed_password=hash_password(str(password or "")),
                    role=normalize_role(role),
                )
            )
            report["counts"]["users"]["inserted"] += 1

        invoice_db_id_by_invoice_no = {}
        used_payment_ids = set()
        client_names = [k for k in app_data.keys() if k != "_settings"]
        report["counts"]["clients"]["read"] = len(client_names)

        for client_name in client_names:
            data = app_data.get(client_name) or {}
            if not str(client_name or "").strip():
                report["counts"]["clients"]["skipped"] += 1
                report["warnings"].append("Skipped unnamed client block.")
                continue

            client = Client(
                name=str(client_name).strip(),
                active=bool(data.get("active", True)),
                excess_funds=to_float(data.get("excess", data.get("unallocated", 0.0)), 0.0),
            )
            db.add(client)
            db.flush()
            report["counts"]["clients"]["inserted"] += 1

            po_id_map = {}
            po_advance_map = data.get("poAdvances") or {}
            po_terms = data.get("poTerms") or {}
            report["counts"]["purchase_orders"]["read"] += len(po_terms)

            for po_no, terms in po_terms.items():
                if not str(po_no or "").strip():
                    report["counts"]["purchase_orders"]["skipped"] += 1
                    continue
                terms = terms or {}
                imported_pool = to_float(po_advance_map.get(po_no, 0.0), 0.0)
                po = PurchaseOrder(
                    client_id=client.id,
                    po_no=str(po_no).strip(),
                    adv_pct=to_float(terms.get("advPct", terms.get("adv", 0.0)), 0.0),
                    ret_pct=to_float(terms.get("retPct", terms.get("ret", 0.0)), 0.0),
                    ret_base=str(terms.get("retBase", terms.get("base", "basic")) or "basic"),
                    tds_base=str(terms.get("tdsBase", terms.get("tds_base", "basic")) or "basic"),
                    tds_enabled=bool(terms.get("tdsEnabled", False)),
                    tds_rate=to_float(terms.get("tdsRate", 0.0), 0.0),
                    tds_threshold=to_float(terms.get("tdsThreshold", 0.0), 0.0),
                    # Legacy snapshot kept a separate PO advance wallet map.
                    # Runtime allocator consumes wallets from payment_allocations
                    # (alloc_type='po_advance'), so we materialize those rows below.
                    # Keep column at 0 to avoid dual-source drift.
                    advance_pool=0.0,
                )
                db.add(po)
                db.flush()
                po_id_map[po.po_no] = po.id
                report["counts"]["purchase_orders"]["inserted"] += 1

                if imported_pool > 0:
                    adv_pay_id = ensure_unique_payment_id(f"LEGACY_PO_ADV_{client.id}_{po.po_no}", used_payment_ids)
                    db.add(
                        PaymentHistory(
                            id=adv_pay_id,
                            client_id=client.id,
                            date=datetime.date.today(),
                            type="RECEIPT",
                            amount=float(imported_pool),
                            details=f"Legacy PO advance wallet import ({po.po_no})",
                            note="Imported from legacy poAdvances map",
                        )
                    )
                    db.flush()
                    db.add(
                        PaymentAllocation(
                            payment_id=adv_pay_id,
                            alloc_type="po_advance",
                            target_inv_id=None,
                            target_po_no=po.po_no,
                            note_id=None,
                            amount=float(imported_pool),
                        )
                    )
                    report["counts"]["payments"]["inserted"] += 1
                    report["counts"]["allocations"]["inserted"] += 1

                baseline_items = terms.get("baselineItems") or []
                report["counts"]["po_baseline_items"]["read"] += len(baseline_items)
                for item in baseline_items:
                    item = item or {}
                    desc = str(item.get("description", "")).strip()
                    if not desc:
                        report["counts"]["po_baseline_items"]["skipped"] += 1
                        continue
                    db.add(
                        PoBaselineItem(
                            po_id=po.id,
                            description=desc,
                            ordered_qty=to_float(item.get("ordered_qty", item.get("orderedQty", item.get("qty", 0.0))), 0.0),
                            inspected_qty=to_float(item.get("inspected_qty", item.get("inspectedQty", 0.0)), 0.0),
                            uom=(str(item.get("uom")).strip() if item.get("uom") is not None else None),
                        )
                    )
                    report["counts"]["po_baseline_items"]["inserted"] += 1

            invoices = data.get("invoices") or []
            report["counts"]["invoices"]["read"] += len(invoices)
            for inv in invoices:
                inv = inv or {}
                invoice_no = str(inv.get("id", "")).strip()
                if not invoice_no:
                    report["counts"]["invoices"]["skipped"] += 1
                    continue

                po_no = str(inv.get("poNo", "UNASSIGNED")).strip()
                po_id = None
                if po_no and po_no != "UNASSIGNED":
                    po_id = po_id_map.get(po_no)
                    if not po_id:
                        lazy_po = PurchaseOrder(client_id=client.id, po_no=po_no)
                        db.add(lazy_po)
                        db.flush()
                        po_id = lazy_po.id
                        po_id_map[po_no] = po_id
                        report["counts"]["purchase_orders"]["inserted"] += 1

                invoice = Invoice(
                    client_id=client.id,
                    po_id=po_id,
                    invoice_no=invoice_no,
                    sub_entity=(str(inv.get("subEntity")).strip() if inv.get("subEntity") is not None else None),
                    lr_no=(str(inv.get("lrNo")).strip() if inv.get("lrNo") is not None else None),
                    inv_date=parse_date(inv.get("invDate")),
                    due_date=parse_date(inv.get("dueDate")),
                    basic=to_float(inv.get("basic", 0.0), 0.0),
                    gst=to_float(inv.get("gst", 0.0), 0.0),
                    total=to_float(inv.get("total", 0.0), 0.0),
                    advance_adj=to_float(inv.get("advance", 0.0), 0.0),
                    tds_ded=to_float(inv.get("tds", 0.0), 0.0),
                    retention_held=to_float(inv.get("retention", 0.0), 0.0),
                    net_payable=to_float(inv.get("netPayable", 0.0), 0.0),
                    paid=to_float(inv.get("paid", 0.0), 0.0),
                    balance=to_float(inv.get("balance", 0.0), 0.0),
                    is_note=bool(inv.get("isNote", False)),
                    note_type=(str(inv.get("noteType")).strip() if inv.get("noteType") is not None else None),
                    note_reason=(str(inv.get("noteReason")).strip() if inv.get("noteReason") is not None else None),
                )
                db.add(invoice)
                db.flush()
                invoice_db_id_by_invoice_no[invoice_no] = invoice.id
                report["counts"]["invoices"]["inserted"] += 1

                dispatch_items = inv.get("dispatchItems") or []
                report["counts"]["dispatch_items"]["read"] += len(dispatch_items)
                for item in dispatch_items:
                    item = item or {}
                    desc = str(item.get("description", "")).strip()
                    if not desc:
                        report["counts"]["dispatch_items"]["skipped"] += 1
                        continue
                    db.add(
                        InvoiceDispatchItem(
                            invoice_id=invoice.id,
                            description=desc,
                            dispatched_qty=to_float(item.get("qty", item.get("dispatched_qty", 0.0)), 0.0),
                            inspected_qty=to_float(item.get("inspected_qty", item.get("inspectedQty", 0.0)), 0.0),
                            uom=(str(item.get("uom")).strip() if item.get("uom") is not None else None),
                        )
                    )
                    report["counts"]["dispatch_items"]["inserted"] += 1

            payments = data.get("paymentHistory") or []
            report["counts"]["payments"]["read"] += len(payments)
            for pay in payments:
                pay = pay or {}
                payment_id = ensure_unique_payment_id(pay.get("id"), used_payment_ids)
                payment = PaymentHistory(
                    id=payment_id,
                    client_id=client.id,
                    date=parse_date(pay.get("date")) or datetime.date.today(),
                    type=str(pay.get("type", "RECEIPT")),
                    amount=to_float(pay.get("amount", 0.0), 0.0),
                    details=str(pay.get("details", "")),
                    note=str(pay.get("note", "")),
                )
                db.add(payment)
                db.flush()
                report["counts"]["payments"]["inserted"] += 1

                allocations = pay.get("allocations") or []
                report["counts"]["allocations"]["read"] += len(allocations)
                for alloc in allocations:
                    alloc = alloc or {}
                    target_inv_id = alloc.get("invId", alloc.get("id"))
                    db.add(
                        PaymentAllocation(
                            payment_id=payment.id,
                            alloc_type=str(alloc.get("type", "invoice")),
                            target_inv_id=(str(target_inv_id).strip() if target_inv_id is not None else None),
                            target_po_no=(str(alloc.get("po")).strip() if alloc.get("po") is not None else None),
                            note_id=(str(alloc.get("noteId")).strip() if alloc.get("noteId") is not None else None),
                            amount=to_float(alloc.get("amount", 0.0), 0.0),
                        )
                    )
                    report["counts"]["allocations"]["inserted"] += 1

        for client in db.query(Client).all():
            recalculate_client_ledger(client.id, db)

        # Integrity checks
        orphan_dispatch = (
            db.query(InvoiceDispatchItem)
            .outerjoin(Invoice, InvoiceDispatchItem.invoice_id == Invoice.id)
            .filter(Invoice.id.is_(None))
            .count()
        )
        orphan_alloc_pay = (
            db.query(PaymentAllocation)
            .outerjoin(PaymentHistory, PaymentAllocation.payment_id == PaymentHistory.id)
            .filter(PaymentHistory.id.is_(None))
            .count()
        )
        invoice_alloc_missing_target = (
            db.query(PaymentAllocation)
            .filter(PaymentAllocation.alloc_type == "invoice")
            .filter(PaymentAllocation.target_inv_id.isnot(None))
            .all()
        )
        known_invoice_nos = {x[0] for x in db.query(Invoice.invoice_no).all()}
        missing_invoice_refs = 0
        for alloc in invoice_alloc_missing_target:
            if alloc.target_inv_id not in known_invoice_nos:
                missing_invoice_refs += 1

        report["integrity"] = {
            "orphan_dispatch_rows": orphan_dispatch,
            "orphan_allocation_payment_rows": orphan_alloc_pay,
            "invoice_allocation_missing_invoice_refs": missing_invoice_refs,
        }

        for client in db.query(Client).all():
            inv_count = db.query(Invoice).filter(Invoice.client_id == client.id).count()
            pay_total = db.query(PaymentHistory).filter(PaymentHistory.client_id == client.id).all()
            pay_sum = sum((p.amount or 0.0) for p in pay_total)
            open_balance = sum((inv.balance or 0.0) for inv in db.query(Invoice).filter(Invoice.client_id == client.id).all())
            report["client_totals"].append(
                {
                    "client": client.name,
                    "invoice_count": inv_count,
                    "payment_total": round(pay_sum, 2),
                    "open_balance": round(open_balance, 2),
                    "excess_funds": round(client.excess_funds or 0.0, 2),
                }
            )

        report["success"] = True
        report["completed_at"] = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")
        db.commit()
        STATUS_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
        RUN_MARKER_PATH.write_text(report["completed_at"], encoding="utf-8")

        print("[INFO] Legacy import completed successfully.")
        print(json.dumps(report["counts"], indent=2))
        print("[INFO] Integrity:", json.dumps(report["integrity"], indent=2))
        print(f"[INFO] Status written to {STATUS_PATH}")
    except Exception as exc:
        db.rollback()
        report["success"] = False
        report["error"] = str(exc)
        report["completed_at"] = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")
        STATUS_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"[WARN] Import failed: {exc}")
        raise
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="One-time full import from old_erp.sqlite to erp_database.sqlite.")
    parser.add_argument("--force", action="store_true", help="Allow re-running import even if one-time marker exists.")
    args = parser.parse_args()
    run_import(force=args.force)


if __name__ == "__main__":
    main()