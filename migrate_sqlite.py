import argparse
import base64
import datetime
import difflib
import hashlib
import json
import os
import secrets
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
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
    User,
)

load_dotenv()

STATUS_PATH = Path("legacy_import_status.json")
RUN_MARKER_PATH = Path(".legacy_import_once.marker")
PBKDF2_ROUNDS = 210000


def target_database_url() -> str:
    return os.getenv("APP_DATABASE_URL", "sqlite:///./erp_database.sqlite").strip()


def target_sqlite_path() -> Path:
    url = target_database_url()
    if not url.startswith("sqlite:///"):
        raise ValueError(f"Legacy import supports SQLite only (got {url!r}).")
    raw = url.replace("sqlite:///", "", 1)
    return Path(raw if raw else "erp_database.sqlite")


def legacy_db_path(override: str | None = None) -> Path:
    if override:
        return Path(override)
    return Path(os.getenv("LEGACY_DB_PATH", "old_erp.sqlite"))


def open_target_session():
    url = target_database_url()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    return engine, SessionLocal


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
    db.commit()


def truncate_target_tables(db):
    db.query(PaymentAllocation).delete()
    db.query(PaymentHistory).delete()
    db.query(InvoiceDispatchItem).delete()
    db.query(Invoice).delete()
    db.query(PoBaselineItem).delete()
    db.query(PurchaseOrder).delete()
    db.query(UnallocatedPaymentRegister).delete()
    db.query(UnallocatedAdvanceRegister).delete()
    db.query(Client).delete()
    db.query(User).delete()
    db.query(SystemSettings).delete()
    db.flush()


def empty_counts() -> dict:
    return {
        "users": {"read": 0, "inserted": 0, "skipped": 0},
        "clients": {"read": 0, "inserted": 0, "skipped": 0},
        "purchase_orders": {"read": 0, "inserted": 0, "skipped": 0},
        "po_baseline_items": {"read": 0, "inserted": 0, "skipped": 0},
        "invoices": {"read": 0, "inserted": 0, "skipped": 0},
        "dispatch_items": {"read": 0, "inserted": 0, "skipped": 0},
        "payments": {"read": 0, "inserted": 0, "skipped": 0},
        "allocations": {"read": 0, "inserted": 0, "skipped": 0},
    }


def merge_counts(report: dict, block: dict) -> None:
    for key, vals in block.items():
        for sub, n in vals.items():
            report["counts"][key][sub] += n


def normalize_name_key(name: str) -> str:
    return str(name or "").strip().casefold()


def legacy_client_names(app_data: dict) -> list[str]:
    return [k for k in app_data.keys() if k != "_settings" and str(k).strip()]


def resolve_selected_clients(requested: list[str], app_data: dict) -> tuple[list[str], list[str]]:
    legacy_map = {normalize_name_key(k): k for k in legacy_client_names(app_data)}
    resolved: list[str] = []
    seen: set[str] = set()
    unknown: list[str] = []
    for raw in requested:
        req = str(raw or "").strip()
        if not req:
            continue
        key = normalize_name_key(req)
        if key in legacy_map:
            canonical = legacy_map[key]
            if canonical not in seen:
                resolved.append(canonical)
                seen.add(canonical)
        else:
            unknown.append(req)
    return resolved, unknown


