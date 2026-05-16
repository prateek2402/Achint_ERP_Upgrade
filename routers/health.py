"""Health probe + metrics (unauthenticated, cheap).

Carved out of main.py as part of Workstream 2A. These endpoints are
deliberately public so any uptime monitor / load balancer / dashboard can
poll them without an API key.
"""

from __future__ import annotations

import datetime
import json
import shutil
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

import main as _main
from main import (
    APP_BOOT_TIME, APP_ENV, BACKUP_DIR, BACKUP_INTERVAL_SECONDS, DB_FILE_PATH,
    KEEP_BACKUPS_DAYS, get_db,
)
from models import (
    AuditLog, Client, Invoice, PaymentHistory, PurchaseOrder,
    UploadedDocument, User,
)


router = APIRouter(tags=["ops"])


def _latest_backup_info() -> dict[str, Optional[Any]]:
    info: dict[str, Optional[Any]] = {"path": None, "mtime_utc": None, "age_seconds": None}
    backup_dir = _main.BACKUP_DIR  # read live so test monkeypatch on main.BACKUP_DIR is honoured
    if not backup_dir.exists():
        return info
    candidates = sorted(
        (p for p in backup_dir.glob("erp_database_*.sqlite") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return info
    latest = candidates[0]
    mtime = latest.stat().st_mtime
    info["path"] = str(latest)
    info["mtime_utc"] = datetime.datetime.fromtimestamp(mtime, datetime.UTC).isoformat()
    info["age_seconds"] = max(0, int(time.time() - mtime))
    return info


@router.get("/api/healthz")
def healthz(db: Session = Depends(get_db)):
    """Cheap liveness/readiness probe for uptime monitors and load balancers.

    Returns 200 with a JSON status. The DB ping is the only thing that can flip
    this to 503; everything else is informational.
    """
    started = time.time()
    db_ok = True
    db_error: Optional[str] = None
    try:
        db.execute(text("SELECT 1")).scalar()
    except Exception as exc:
        db_ok = False
        db_error = f"{type(exc).__name__}: {exc}"

    disk_free_bytes: Optional[int] = None
    try:
        disk_free_bytes = shutil.disk_usage(str(DB_FILE_PATH.parent or ".")).free
    except Exception:
        pass

    payload = {
        "status": "ok" if db_ok else "degraded",
        "now_utc": datetime.datetime.now(datetime.UTC).isoformat(),
        "uptime_seconds": int(time.time() - APP_BOOT_TIME),
        "db": {"ok": db_ok, "error": db_error},
        "disk_free_bytes": disk_free_bytes,
        "latest_backup": _latest_backup_info(),
        "elapsed_ms": int((time.time() - started) * 1000),
    }
    if not db_ok:
        return Response(content=json.dumps(payload), status_code=503, media_type="application/json")
    return payload


@router.get("/api/metrics")
def metrics(db: Session = Depends(get_db)):
    """Lightweight counts + uptime, suitable for dashboards.

    Public on purpose (numbers only, no PII). Behind a reverse proxy you can
    restrict by IP if you need to.
    """
    counts = {
        "clients": db.query(Client).count(),
        "purchase_orders": db.query(PurchaseOrder).count(),
        "invoices": db.query(Invoice).count(),
        "payments": db.query(PaymentHistory).count(),
        "users": db.query(User).count(),
        "audit_log_entries": db.query(AuditLog).count(),
        "uploaded_documents": db.query(UploadedDocument).count(),
    }
    return {
        "now_utc": datetime.datetime.now(datetime.UTC).isoformat(),
        "uptime_seconds": int(time.time() - APP_BOOT_TIME),
        "boot_time_utc": datetime.datetime.fromtimestamp(APP_BOOT_TIME, datetime.UTC).isoformat(),
        "counts": counts,
        "latest_backup": _latest_backup_info(),
        "config": {
            "app_env": APP_ENV,
            "backup_keep_days": KEEP_BACKUPS_DAYS,
            "backup_interval_seconds": BACKUP_INTERVAL_SECONDS,
        },
    }
