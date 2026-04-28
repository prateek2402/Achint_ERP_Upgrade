import argparse
import csv
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests


def normalize_dispatch_desc(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]+", " ", (text or "").upper())).strip()


def to_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class BackfillResult:
    scanned_pdfs: int = 0
    deduped_pdfs: int = 0
    parsed_ok: int = 0
    parse_failed: int = 0
    matched_invoices: int = 0
    unmatched_pdfs: int = 0
    updated_invoices: int = 0
    merged_lines: int = 0
    appended_lines: int = 0
    skipped_no_dispatch_items: int = 0


def collect_pdfs(folder_paths: List[str]) -> List[Path]:
    pdfs: List[Path] = []
    for folder in folder_paths:
        root = Path(folder).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            print(f"[WARN] Skipping invalid folder: {root}")
            continue
        for p in root.rglob("*.pdf"):
            if p.is_file():
                pdfs.append(p)
    return pdfs


def dedupe_pdfs(pdfs: List[Path]) -> Tuple[List[Path], Dict[str, str]]:
    seen_hash: Dict[str, Path] = {}
    duplicates: Dict[str, str] = {}
    out: List[Path] = []
    for p in pdfs:
        digest = file_sha256(p)
        if digest in seen_hash:
            duplicates[str(p)] = str(seen_hash[digest])
            continue
        seen_hash[digest] = p
        out.append(p)
    return out, duplicates


def build_auth_headers(base_url: str, token: str, username: str, password: str, timeout_sec: int) -> Dict[str, str]:
    if token:
        return {"Authorization": f"Bearer {token}"}
    if not username or not password:
        raise ValueError("Provide either --token or both --username and --password.")
    resp = requests.post(
        f"{base_url}/api/login",
        json={"username": username, "password": password},
        timeout=timeout_sec,
    )
    resp.raise_for_status()
    payload = resp.json()
    access_token = payload.get("access_token")
    if not access_token:
        raise ValueError("Login succeeded but no access_token returned.")
    return {"Authorization": f"Bearer {access_token}"}


def load_invoices_map(base_url: str, headers: Dict[str, str], timeout_sec: int) -> Dict[str, Dict[str, Any]]:
    resp = requests.get(f"{base_url}/api/invoices", headers=headers, timeout=timeout_sec)
    resp.raise_for_status()
    invoices = resp.json()
    inv_map: Dict[str, Dict[str, Any]] = {}
    for inv in invoices:
        inv_no = (inv.get("id") or "").strip()
        if inv_no:
            inv_map[inv_no] = inv
    return inv_map


def extract_invoice_from_pdf(base_url: str, headers: Dict[str, str], pdf_path: Path, timeout_sec: int) -> Dict[str, Any]:
    with pdf_path.open("rb") as f:
        files = [("invoice_pdf", (pdf_path.name, f, "application/pdf"))]
        resp = requests.post(f"{base_url}/api/upload-invoice", headers=headers, files=files, timeout=timeout_sec)
    resp.raise_for_status()
    payload = resp.json()
    results = payload.get("results") or []
    if not results:
        raise ValueError("No extraction result returned")
    first = results[0]
    if not first.get("success") or not first.get("raw_data"):
        raise ValueError(first.get("error") or "Extraction failed")
    raw = str(first["raw_data"]).replace("```json", "").replace("```", "").strip()
    parsed = json.loads(raw)
    return parsed


def merge_dispatch_items(
    existing_items: List[Dict[str, Any]], extracted_items: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], int, int]:
    merged = [dict(item) for item in (existing_items or [])]
    index: Dict[Tuple[str, str], int] = {}
    for i, item in enumerate(merged):
        key = (normalize_dispatch_desc(str(item.get("description") or "")), str(item.get("uom") or "").strip().upper())
        index[key] = i

    merged_lines = 0
    appended_lines = 0
    for item in extracted_items or []:
        desc = str(item.get("desc") or item.get("description") or "").strip()
        if not desc:
            continue
        uom = str(item.get("uom") or "Nos").strip() or "Nos"
        qty = to_float(item.get("qty"))
        if qty <= 0:
            continue
        key = (normalize_dispatch_desc(desc), uom.upper())
        if key in index:
            idx = index[key]
            merged[idx]["qty"] = to_float(merged[idx].get("qty")) + qty
            merged_lines += 1
        else:
            merged.append(
                {
                    "description": desc,
                    "qty": qty,
                    "uom": uom,
                    "inspected_qty": to_float(item.get("inspected_qty")),
                }
            )
            index[key] = len(merged) - 1
            appended_lines += 1
    return merged, merged_lines, appended_lines