def parse_clients_arg(clients: str | None, clients_file: str | None) -> list[str]:
    names: list[str] = []
    if clients:
        names.extend(part.strip() for part in clients.split(",") if part.strip())
    if clients_file:
        path = Path(clients_file)
        if not path.exists():
            raise FileNotFoundError(f"Clients file not found: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                names.append(line)
    return names


def load_existing_payment_ids(db) -> set[str]:
    return {row[0] for row in db.query(PaymentHistory.id).all() if row[0]}


def delete_client_for_reimport(db, client: Client) -> None:
    cid = client.id
    db.query(UnallocatedPaymentRegister).filter(UnallocatedPaymentRegister.client_id == cid).delete()
    db.query(UnallocatedAdvanceRegister).filter(UnallocatedAdvanceRegister.client_id == cid).delete()
    db.delete(client)
    db.flush()


def assert_no_global_collisions(db, data: dict, exclude_client_id: int | None = None) -> None:
    invoice_nos: list[str] = []
    for inv in data.get("invoices") or []:
        inv = inv or {}
        no = str(inv.get("id", "")).strip()
        if no:
            invoice_nos.append(no)
    po_nos: list[str] = []
    for po_no in (data.get("poTerms") or {}).keys():
        p = str(po_no or "").strip()
        if p:
            po_nos.append(p)

    if invoice_nos:
        q = db.query(Invoice.invoice_no).filter(Invoice.invoice_no.in_(invoice_nos))
        if exclude_client_id is not None:
            q = q.filter(Invoice.client_id != exclude_client_id)
        clash = [r[0] for r in q.all()]
        if clash:
            raise ValueError(
                f"Invoice number(s) already used by another client: {', '.join(clash[:5])}"
                + (" ..." if len(clash) > 5 else "")
            )

    if po_nos:
        q = db.query(PurchaseOrder.po_no).filter(PurchaseOrder.po_no.in_(po_nos))
        if exclude_client_id is not None:
            q = q.filter(PurchaseOrder.client_id != exclude_client_id)
        clash = [r[0] for r in q.all()]
        if clash:
            raise ValueError(
                f"PO number(s) already used by another client: {', '.join(clash[:5])}"
                + (" ..." if len(clash) > 5 else "")
            )


def import_users_block(db, users_rows: list, counts: dict) -> None:
    counts["users"]["read"] = len(users_rows)
    for _, username, password, role in users_rows:
        if not str(username or "").strip():
            counts["users"]["skipped"] += 1
            continue
        db.add(
            User(
                username=str(username).strip(),
                hashed_password=hash_password(str(password or "")),
                role=normalize_role(role),
            )
        )
        counts["users"]["inserted"] += 1


def import_settings_block(db, app_data: dict) -> None:
    settings_data = app_data.get("_settings", {})
    db.query(SystemSettings).delete()
    db.add(
        SystemSettings(
            exchange_rate=to_float(settings_data.get("exchangeRate", 83.0), 83.0),
            custom_columns=json.dumps(settings_data.get("customColumns", [])),
        )
    )
    db.flush()


def import_client_block(
    db,
    client_name: str,
    data: dict,
    used_payment_ids: set[str],
) -> dict:
    counts = empty_counts()
    counts["clients"]["read"] = 1

    client = Client(
        name=str(client_name).strip(),
        active=bool(data.get("active", True)),
        excess_funds=to_float(data.get("excess", data.get("unallocated", 0.0)), 0.0),
    )
    db.add(client)
    db.flush()
    counts["clients"]["inserted"] = 1

    po_id_map: dict[str, int] = {}
    po_advance_map = data.get("poAdvances") or {}
    po_terms = data.get("poTerms") or {}
    counts["purchase_orders"]["read"] = len(po_terms)

    for po_no, terms in po_terms.items():
        if not str(po_no or "").strip():
            counts["purchase_orders"]["skipped"] += 1
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
            advance_pool=0.0,
        )
        db.add(po)
        db.flush()
        po_id_map[po.po_no] = po.id
        counts["purchase_orders"]["inserted"] += 1

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
            counts["payments"]["inserted"] += 1
            counts["allocations"]["inserted"] += 1

        baseline_items = terms.get("baselineItems") or []
        counts["po_baseline_items"]["read"] += len(baseline_items)
        for item in baseline_items:
            item = item or {}
            desc = str(item.get("description", "")).strip()
            if not desc:
                counts["po_baseline_items"]["skipped"] += 1
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
            counts["po_baseline_items"]["inserted"] += 1

    invoices = data.get("invoices") or []
    counts["invoices"]["read"] = len(invoices)
    for inv in invoices:
        inv = inv or {}
        invoice_no = str(inv.get("id", "")).strip()
        if not invoice_no:
            counts["invoices"]["skipped"] += 1
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
                counts["purchase_orders"]["inserted"] += 1

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
        counts["invoices"]["inserted"] += 1

        dispatch_items = inv.get("dispatchItems") or []
        counts["dispatch_items"]["read"] += len(dispatch_items)
        for item in dispatch_items:
            item = item or {}
            desc = str(item.get("description", "")).strip()
            if not desc:
                counts["dispatch_items"]["skipped"] += 1
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
            counts["dispatch_items"]["inserted"] += 1

    payments = data.get("paymentHistory") or []
    counts["payments"]["read"] = len(payments)
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
        counts["payments"]["inserted"] += 1

        allocations = pay.get("allocations") or []
        counts["allocations"]["read"] += len(allocations)
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
            counts["allocations"]["inserted"] += 1

    return counts


