"""Reconciliation report API (admin only).

Carved out of main.py as part of Workstream 2A. The check functions live in
``reconciliation.py`` at the repo root; this module only exposes them via HTTP.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from main import get_db, get_current_user
from models import User
from reconciliation import run_reconciliation


router = APIRouter(tags=["reconciliation"])


@router.get("/api/reconciliation/summary")
def reconciliation_summary(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    report = run_reconciliation(db)
    report["generated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return report