def build_invoice_update_payload(inv: Dict[str, Any], merged_dispatch_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "client_id": inv["client_id"],
        "po_no": inv.get("poNo") or "UNASSIGNED",
        "invoice_no": inv.get("id"),
        "sub_entity": inv.get("subEntity") or "",
        "lr_no": inv.get("lrNo") or "",
        "inv_date": inv.get("invDate"),
        "due_date": inv.get("dueDate"),
        "basic": to_float(inv.get("basic")),
        "gst": to_float(inv.get("gst")),
        "total": to_float(inv.get("total")),
        "advance_adj": to_float(inv.get("advance")),
        "tds_ded": to_float(inv.get("tds")),
        "retention_held": to_float(inv.get("retention")),
        "net_payable": to_float(inv.get("netPayable")),
        "paid": to_float(inv.get("paid")),
        "balance": to_float(inv.get("balance")),
        "is_note": bool(inv.get("isNote")),
        "note_type": inv.get("noteType"),
        "note_reason": inv.get("noteReason"),
        "dispatch_items": merged_dispatch_items,
    }


def write_report(report_dir: Path, report: Dict[str, Any]) -> Tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "dispatch_backfill_report.json"
    csv_path = report_dir / "dispatch_backfill_report.csv"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["pdf_path", "invoice_no", "status", "message", "merged_lines", "appended_lines"],
        )
        writer.writeheader()
        for row in report.get("rows", []):
            writer.writerow(
                {
                    "pdf_path": row.get("pdf_path", ""),
                    "invoice_no": row.get("invoice_no", ""),
                    "status": row.get("status", ""),
                    "message": row.get("message", ""),
                    "merged_lines": row.get("merged_lines", 0),
                    "appended_lines": row.get("appended_lines", 0),
                }
            )
    return json_path, csv_path