def build_integrity_report(db, client_ids: list[int] | None = None) -> dict:
    inv_q = db.query(InvoiceDispatchItem).outerjoin(Invoice, InvoiceDispatchItem.invoice_id == Invoice.id)
    if client_ids is not None:
        inv_q = inv_q.filter(Invoice.client_id.in_(client_ids))
    orphan_dispatch = inv_q.filter(Invoice.id.is_(None)).count()

    alloc_q = db.query(PaymentAllocation).outerjoin(
        PaymentHistory, PaymentAllocation.payment_id == PaymentHistory.id
    )
    if client_ids is not None:
        alloc_q = alloc_q.filter(PaymentHistory.client_id.in_(client_ids))
    orphan_alloc_pay = alloc_q.filter(PaymentHistory.id.is_(None)).count()

    invoice_alloc_q = db.query(PaymentAllocation).filter(PaymentAllocation.alloc_type == "invoice")
    if client_ids is not None:
        invoice_alloc_q = invoice_alloc_q.join(PaymentHistory).filter(PaymentHistory.client_id.in_(client_ids))
    invoice_alloc_missing_target = invoice_alloc_q.filter(PaymentAllocation.target_inv_id.isnot(None)).all()

    if client_ids is not None:
        known_invoice_nos = {
            x[0]
            for x in db.query(Invoice.invoice_no).filter(Invoice.client_id.in_(client_ids)).all()
        }
    else:
        known_invoice_nos = {x[0] for x in db.query(Invoice.invoice_no).all()}
    missing_invoice_refs = sum(
        1 for alloc in invoice_alloc_missing_target if alloc.target_inv_id not in known_invoice_nos
    )

    return {
        "orphan_dispatch_rows": orphan_dispatch,
        "orphan_allocation_payment_rows": orphan_alloc_pay,
        "invoice_allocation_missing_invoice_refs": missing_invoice_refs,
    }


def client_totals_row(db, client: Client) -> dict:
    inv_count = db.query(Invoice).filter(Invoice.client_id == client.id).count()
    pay_total = db.query(PaymentHistory).filter(PaymentHistory.client_id == client.id).all()
    pay_sum = sum((p.amount or 0.0) for p in pay_total)
    open_balance = sum(
        (inv.balance or 0.0) for inv in db.query(Invoice).filter(Invoice.client_id == client.id).all()
    )
    return {
        "client": client.name,
        "invoice_count": inv_count,
        "payment_total": round(pay_sum, 2),
        "open_balance": round(open_balance, 2),
        "excess_funds": round(client.excess_funds or 0.0, 2),
    }


def list_legacy_clients(legacy_path: str | None = None) -> None:
    source = legacy_db_path(legacy_path)
    _, payload_id, app_data = read_legacy_snapshot(source)
    print(f"Legacy source: {source} (erp_data id={payload_id})")
    print("CLIENT\tINVOICES\tPAYMENTS\tPO_TERMS\tACTIVE")
    for name in sorted(legacy_client_names(app_data), key=lambda x: x.lower()):
        data = app_data.get(name) or {}
        active = "yes" if data.get("active", True) else "no"
        print(
            f"{name}\t{len(data.get('invoices') or [])}\t"
            f"{len(data.get('paymentHistory') or [])}\t"
            f"{len(data.get('poTerms') or {})}\t{active}"
        )


