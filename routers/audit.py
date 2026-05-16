"""Audit log read API (admin only).

Carved out of main.py as part of Workstream 2A. The append-side
``record_audit()`` helper still lives in main.py because it is invoked from
many existing mutation endpoints; this module owns just the read endpoint.
"""

from __future__ import annotations

import datetime
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

# Imported from main - which is why this module is included from the BOTTOM of
# main.py, after these helpers are declared. See routers/__init__.py.
from main import get_db, get_current_user
from models import AuditLog, User


router = APIRouter(tags=["audit"])


def _serialize(row: AuditLog) -> dict:
    details_obj = None
    if row.details:
        try:
            details_obj = json.loads(row.details)
        except Exception:
            details_obj = row.details
    return {
        "id": row.id,
        "at_utc": row.at_utc.isoformat() if row.at_utc else None,
        "user_id": row.user_id,
        "username": row.username,
        "role": row.role,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "action": row.action,
        "summary": row.summary,
        "details": details_obj,
        "ip_address": row.ip_address,
    }


def _parse_iso(value: str) -> datetime.datetime:
    try:
        return datetime.datetime.fromisoformat(value)
    except Exception:
        raise HTTPException(status_code=400, detail=f"Invalid ISO datetime: {value}")


@router.get("/api/audit-log")
def list_audit_log(
    entity_type: Optional[str] = Query(default=None),
    entity_id: Optional[str] = Query(default=None),
    action: Optional[str] = Query(default=None),
    username: Optional[str] = Query(default=None),
    since: Optional[str] = Query(default=None, description="ISO 8601 datetime; entries at or after this UTC time"),
    until: Optional[str] = Query(default=None, description="ISO 8601 datetime; entries strictly before this UTC time"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    q = db.query(AuditLog)
    if entity_type:
        q = q.filter(AuditLog.entity_type == entity_type)
    if entity_id:
        q = q.filter(AuditLog.entity_id == entity_id)
    if action:
        q = q.filter(AuditLog.action == action)
    if username:
        q = q.filter(AuditLog.username == username)
    if since:
        q = q.filter(AuditLog.at_utc >= _parse_iso(since))
    if until:
        q = q.filter(AuditLog.at_utc < _parse_iso(until))

    total = q.count()
    rows = q.order_by(AuditLog.at_utc.desc(), AuditLog.id.desc()).offset(offset).limit(limit).all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [_serialize(r) for r in rows],
    }