def verify_updated_invoices(
    base_url: str, headers: Dict[str, str], updated_invoice_nos: List[str], timeout_sec: int
) -> Dict[str, Any]:
    if not updated_invoice_nos:
        return {"verified_count": 0, "dispatch_present_count": 0, "missing_dispatch": []}
    inv_map = load_invoices_map(base_url, headers, timeout_sec)
    missing_dispatch: List[str] = []
    present_count = 0
    for inv_no in updated_invoice_nos:
        inv = inv_map.get(inv_no)
        if not inv:
            missing_dispatch.append(inv_no)
            continue
        if len(inv.get("dispatchItems") or []) > 0:
            present_count += 1
        else:
            missing_dispatch.append(inv_no)
    return {
        "verified_count": len(updated_invoice_nos),
        "dispatch_present_count": present_count,
        "missing_dispatch": missing_dispatch,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill dispatch items for existing invoices from PDF files.")
    parser.add_argument("--folders", nargs="+", required=True, help="One or more folders containing invoice PDFs.")
    parser.add_argument("--base-url", default="http://127.0.0.1:3000", help="ERP API base URL.")
    parser.add_argument("--token", default="", help="Bearer token. If omitted, username/password login is used.")
    parser.add_argument("--username", default="", help="Username for /api/login.")
    parser.add_argument("--password", default="", help="Password for /api/login.")
    parser.add_argument("--mode", choices=["dry-run", "commit"], default="dry-run", help="Dry run or write updates.")
    parser.add_argument("--report-dir", default=".", help="Directory to write JSON/CSV report.")
    parser.add_argument("--timeout-sec", type=int, default=120, help="HTTP timeout per request.")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    report_rows: List[Dict[str, Any]] = []
    summary = BackfillResult()
    updated_invoice_nos: List[str] = []

    headers = build_auth_headers(base_url, args.token, args.username, args.password, args.timeout_sec)
    invoices_map = load_invoices_map(base_url, headers, args.timeout_sec)

    all_pdfs = collect_pdfs(args.folders)
    summary.scanned_pdfs = len(all_pdfs)
    deduped_pdfs, duplicates = dedupe_pdfs(all_pdfs)
    summary.deduped_pdfs = len(deduped_pdfs)

    for dup_src, dup_of in duplicates.items():
        report_rows.append(
            {
                "pdf_path": dup_src,
                "invoice_no": "",
                "status": "dedupe_skipped",
                "message": f"Duplicate of {dup_of}",
                "merged_lines": 0,
                "appended_lines": 0,
            }
        )

    for pdf in deduped_pdfs:
        row_base = {"pdf_path": str(pdf), "invoice_no": "", "merged_lines": 0, "appended_lines": 0}
        try:
            parsed = extract_invoice_from_pdf(base_url, headers, pdf, args.timeout_sec)
            summary.parsed_ok += 1
        except Exception as e:
            summary.parse_failed += 1
            report_rows.append({**row_base, "status": "parse_failed", "message": str(e)})
            continue

        inv_no = str(parsed.get("invNo") or "").strip()
        if not inv_no:
            summary.unmatched_pdfs += 1
            report_rows.append({**row_base, "status": "unmatched_pdf", "message": "No invNo extracted."})
            continue

        row_base["invoice_no"] = inv_no
        existing = invoices_map.get(inv_no)
        if not existing:
            summary.unmatched_pdfs += 1
            report_rows.append({**row_base, "status": "unmatched_pdf", "message": "Invoice number not found in DB."})
            continue

        summary.matched_invoices += 1
        extracted_items = parsed.get("items") or []
        merged_items, merged_lines, appended_lines = merge_dispatch_items(existing.get("dispatchItems") or [], extracted_items)
        row_base["merged_lines"] = merged_lines
        row_base["appended_lines"] = appended_lines
        summary.merged_lines += merged_lines
        summary.appended_lines += appended_lines

        if not merged_lines and not appended_lines:
            summary.skipped_no_dispatch_items += 1
            report_rows.append({**row_base, "status": "no_change", "message": "No mergeable dispatch items extracted."})
            continue

        if args.mode == "dry-run":
            report_rows.append({**row_base, "status": "dry_run_ready", "message": "Would update invoice dispatch items."})
            continue

        payload = build_invoice_update_payload(existing, merged_items)
        try:
            resp = requests.put(
                f"{base_url}/api/invoices/{requests.utils.quote(inv_no, safe='')}",
                headers={**headers, "Content-Type": "application/json"},
                data=json.dumps(payload),
                timeout=args.timeout_sec,
            )
            data = resp.json() if resp.content else {}
            if not resp.ok:
                report_rows.append({**row_base, "status": "update_failed", "message": str(data.get("detail") or resp.text)})
                continue
            summary.updated_invoices += 1
            updated_invoice_nos.append(inv_no)
            report_rows.append({**row_base, "status": "updated", "message": "Invoice updated via existing API flow."})
        except Exception as e:
            report_rows.append({**row_base, "status": "update_failed", "message": str(e)})

    verification = verify_updated_invoices(base_url, headers, updated_invoice_nos, args.timeout_sec) if args.mode == "commit" else {}
    report = {
        "mode": args.mode,
        "base_url": base_url,
        "folders": args.folders,
        "summary": {
            "scanned_pdfs": summary.scanned_pdfs,
            "deduped_pdfs": summary.deduped_pdfs,
            "parsed_ok": summary.parsed_ok,
            "parse_failed": summary.parse_failed,
            "matched_invoices": summary.matched_invoices,
            "unmatched_pdfs": summary.unmatched_pdfs,
            "updated_invoices": summary.updated_invoices,
            "merged_lines": summary.merged_lines,
            "appended_lines": summary.appended_lines,
            "skipped_no_dispatch_items": summary.skipped_no_dispatch_items,
        },
        "verification": verification,
        "rows": report_rows,
    }
    json_path, csv_path = write_report(Path(args.report_dir), report)

    print("[INFO] Dispatch backfill completed.")
    print(json.dumps(report["summary"], indent=2))
    print(f"[INFO] JSON report: {json_path}")
    print(f"[INFO] CSV report : {csv_path}")


if __name__ == "__main__":
    main()