def read_legacy_snapshot(source: Path):
    if not source.exists():
        raise FileNotFoundError(
            f"Legacy database not found: {source.resolve()}\n"
            "Copy your previous ERP export as old_erp.sqlite in the project root "
            "(see LEGACY_IMPORT_MAPPING.md), then run: .\\venv\\Scripts\\python.exe migrate_sqlite.py"
        )
    old_conn = sqlite3.connect(str(source))
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


def suggest_legacy_names(unknown: list[str], app_data: dict, n: int = 3) -> dict[str, list[str]]:
    choices = legacy_client_names(app_data)
    out: dict[str, list[str]] = {}
    for name in unknown:
        hits = difflib.get_close_matches(name, choices, n=n, cutoff=0.5)
        if hits:
            out[name] = hits
    return out


def ensure_target_schema(db_file: Path) -> None:
    conn = sqlite3.connect(str(db_file))
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


def run_import(
    force: bool = False,
    legacy_path: str | None = None,
    mode: str = "replace",
    clients: str | None = None,
    clients_file: str | None = None,
    import_users: bool | None = None,
    import_settings: bool | None = None,
    dry_run: bool = False,
):
    mode = (mode or "replace").strip().lower()
    if mode not in {"replace", "merge"}:
        raise ValueError("--mode must be 'replace' or 'merge'")

    source = legacy_db_path(legacy_path)
    db_file = target_sqlite_path()
    ensure_target_schema(db_file)

    if mode == "replace" and RUN_MARKER_PATH.exists() and not force and not dry_run:
        raise RuntimeError("One-time import already completed. Re-run with --force to import again.")

    users_rows, payload_id, app_data = read_legacy_snapshot(source)

    if mode == "merge":
        requested = parse_clients_arg(clients, clients_file)
        if not requested:
            raise ValueError("Merge mode requires --clients or --clients-file with at least one client name.")
        client_names, unknown = resolve_selected_clients(requested, app_data)
        if not client_names:
            hints = suggest_legacy_names(unknown, app_data)
            msg = "No legacy clients matched the requested names."
            if unknown:
                msg += f" Unknown: {', '.join(unknown)}."
            if hints:
                msg += " Did you mean: " + "; ".join(f"{k} -> {', '.join(v)}" for k, v in hints.items())
            raise ValueError(msg)
    else:
        client_names = legacy_client_names(app_data)
        unknown = []

    do_users = import_users if import_users is not None else (mode == "replace")
    do_settings = import_settings if import_settings is not None else (mode == "replace")

    report = {
        "started_at": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
        "source_db": str(source),
        "target_db": str(db_file),
        "source_payload_id": payload_id,
        "mode": "selective_merge" if mode == "merge" else "one_time_full_replace",
        "dry_run": dry_run,
        "clients_selected": client_names if mode == "merge" else None,
        "clients_unknown": unknown if unknown else None,
        "import_users": do_users,
        "import_settings": do_settings,
        "counts": empty_counts(),
        "warnings": [],
        "client_totals": [],
        "integrity": {},
        "success": False,
    }

    if unknown:
        hints = suggest_legacy_names(unknown, app_data)
        for name in unknown:
            if name in hints:
                report["warnings"].append(f"Unknown client '{name}'; did you mean: {', '.join(hints[name])}?")
            else:
                report["warnings"].append(f"Unknown client '{name}' (not in legacy export).")

    if dry_run:
        print(f"[DRY-RUN] mode={mode} clients={len(client_names)} import_users={do_users} import_settings={do_settings}")
        for name in client_names:
            data = app_data.get(name) or {}
            print(
                f"  {name}: legacy invoices={len(data.get('invoices') or [])} "
                f"payments={len(data.get('paymentHistory') or [])}"
            )
        report["success"] = True
        report["completed_at"] = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")
        STATUS_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"[DRY-RUN] Plan written to {STATUS_PATH}")
        return

    _, SessionLocal = open_target_session()
    db = SessionLocal()
    imported_client_ids: list[int] = []

    try:
        if mode == "replace":
            truncate_target_tables(db)
        else:
            db.commit()

        if do_settings:
            import_settings_block(db, app_data)

        if do_users:
            if mode == "replace":
                import_users_block(db, users_rows, report["counts"])
            else:
                db.query(User).delete()
                db.flush()
                import_users_block(db, users_rows, report["counts"])

        used_payment_ids = load_existing_payment_ids(db) if mode == "merge" else set()
        report["counts"]["clients"]["read"] = len(client_names)

        for client_name in client_names:
            data = app_data.get(client_name) or {}
            if not str(client_name or "").strip():
                report["counts"]["clients"]["skipped"] += 1
                report["warnings"].append("Skipped unnamed client block.")
                continue

            exclude_id = None
            if mode == "merge":
                existing = None
                target_key = normalize_name_key(client_name)
                for row in db.query(Client).all():
                    if normalize_name_key(row.name) == target_key:
                        existing = row
                        break
                if existing:
                    exclude_id = existing.id
                assert_no_global_collisions(db, data, exclude_client_id=exclude_id)
                if existing:
                    delete_client_for_reimport(db, existing)

            block = import_client_block(db, client_name, data, used_payment_ids)
            merge_counts(report, block)
            db.flush()
            client_row = db.query(Client).filter(Client.name == str(client_name).strip()).first()
            if client_row:
                imported_client_ids.append(client_row.id)

        db.commit()

        for cid in imported_client_ids:
            recalculate_client_ledger(cid, db)

        scope_ids = imported_client_ids if mode == "merge" else None
        report["integrity"] = build_integrity_report(db, scope_ids)

        totals_q = db.query(Client)
        if mode == "merge" and imported_client_ids:
            totals_q = totals_q.filter(Client.id.in_(imported_client_ids))
        for client in totals_q.all():
            report["client_totals"].append(client_totals_row(db, client))

        report["success"] = True
        report["completed_at"] = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")
        STATUS_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
        if mode == "replace":
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


def dispatch_import_args(args) -> None:
    import_users = None
    if getattr(args, "import_users", False):
        import_users = True
    if getattr(args, "no_import_users", False):
        import_users = False
    import_settings = None
    if getattr(args, "import_settings", False):
        import_settings = True
    if getattr(args, "no_import_settings", False):
        import_settings = False
    run_import(
        force=args.force,
        legacy_path=args.legacy_path,
        mode=args.mode,
        clients=args.clients,
        clients_file=args.clients_file,
        import_users=import_users,
        import_settings=import_settings,
        dry_run=args.dry_run,
    )


def main():
    parser = argparse.ArgumentParser(description="Legacy ERP import from old_erp.sqlite.")
    parser.add_argument(
        "command",
        nargs="?",
        default="import",
        choices=["import", "list-clients"],
        help="import (default): load data; list-clients: preview legacy client names.",
    )
    parser.add_argument("--force", action="store_true", help="Allow re-running full replace when marker exists.")
    parser.add_argument(
        "--legacy-path",
        default=None,
        help="Legacy SQLite path (default: LEGACY_DB_PATH env or old_erp.sqlite).",
    )
    parser.add_argument(
        "--mode",
        choices=["replace", "merge"],
        default="replace",
        help="replace: wipe target and import all; merge: replace only selected clients.",
    )
    parser.add_argument("--clients", default=None, help="Comma-separated client names (merge mode).")
    parser.add_argument("--clients-file", default=None, help="One client name per line (merge mode).")
    parser.add_argument("--dry-run", action="store_true", help="Validate names and print plan; no DB writes.")
    parser.add_argument(
        "--import-users",
        action="store_true",
        default=None,
        help="Import users from legacy (default: on for replace, off for merge).",
    )
    parser.add_argument("--no-import-users", action="store_true", help="Do not import users from legacy.")
    parser.add_argument(
        "--import-settings",
        action="store_true",
        default=None,
        help="Replace system_settings from legacy (default: on for replace, off for merge).",
    )
    parser.add_argument("--no-import-settings", action="store_true", help="Do not import system settings.")

    args = parser.parse_args()
    if args.command == "list-clients":
        list_legacy_clients(legacy_path=args.legacy_path)
        return
    dispatch_import_args(args)


if __name__ == "__main__":
    main()