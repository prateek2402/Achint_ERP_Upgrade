import os
import zipfile
import signal
import asyncio
import time
import re
import hashlib
import hmac
import base64
import secrets
import sqlite3
import shutil
import threading
import io
from pathlib import Path
from collections import defaultdict, deque
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Security, Request, Response, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, func, or_
from sqlalchemy.orm import sessionmaker, Session, joinedload, selectinload
from pydantic import BaseModel
import google.generativeai as genai
import jwt
import datetime
from typing import Optional
import json
from decimal import Decimal, ROUND_HALF_UP
try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
except Exception:
    Workbook = None
    Font = PatternFill = Alignment = None

from models import (
    Base, User, Client, PurchaseOrder, PoBaselineItem, 
    InvoiceDispatchItem, Invoice, PaymentHistory, 
    PaymentAllocation, SystemSettings, UnallocatedPaymentRegister, UnallocatedAdvanceRegister
)

def load_local_env_file():
    env_path = ".env"
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception:
        # Keep startup resilient if .env is malformed.
        pass


load_local_env_file()

# --- Config & Setup ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

DATABASE_URL = "sqlite:///./erp_database.sqlite"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
LEGACY_IMPORT_STATUS_PATH = Path("legacy_import_status.json")
DB_FILE_PATH = Path("erp_database.sqlite")
BACKUP_DIR = Path(os.getenv("DB_BACKUP_DIR", "db_backups"))
BACKUP_INTERVAL_SECONDS = int(os.getenv("DB_BACKUP_INTERVAL_SECONDS", str(24 * 60 * 60)))
_backup_thread_started = False
_write_serialization_lock = threading.Lock()

Base.metadata.create_all(bind=engine)


def ensure_schema_columns():
    conn = sqlite3.connect(str(DB_FILE_PATH))
    cur = conn.cursor()
    try:
        cur.execute("PRAGMA table_info(clients)")
        client_cols = {row[1] for row in cur.fetchall()}
        if "display_currency" not in client_cols:
            cur.execute("ALTER TABLE clients ADD COLUMN display_currency TEXT DEFAULT 'INR'")
        if "exchange_rate" not in client_cols:
            cur.execute("ALTER TABLE clients ADD COLUMN exchange_rate REAL DEFAULT 83.0")

        cur.execute("PRAGMA table_info(purchase_orders)")
        cols = {row[1] for row in cur.fetchall()}
        if "contact_person" not in cols:
            cur.execute("ALTER TABLE purchase_orders ADD COLUMN contact_person TEXT")
        if "project_name" not in cols:
            cur.execute("ALTER TABLE purchase_orders ADD COLUMN project_name TEXT")
        if "is_completed" not in cols:
            cur.execute("ALTER TABLE purchase_orders ADD COLUMN is_completed INTEGER DEFAULT 0")
        if "is_hidden" not in cols:
            cur.execute("ALTER TABLE purchase_orders ADD COLUMN is_hidden INTEGER DEFAULT 0")
        if "completed_at" not in cols:
            cur.execute("ALTER TABLE purchase_orders ADD COLUMN completed_at TEXT")

        cur.execute("PRAGMA table_info(po_baseline_items)")
        baseline_cols = {row[1] for row in cur.fetchall()}
        if "material_type" not in baseline_cols:
            cur.execute("ALTER TABLE po_baseline_items ADD COLUMN material_type TEXT")

        cur.execute("PRAGMA table_info(system_settings)")
        settings_cols = {row[1] for row in cur.fetchall()}
        if "fy_start_month" not in settings_cols:
            cur.execute("ALTER TABLE system_settings ADD COLUMN fy_start_month INTEGER DEFAULT 4")
        if "fy_start_day" not in settings_cols:
            cur.execute("ALTER TABLE system_settings ADD COLUMN fy_start_day INTEGER DEFAULT 1")
        conn.commit()
    finally:
        conn.close()


def perform_database_backup():
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d_%H%M%S")
        backup_path = BACKUP_DIR / f"erp_database_{ts}.sqlite"
        if DB_FILE_PATH.exists():
            shutil.copy2(DB_FILE_PATH, backup_path)
            print(f"[INFO] Daily backup created: {backup_path}")
    except Exception as exc:
        print(f"[WARN] Daily backup failed: {exc}")


def start_daily_backup_worker():
    global _backup_thread_started
    if _backup_thread_started:
        return
    _backup_thread_started = True

    def _worker():
        while True:
            perform_database_backup()
            time.sleep(max(3600, BACKUP_INTERVAL_SECONDS))

    threading.Thread(target=_worker, name="daily-db-backup", daemon=True).start()


ensure_schema_columns()

APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
ENABLE_API_DOCS = os.getenv("ENABLE_API_DOCS", "1" if APP_ENV != "production" else "0").strip() == "1"
app = FastAPI(
    title="Achint ERP API",
    docs_url="/docs" if ENABLE_API_DOCS else None,
    redoc_url="/redoc" if ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_API_DOCS else None
)
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "").strip()
if not SECRET_KEY:
    SECRET_KEY = secrets.token_urlsafe(48)
    print("[WARN] JWT_SECRET_KEY is not set. Using an ephemeral runtime secret; set env var for stable secure authentication.")
JWT_ALGORITHM = "HS256"

PBKDF2_ROUNDS = int(os.getenv("PASSWORD_PBKDF2_ROUNDS", "210000"))
UPLOAD_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("UPLOAD_RATE_LIMIT_WINDOW_SECONDS", "60"))
UPLOAD_RATE_LIMIT_MAX_REQUESTS = int(os.getenv("UPLOAD_RATE_LIMIT_MAX_REQUESTS", "10"))
MAX_UPLOAD_FILES_PER_REQUEST = int(os.getenv("MAX_UPLOAD_FILES_PER_REQUEST", "5"))
MAX_UPLOAD_FILE_SIZE_BYTES = int(os.getenv("MAX_UPLOAD_FILE_SIZE_BYTES", str(10 * 1024 * 1024)))
_upload_rate_limiter: dict[str, deque] = defaultdict(deque)
LOGIN_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("LOGIN_RATE_LIMIT_WINDOW_SECONDS", "300"))
LOGIN_RATE_LIMIT_MAX_FAILURES = int(os.getenv("LOGIN_RATE_LIMIT_MAX_FAILURES", "10"))
_login_failures: dict[str, deque] = defaultdict(deque)

cors_origins_env = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
if cors_origins_env:
    allow_origins = [o.strip() for o in cors_origins_env.split(",") if o.strip()]
else:
    allow_origins = [
        "http://localhost",
        "http://127.0.0.1",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored_password: str) -> bool:
    if not stored_password:
        return False
    if not stored_password.startswith("pbkdf2_sha256$"):
        return hmac.compare_digest(stored_password, password)
    try:
        _, rounds, salt_b64, digest_b64 = stored_password.split("$", 3)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
        return hmac.compare_digest(candidate, expected)
    except Exception:
        return False


ALLOWED_USER_ROLES = {"admin", "logistics", "user"}
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{3,32}$")


def normalize_username(value: str) -> str:
    return (value or "").strip().lower()


def validate_password_strength(password: str):
    pwd = password or ""
    if len(pwd) < 10:
        raise HTTPException(status_code=400, detail="Password must be at least 10 characters long.")
    if not re.search(r"[A-Z]", pwd):
        raise HTTPException(status_code=400, detail="Password must include at least one uppercase letter.")
    if not re.search(r"[a-z]", pwd):
        raise HTTPException(status_code=400, detail="Password must include at least one lowercase letter.")
    if not re.search(r"\d", pwd):
        raise HTTPException(status_code=400, detail="Password must include at least one number.")
    if not re.search(r"[^A-Za-z0-9]", pwd):
        raise HTTPException(status_code=400, detail="Password must include at least one special character.")


def enforce_upload_rate_limit(user_key: str):
    now = time.time()
    q = _upload_rate_limiter[user_key]
    while q and now - q[0] > UPLOAD_RATE_LIMIT_WINDOW_SECONDS:
        q.popleft()
    if len(q) >= UPLOAD_RATE_LIMIT_MAX_REQUESTS:
        raise HTTPException(status_code=429, detail="Upload rate limit exceeded. Please retry later.")
    q.append(now)


def require_pdf_files(files: list[UploadFile]):
    if not files:
        raise HTTPException(status_code=400, detail="At least one PDF file is required.")
    if len(files) > MAX_UPLOAD_FILES_PER_REQUEST:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_UPLOAD_FILES_PER_REQUEST} files allowed per request.")
    for f in files:
        if (f.content_type or "").lower() not in {"application/pdf", "application/x-pdf"}:
            raise HTTPException(status_code=400, detail=f"Invalid content type for {f.filename}. Only PDF is allowed.")


def _client_ip_from_request(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "").strip()
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def enforce_login_rate_limit(user_key: str):
    now = time.time()
    q = _login_failures[user_key]
    while q and now - q[0] > LOGIN_RATE_LIMIT_WINDOW_SECONDS:
        q.popleft()
    if len(q) >= LOGIN_RATE_LIMIT_MAX_FAILURES:
        raise HTTPException(status_code=429, detail="Too many failed login attempts. Please retry later.")


def record_login_failure(user_key: str):
    now = time.time()
    q = _login_failures[user_key]
    while q and now - q[0] > LOGIN_RATE_LIMIT_WINDOW_SECONDS:
        q.popleft()
    q.append(now)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    write_methods = {"POST", "PUT", "PATCH", "DELETE"}
    if request.method.upper() in write_methods:
        with _write_serialization_lock:
            response: Response = await call_next(request)
    else:
        response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- Security Middleware ---
security_scheme = HTTPBearer()

def get_current_user(credentials: HTTPAuthorizationCredentials = Security(security_scheme), db: Session = Depends(get_db)):
    token = credentials.credentials
    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[JWT_ALGORITHM],
            options={"require": ["exp", "id", "type"]}
        )
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type")
        user_id: int = payload.get("id")
        user = db.query(User).filter(User.id == user_id).first()
        if user is None:
            raise HTTPException(status_code=401, detail="User not found")
        return user
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

# --- App Startup: Default Admin ---
@app.on_event("startup")
def startup_event():
    ensure_schema_columns()
    start_daily_backup_worker()
    db = SessionLocal()
    bootstrap_username = os.getenv("BOOTSTRAP_ADMIN_USERNAME", "").strip()
    bootstrap_password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "").strip()
    if bootstrap_username and bootstrap_password:
        admin = db.query(User).filter(User.username == bootstrap_username).first()
    else:
        admin = True
    if not admin:
        default_admin = User(
            username=bootstrap_username,
            hashed_password=hash_password(bootstrap_password),
            role="admin"
        )
        db.add(default_admin)
        db.commit()
        print(f"[INFO] Bootstrap admin account created for user: {bootstrap_username}")
    db.close()

# --- Pydantic Schemas ---
class LoginRequest(BaseModel):
    username: str
    password: str

class UserCreate(BaseModel):
    username: str
    password: str
    role: str

class PasswordChange(BaseModel):
    newPassword: str

class SelfPasswordChange(BaseModel):
    currentPassword: str
    newPassword: str

class ClientCreate(BaseModel):
    name: str

class ClientUpdate(BaseModel):
    name: str
    active: bool


class BaselineItemCreate(BaseModel):
    description: str
    ordered_qty: float
    inspected_qty: Optional[float] = 0.0 # NEW COLUMN
    uom: str
    material_type: Optional[str] = None

class DispatchItemCreate(BaseModel):
    description: str
    qty: float
    uom: str
    inspected_qty: Optional[float] = 0.0

class POCreate(BaseModel):
    client_id: int
    po_no: str
    contact_person: Optional[str] = None
    project_name: Optional[str] = None
    adv_pct: float = 0.0
    ret_pct: float = 0.0
    ret_base: str = "total"
    tds_enabled: bool = False
    tds_rate: float = 0.0
    tds_threshold: float = 0.0
    baseline_items: list[BaselineItemCreate] = []

class InvoiceCreate(BaseModel):
    client_id: int
    po_no: Optional[str] = None
    invoice_no: str
    sub_entity: Optional[str] = None
    lr_no: Optional[str] = None
    inv_date: Optional[str] = None
    due_date: Optional[str] = None
    basic: float = 0.0
    gst: float = 0.0
    total: float = 0.0
    advance_adj: float = 0.0
    tds_ded: float = 0.0
    retention_held: float = 0.0
    net_payable: float = 0.0
    paid: float = 0.0
    balance: float = 0.0
    is_note: bool = False
    note_type: Optional[str] = None
    note_reason: Optional[str] = None
    dispatch_items: list[DispatchItemCreate] = [] # Ensure this matches the frontend key

class InvoiceUpdate(InvoiceCreate):
    pass


class LedgerExportRequest(BaseModel):
    invoice_nos: list[str]


class DispatchItemUpdate(BaseModel):
    description: str
    qty: float
    inspected_qty: Optional[float] = 0.0
    uom: Optional[str] = "Nos"
    merge_on_match: bool = True


class DispatchCellUpsert(BaseModel):
    invoice_no: str
    description: str
    uom: Optional[str] = "Nos"
    qty: float


class DispatchColumnDeleteRequest(BaseModel):
    po_no: str
    description: str
    uom: Optional[str] = None
    client_id: Optional[int] = None


class DispatchColumnRenameRequest(BaseModel):
    po_no: str
    old_description: str
    old_uom: Optional[str] = None
    new_description: str
    new_uom: Optional[str] = None
    client_id: Optional[int] = None


class InvoiceInlineUpdate(BaseModel):
    inv_date: Optional[str] = None
    lr_no: Optional[str] = None
    total: Optional[float] = None

class PaymentUpdate(BaseModel):
    amount: float
    note: str

class PaymentAllocationTarget(BaseModel):
    inv_id: str
    amount: float

class PaymentAllocateRequest(BaseModel):
    client_id: int
    id: str
    date: str
    amount: float = 0.0
    note: Optional[str] = None
    mode: str = "cascade"  # cascade | targeted
    targets: list[PaymentAllocationTarget] = []
    hold_ret: bool = False
    hold_gst: bool = False
    only_gst: bool = False
    apply_adv: bool = False
    advance_only: bool = False
    fund_source: str = "receipt"  # receipt | unallocated
    move_to_po: Optional[str] = None
    po_no: Optional[str] = None
    clear_po_pool: bool = False
    excess_action: str = "park"  # park | allocate_pending

class TransferRequest(BaseModel):
    new_client_id: int
    action: str

class NoteIssueRequest(BaseModel):
    client_id: int
    note_no: str
    date: Optional[str] = None
    note_type: str
    amount: float
    reason: Optional[str] = None
    target_invoice_id: Optional[str] = None


# --- Auth & User Routes ---
@app.post("/api/login")
def login(req: LoginRequest, request: Request, db: Session = Depends(get_db)):
    normalized_username = normalize_username(req.username)
    user_key = f"{_client_ip_from_request(request)}:{normalized_username}"
    enforce_login_rate_limit(user_key)
    user = db.query(User).filter(func.lower(User.username) == normalized_username).first()
    if user and verify_password(req.password, user.hashed_password) and not user.hashed_password.startswith("pbkdf2_sha256$"):
        # Seamless upgrade path for legacy plaintext passwords.
        user.hashed_password = hash_password(req.password)
        db.commit()
    if not user:
        record_login_failure(user_key)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if not verify_password(req.password, user.hashed_password):
        record_login_failure(user_key)
        raise HTTPException(status_code=401, detail="Invalid username or password")

    _login_failures.pop(user_key, None)
    payload = {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "type": "access",
        "iat": datetime.datetime.now(datetime.timezone.utc),
        "nbf": datetime.datetime.now(datetime.timezone.utc),
        "exp": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=12)
    }
    token = jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALGORITHM)
    return {"token": token, "username": user.username, "role": user.role}

@app.get("/api/users")
def get_users(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    users = db.query(User).all()
    return [{"id": u.id, "username": u.username, "role": u.role} for u in users]

@app.post("/api/users")
def create_user(user_data: UserCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    username = (user_data.username or "").strip()
    if not USERNAME_PATTERN.match(username):
        raise HTTPException(status_code=400, detail="Username must be 3-32 chars and use only letters, numbers, dot, underscore, or hyphen.")
    normalized_role = (user_data.role or "").strip().lower()
    if normalized_role not in ALLOWED_USER_ROLES:
        raise HTTPException(status_code=400, detail="Invalid role selected.")
    validate_password_strength(user_data.password)
    existing = db.query(User).filter(func.lower(User.username) == normalize_username(username)).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists.")
    new_user = User(username=username, hashed_password=hash_password(user_data.password), role=normalized_role)
    db.add(new_user)
    db.commit()
    return {"success": True, "id": new_user.id}


@app.get("/api/users/me")
def get_my_account(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "username": current_user.username,
        "role": current_user.role
    }


@app.post("/api/users/change-password")
def change_own_password(req: SelfPasswordChange, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not verify_password(req.currentPassword, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    validate_password_strength(req.newPassword)
    if verify_password(req.newPassword, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="New password must be different from current password.")
    current_user.hashed_password = hash_password(req.newPassword)
    db.commit()
    return {"success": True}


@app.put("/api/users/{user_id}/password")
def admin_reset_password(user_id: int, req: PasswordChange, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    user_to_update = db.query(User).filter(User.id == user_id).first()
    if not user_to_update:
        raise HTTPException(status_code=404, detail="User not found.")
    validate_password_strength(req.newPassword)
    user_to_update.hashed_password = hash_password(req.newPassword)
    db.commit()
    return {"success": True}

@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    if current_user.id == user_id:
        raise HTTPException(status_code=400, detail="Cannot delete own account.")
    user_to_delete = db.query(User).filter(User.id == user_id).first()
    if user_to_delete:
        db.delete(user_to_delete)
        db.commit()
    return {"success": True}

class SettingsSchema(BaseModel):
    exchangeRate: float
    customColumns: list


class FiscalYearConfigSchema(BaseModel):
    startMonth: int
    startDay: int


class ClientCurrencySchema(BaseModel):
    displayCurrency: str
    exchangeRate: float


class POStatusSchema(BaseModel):
    is_completed: bool
    is_hidden: bool = False

# --- PHASE 4: GLOBAL SETTINGS ---
@app.get("/api/settings")
def get_settings(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role == "logistics":
        raise HTTPException(status_code=403, detail="Logistics role cannot access financial settings.")
    settings = db.query(SystemSettings).first()
    if not settings:
        settings = SystemSettings(exchange_rate=83.0, custom_columns="[]")
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return {
        "exchangeRate": settings.exchange_rate,
        "customColumns": json.loads(settings.custom_columns),
        "fiscalYearStart": {"month": int(settings.fy_start_month or 4), "day": int(settings.fy_start_day or 1)}
    }

@app.post("/api/settings")
def update_settings(data: SettingsSchema, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    settings = db.query(SystemSettings).first()
    if not settings:
        settings = SystemSettings()
        db.add(settings)
        
    settings.exchange_rate = data.exchangeRate
    settings.custom_columns = json.dumps(data.customColumns)
    db.commit()
    return {"success": True}


@app.put("/api/settings/fiscal-year")
def update_fiscal_year_settings(data: FiscalYearConfigSchema, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    if data.startMonth < 1 or data.startMonth > 12 or data.startDay < 1 or data.startDay > 31:
        raise HTTPException(status_code=400, detail="Invalid fiscal year start date.")
    settings = db.query(SystemSettings).first()
    if not settings:
        settings = SystemSettings()
        db.add(settings)
    settings.fy_start_month = data.startMonth
    settings.fy_start_day = data.startDay
    db.commit()
    return {"success": True}


@app.put("/api/clients/{client_id}/currency")
def update_client_currency(client_id: int, data: ClientCurrencySchema, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    currency = (data.displayCurrency or "INR").upper().strip()
    if currency not in ("INR", "USD"):
        raise HTTPException(status_code=400, detail="Currency must be INR or USD.")
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    client.display_currency = currency
    client.exchange_rate = max(0.0001, float(data.exchangeRate or 83.0))
    db.commit()
    return {"success": True}


@app.get("/api/admin/legacy-import-status")
def get_legacy_import_status(current_user: User = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    if not LEGACY_IMPORT_STATUS_PATH.exists():
        return {"available": False}
    try:
        payload = json.loads(LEGACY_IMPORT_STATUS_PATH.read_text(encoding="utf-8"))
        return {"available": True, "status": payload}
    except Exception:
        raise HTTPException(status_code=500, detail="Legacy import status file is unreadable.")

# --- PHASE 3: STRICT RELATIONAL CLIENT ENDPOINTS ---
@app.get("/api/clients")
def get_clients(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role == "logistics":
        raise HTTPException(status_code=403, detail="Logistics role cannot access client financial records.")
    clients = db.query(Client).all()
    return [{
        "id": c.id,
        "name": c.name,
        "active": c.active,
        "excess_funds": c.excess_funds,
        "display_currency": c.display_currency or "INR",
        "exchange_rate": c.exchange_rate or 83.0
    } for c in clients]

@app.post("/api/clients")
def create_client(client: ClientCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    existing = db.query(Client).filter(Client.name == client.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Account already exists.")
    new_client = Client(name=client.name, active=True, excess_funds=0.0, display_currency="INR", exchange_rate=83.0)
    db.add(new_client)
    db.commit()
    return {"success": True, "id": new_client.id, "name": new_client.name, "display_currency": "INR", "exchange_rate": 83.0}

@app.delete("/api/clients/{client_id}")
def delete_client(client_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    client = db.query(Client).filter(Client.id == client_id).first()
    if client:
        db.delete(client)
        db.commit()
    return {"success": True}

@app.get("/api/purchase-orders")
def get_purchase_orders(include_completed: bool = True, include_hidden: bool = False, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role == "logistics":
        raise HTTPException(status_code=403, detail="Logistics role cannot access PO financial configuration.")
    query = db.query(PurchaseOrder).options(selectinload(PurchaseOrder.baseline_items))
    if not include_completed:
        query = query.filter(PurchaseOrder.is_completed == False)
    if not include_hidden:
        query = query.filter(PurchaseOrder.is_hidden == False)
    pos = query.all()
    result = []
    for po in pos:
        items = [{
            "description": item.description,
            "ordered_qty": item.ordered_qty,
            "inspected_qty": item.inspected_qty,
            "uom": item.uom,
            "material_type": item.material_type
        } for item in po.baseline_items]
        result.append({
            "id": po.id,
            "client_id": po.client_id,
            "po_no": po.po_no,
            "contact_person": po.contact_person,
            "project_name": po.project_name,
            "is_completed": bool(po.is_completed),
            "is_hidden": bool(po.is_hidden),
            "completed_at": po.completed_at.isoformat() if po.completed_at else None,
            "adv_pct": po.adv_pct,
            "ret_pct": po.ret_pct,
            "ret_base": po.ret_base,
            "tds_enabled": po.tds_enabled,
            "tds_rate": po.tds_rate,
            "tds_threshold": po.tds_threshold,
            "baseline_items": items
        })
    return result

@app.post("/api/purchase-orders")
def create_purchase_order(po: POCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role not in ["admin", "logistics", "user"]:
        raise HTTPException(status_code=403, detail="Authorized operations access required")
    
    existing_po = db.query(PurchaseOrder).filter(PurchaseOrder.po_no == po.po_no).first()
    
    if existing_po:
        # UPSERT: Update the existing lazily-created PO with strict financial terms
        existing_po.contact_person = po.contact_person
        existing_po.project_name = po.project_name
        if current_user.role == "admin":
            existing_po.adv_pct = po.adv_pct
            existing_po.ret_pct = po.ret_pct
            existing_po.ret_base = po.ret_base
            existing_po.tds_enabled = po.tds_enabled
            existing_po.tds_rate = po.tds_rate
            existing_po.tds_threshold = po.tds_threshold
        po_id = existing_po.id
    else:
        # INSERT: Brand new PO from the terms configurator
        new_po = PurchaseOrder(
            client_id=po.client_id, po_no=po.po_no, contact_person=po.contact_person, project_name=po.project_name,
            adv_pct=(po.adv_pct if current_user.role == "admin" else 0.0),
            ret_pct=(po.ret_pct if current_user.role == "admin" else 0.0),
            ret_base=(po.ret_base if current_user.role == "admin" else "total"),
            tds_enabled=(po.tds_enabled if current_user.role == "admin" else False),
            tds_rate=(po.tds_rate if current_user.role == "admin" else 0.0),
            tds_threshold=(po.tds_threshold if current_user.role == "admin" else 0.0)
        )
        db.add(new_po)
        db.flush()
        po_id = new_po.id
    
    # Safely clear and rewrite digital SKU baseline items
    db.query(PoBaselineItem).filter(PoBaselineItem.po_id == po_id).delete()
    
    for item in po.baseline_items:
        mt = (item.material_type or "").strip().lower() if item.material_type else None
        if mt not in ("brick", "castable_mortar"):
            mt = None
        new_item = PoBaselineItem(
            po_id=po_id,
            description=item.description,
            ordered_qty=item.ordered_qty,
            inspected_qty=item.inspected_qty,
            uom=item.uom,
            material_type=mt
        )
        db.add(new_item)
        
    db.commit()
    return {"success": True, "po_id": po_id}


@app.put("/api/purchase-orders/{po_no:path}/status")
def update_purchase_order_status(po_no: str, status: POStatusSchema, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    po_query = db.query(PurchaseOrder).filter(PurchaseOrder.po_no == po_no)
    if payload.client_id:
        po_query = po_query.filter(PurchaseOrder.client_id == payload.client_id)
    po = po_query.first()
    if not po:
        raise HTTPException(status_code=404, detail="PO not found")
    po.is_completed = bool(status.is_completed)
    po.is_hidden = bool(status.is_hidden)
    po.completed_at = datetime.date.today() if po.is_completed else None
    db.commit()
    return {"success": True}

@app.delete("/api/purchase-orders/{po_no:path}")
def delete_purchase_order(po_no: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    po_query = db.query(PurchaseOrder).filter(PurchaseOrder.po_no == po_no)
    if payload.client_id:
        po_query = po_query.filter(PurchaseOrder.client_id == payload.client_id)
    po = po_query.first()
    if po:
        client_id = po.client_id
        db.delete(po)
        db.commit()
        recalculate_client_ledger(client_id, db)
    return {"success": True}


@app.get("/api/logistics/dispatch-summary")
def get_logistics_dispatch_summary(
    status: Optional[str] = Query(default="", description="pending | cleared | empty"),
    search: Optional[str] = Query(default="", description="Search by client name or PO number"),
    sort_by: Optional[str] = Query(default="pending_qty", description="pending_qty | completion | client | po_no"),
    sort_dir: Optional[str] = Query(default="desc", description="asc | desc"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if current_user.role not in ["admin", "logistics"]:
        raise HTTPException(status_code=403, detail="Admin or Logistics access required")

    def _empty_dispatch_summary():
        return {
            "overview": {
                "total_statements": 0,
                "pending_statements": 0,
                "total_pending_qty": 0.0,
                "total_ordered_qty": 0.0,
                "total_dispatched_qty": 0.0,
                "completion_pct": 0.0
            },
            "rows": []
        }

    active_clients = db.query(Client).filter(or_(Client.active == True, Client.active.is_(None))).all()
    if not active_clients:
        return _empty_dispatch_summary()

    client_by_id = {c.id: c.name for c in active_clients}
    client_ids = list(client_by_id.keys())

    pos = db.query(PurchaseOrder).options(selectinload(PurchaseOrder.baseline_items)).filter(
        PurchaseOrder.client_id.in_(client_ids)
    ).all()

    po_by_id = {po.id: po for po in pos}
    po_ids = list(po_by_id.keys())
    if not po_ids:
        return _empty_dispatch_summary()

    invoices = db.query(Invoice).options(selectinload(Invoice.dispatch_items)).filter(
        Invoice.po_id.in_(po_ids),
        Invoice.is_note == False
    ).all()

    dispatch_map: dict[tuple[int, str], float] = defaultdict(float)
    dispatch_inspected_map: dict[tuple[int, str], float] = defaultdict(float)
    po_dispatch_totals: dict[int, float] = defaultdict(float)
    po_dispatch_descriptions: dict[int, set[str]] = defaultdict(set)
    for inv in invoices:
        if not inv.po_id:
            continue
        for item in inv.dispatch_items:
            key = (inv.po_id, (item.description or "").strip().upper())
            if key[1]:
                dispatch_map[key] += float(item.dispatched_qty or 0.0)
                dispatch_inspected_map[key] += float(item.inspected_qty or 0.0)
                po_dispatch_totals[inv.po_id] += float(item.dispatched_qty or 0.0)
                po_dispatch_descriptions[inv.po_id].add(key[1])

    rows = []
    total_ordered_qty = 0.0
    total_dispatched_qty = 0.0
    total_pending_qty = 0.0
    pending_statements = 0

    for po in pos:
        baseline_items = po.baseline_items or []
        if not baseline_items:
            # Legacy-safe fallback: show statement rows even when baseline lines are missing.
            dispatched_fallback = float(po_dispatch_totals.get(po.id, 0.0))
            if dispatched_fallback <= 0.0001:
                continue
            rows.append({
                "client": client_by_id.get(po.client_id, "Unknown"),
                "po_no": po.po_no,
                "project_name": po.project_name,
                "contact_person": po.contact_person,
                "material_lines": len(po_dispatch_descriptions.get(po.id, set())),
                "pending_lines": 0,
                "ordered_qty": dispatched_fallback,
                "dispatched_qty": dispatched_fallback,
                "inspected_qty": 0.0,
                "pending_qty": 0.0,
                "completion": 100.0
            })
            total_ordered_qty += dispatched_fallback
            total_dispatched_qty += dispatched_fallback
            continue

        ordered_qty = 0.0
        dispatched_qty = 0.0
        inspected_qty = 0.0
        pending_qty = 0.0
        pending_lines = 0

        for base in baseline_items:
            ordered = float(base.ordered_qty or 0.0)
            inspected = float(base.inspected_qty or 0.0)
            desc_key = (base.description or "").strip().upper()
            dispatched = dispatch_map.get((po.id, desc_key), 0.0) if desc_key else 0.0
            inspected_actual = dispatch_inspected_map.get((po.id, desc_key), 0.0) if desc_key else 0.0
            pending = max(0.0, ordered - dispatched)

            ordered_qty += ordered
            dispatched_qty += dispatched
            inspected_qty += max(inspected, inspected_actual)
            pending_qty += pending
            if pending > 0.0001:
                pending_lines += 1

        completion = min(100.0, (dispatched_qty / ordered_qty) * 100.0) if ordered_qty > 0 else 0.0
        if pending_qty > 0.0001:
            pending_statements += 1

        total_ordered_qty += ordered_qty
        total_dispatched_qty += dispatched_qty
        total_pending_qty += pending_qty

        rows.append({
            "client": client_by_id.get(po.client_id, "Unknown"),
            "po_no": po.po_no,
            "project_name": po.project_name,
            "contact_person": po.contact_person,
            "material_lines": len(baseline_items),
            "pending_lines": pending_lines,
            "ordered_qty": ordered_qty,
            "dispatched_qty": dispatched_qty,
            "inspected_qty": inspected_qty,
            "pending_qty": pending_qty,
            "completion": completion
        })

    status_val = (status or "").strip().lower()
    search_val = (search or "").strip().lower()

    filtered_rows = []
    for row in rows:
        if status_val == "pending" and row["pending_qty"] <= 0.0001:
            continue
        if status_val == "cleared" and row["pending_qty"] > 0.0001:
            continue
        if search_val and search_val not in row["client"].lower() and search_val not in row["po_no"].lower():
            continue
        filtered_rows.append(row)

    sort_key = (sort_by or "pending_qty").strip().lower()
    reverse_sort = (sort_dir or "desc").strip().lower() != "asc"
    if sort_key == "completion":
        filtered_rows.sort(key=lambda x: x["completion"], reverse=reverse_sort)
    elif sort_key == "client":
        filtered_rows.sort(key=lambda x: x["client"].lower(), reverse=reverse_sort)
    elif sort_key == "po_no":
        filtered_rows.sort(key=lambda x: x["po_no"].lower(), reverse=reverse_sort)
    else:
        filtered_rows.sort(key=lambda x: x["pending_qty"], reverse=reverse_sort)

    completion_pct = (min(total_dispatched_qty, total_ordered_qty) / total_ordered_qty * 100.0) if total_ordered_qty > 0 else 0.0
    return {
        "overview": {
            "total_statements": len(rows),
            "pending_statements": pending_statements,
            "total_pending_qty": total_pending_qty,
            "total_ordered_qty": total_ordered_qty,
            "total_dispatched_qty": total_dispatched_qty,
            "completion_pct": completion_pct
        },
        "rows": filtered_rows
    }


@app.get("/api/logistics/dispatch-detail")
def get_logistics_dispatch_detail(
    client: str = Query(..., description="Client name"),
    po_no: str = Query(..., description="Purchase order number"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if current_user.role not in ["admin", "logistics"]:
        raise HTTPException(status_code=403, detail="Admin or Logistics access required")

    client_name = (client or "").strip()
    po_no_val = (po_no or "").strip()
    if not client_name or not po_no_val:
        raise HTTPException(status_code=400, detail="client and po_no are required")

    po = db.query(PurchaseOrder).options(selectinload(PurchaseOrder.baseline_items)).join(Client).filter(
        Client.name == client_name,
        Client.active == True,
        PurchaseOrder.po_no == po_no_val
    ).first()
    if not po:
        raise HTTPException(status_code=404, detail="Dispatch statement not found")

    invoices = db.query(Invoice).options(selectinload(Invoice.dispatch_items)).filter(
        Invoice.po_id == po.id,
        Invoice.is_note == False
    ).all()

    dispatch_by_desc: dict[str, float] = defaultdict(float)
    invoice_rows = []
    for inv in invoices:
        for item in inv.dispatch_items:
            key = (item.description or "").strip().upper()
            qty = float(item.dispatched_qty or 0.0)
            if key:
                dispatch_by_desc[key] += qty
            invoice_rows.append({
                "dispatch_item_id": item.id,
                "invoice_no": inv.invoice_no,
                "invoice_date": inv.inv_date.isoformat() if inv.inv_date else None,
                "description": item.description or "",
                "dispatched_qty": qty,
                "inspected_qty": float(item.inspected_qty or 0.0),
                "uom": item.uom or ""
            })

    summary_rows = []
    total_ordered = 0.0
    total_dispatched = 0.0
    for base in (po.baseline_items or []):
        ordered = float(base.ordered_qty or 0.0)
        dispatched = float(dispatch_by_desc.get((base.description or "").strip().upper(), 0.0))
        pending = max(0.0, ordered - dispatched)
        total_ordered += ordered
        total_dispatched += dispatched
        summary_rows.append({
            "description": base.description or "",
            "uom": base.uom or "",
            "ordered_qty": ordered,
            "inspected_qty": float(base.inspected_qty or 0.0),
            "material_type": base.material_type or None,
            "dispatched_qty": dispatched,
            "pending_qty": pending
        })

    invoice_rows.sort(key=lambda r: (r["invoice_date"] or "9999-12-31", r["invoice_no"], r["dispatch_item_id"]))
    completion = min(100.0, (total_dispatched / total_ordered) * 100.0) if total_ordered > 0 else 0.0
    return {
        "overview": {
            "client": client_name,
            "po_no": po_no_val,
            "project_name": po.project_name,
            "contact_person": po.contact_person,
            "ordered_qty": total_ordered,
            "dispatched_qty": total_dispatched,
            "pending_qty": max(0.0, total_ordered - total_dispatched),
            "completion_pct": completion
        },
        "summary_rows": summary_rows,
        "invoice_rows": invoice_rows
    }


@app.get("/api/dispatch/export-xlsx")
def export_dispatch_detail_xlsx(
    client: str = Query(..., description="Client name"),
    po_no: str = Query(..., description="Purchase order number"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if Workbook is None:
        raise HTTPException(status_code=500, detail="Excel export dependency missing. Install openpyxl.")
    if current_user.role not in ["admin", "logistics", "user"]:
        raise HTTPException(status_code=403, detail="Authorized operations access required")

    client_name = (client or "").strip()
    po_no_val = (po_no or "").strip()
    if not client_name or not po_no_val:
        raise HTTPException(status_code=400, detail="client and po_no are required")

    po = db.query(PurchaseOrder).options(selectinload(PurchaseOrder.baseline_items)).join(Client).filter(
        Client.name == client_name,
        PurchaseOrder.po_no == po_no_val
    ).first()
    if not po:
        raise HTTPException(status_code=404, detail="Dispatch statement not found")

    invoices = db.query(Invoice).options(selectinload(Invoice.dispatch_items)).filter(
        Invoice.po_id == po.id,
        Invoice.is_note == False
    ).all()

    dispatch_by_desc: dict[str, float] = defaultdict(float)
    invoice_rows = []
    for inv in invoices:
        for item in inv.dispatch_items:
            key = (item.description or "").strip().upper()
            qty = float(item.dispatched_qty or 0.0)
            if key:
                dispatch_by_desc[key] += qty
            invoice_rows.append({
                "invoice_no": inv.invoice_no,
                "invoice_date": inv.inv_date.isoformat() if inv.inv_date else "",
                "description": item.description or "",
                "dispatched_qty": qty,
                "inspected_qty": float(item.inspected_qty or 0.0),
                "uom": item.uom or ""
            })

    summary_rows = []
    total_ordered = 0.0
    total_dispatched = 0.0
    for base in (po.baseline_items or []):
        ordered = float(base.ordered_qty or 0.0)
        dispatched = float(dispatch_by_desc.get((base.description or "").strip().upper(), 0.0))
        pending = max(0.0, ordered - dispatched)
        total_ordered += ordered
        total_dispatched += dispatched
        summary_rows.append({
            "description": base.description or "",
            "uom": base.uom or "",
            "ordered_qty": ordered,
            "inspected_qty": float(base.inspected_qty or 0.0),
            "material_type": base.material_type or None,
            "dispatched_qty": dispatched,
            "pending_qty": pending
        })

    wb = Workbook()
    ws_summary = wb.active
    ws_summary.title = "Summary"
    ws_invoice = wb.create_sheet("InvoiceWise")

    header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    title_font = Font(bold=True, size=12)

    ws_summary["A1"] = f"Dispatch Statement: {client_name} | PO: {po_no_val}"
    ws_summary["A1"].font = title_font
    ws_summary.merge_cells("A1:F1")
    ws_summary.append(["Material Description", "UOM", "Ordered Qty", "Dispatched Qty", "Inspected Qty", "Pending Qty"])
    for c in ws_summary[2]:
        c.fill = header_fill
        c.font = header_font
        c.alignment = Alignment(horizontal="center")
    for row in summary_rows:
        ws_summary.append([
            row["description"],
            row["uom"],
            round(row["ordered_qty"], 2),
            round(row["dispatched_qty"], 2),
            round(row["inspected_qty"], 2),
            round(row["pending_qty"], 2),
        ])
    ws_summary.append(["TOTAL", "", round(total_ordered, 2), round(total_dispatched, 2), "", round(max(0.0, total_ordered - total_dispatched), 2)])
    for c in ws_summary[ws_summary.max_row]:
        c.font = Font(bold=True)

    ws_invoice["A1"] = f"Invoice-wise Dispatch: {client_name} | PO: {po_no_val}"
    ws_invoice["A1"].font = title_font
    ws_invoice.merge_cells("A1:F1")
    ws_invoice.append(["Invoice No", "Invoice Date", "Material Description", "Dispatched Qty", "Inspected Qty", "UOM"])
    for c in ws_invoice[2]:
        c.fill = header_fill
        c.font = header_font
        c.alignment = Alignment(horizontal="center")
    for row in invoice_rows:
        ws_invoice.append([
            row["invoice_no"],
            row["invoice_date"],
            row["description"],
            round(row["dispatched_qty"], 2),
            round(row["inspected_qty"], 2),
            row["uom"],
        ])

    for ws in [ws_summary, ws_invoice]:
        ws.column_dimensions["A"].width = 48
        ws.column_dimensions["B"].width = 14
        ws.column_dimensions["C"].width = 16
        ws.column_dimensions["D"].width = 16
        ws.column_dimensions["E"].width = 16
        ws.column_dimensions["F"].width = 16

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    safe_client = re.sub(r"[^A-Za-z0-9._-]+", "_", client_name)
    safe_po = re.sub(r"[^A-Za-z0-9._-]+", "_", po_no_val)
    filename = f"dispatch_{safe_client}_{safe_po}.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@app.post("/api/ledger/export-xlsx")
def export_ledger_xlsx(
    payload: LedgerExportRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if Workbook is None:
        raise HTTPException(status_code=500, detail="Excel export dependency missing. Install openpyxl.")
    if current_user.role == "logistics":
        raise HTTPException(status_code=403, detail="Logistics role cannot access financial invoice data.")

    invoice_nos = [str(x or "").strip() for x in (payload.invoice_nos or []) if str(x or "").strip()]
    if not invoice_nos:
        raise HTTPException(status_code=400, detail="No invoice numbers provided for export.")

    invoices = db.query(Invoice).options(joinedload(Invoice.client), joinedload(Invoice.purchase_order)).filter(
        Invoice.invoice_no.in_(invoice_nos)
    ).all()
    if not invoices:
        raise HTTPException(status_code=404, detail="No matching invoices found for export.")

    invoice_map = {inv.invoice_no: inv for inv in invoices}
    ordered_invoices = [invoice_map[i] for i in invoice_nos if i in invoice_map]

    wb = Workbook()
    ws = wb.active
    ws.title = "FinancialLedger"

    header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    title_font = Font(bold=True, size=12)

    ws["A1"] = f"Financial Ledger Export | Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
    ws["A1"].font = title_font
    ws.merge_cells("A1:Q1")

    headers = [
        "Client", "Sub Entity", "PO No", "Invoice No", "Invoice Date", "Due Date", "Status",
        "LR No", "Basic", "GST", "Gross", "Advance", "TDS", "Retention", "Net Payable",
        "Payment Received", "Balance"
    ]
    ws.append(headers)
    for c in ws[2]:
        c.fill = header_fill
        c.font = header_font
        c.alignment = Alignment(horizontal="center")

    today = datetime.date.today()
    totals = {
        "basic": 0.0, "gst": 0.0, "gross": 0.0, "advance": 0.0, "tds": 0.0,
        "retention": 0.0, "net": 0.0, "paid": 0.0, "balance": 0.0
    }
    for inv in ordered_invoices:
        inv_date = inv.inv_date.isoformat() if inv.inv_date else ""
        due_date = inv.due_date.isoformat() if inv.due_date else ""
        balance = float(inv.balance or 0.0)
        status = "CLEARED"
        if balance > 0.0001:
            if inv.due_date and inv.due_date < today:
                status = "OVERDUE"
            else:
                status = "PENDING"

        basic = float(inv.basic or 0.0)
        gst = float(inv.gst or 0.0)
        gross = float(inv.total or 0.0)
        advance = float(inv.advance_adj or 0.0)
        tds = float(inv.tds_ded or 0.0)
        retention = float(inv.retention_held or 0.0)
        net = float(inv.net_payable or 0.0)
        paid = float(inv.paid or 0.0)

        totals["basic"] += basic
        totals["gst"] += gst
        totals["gross"] += gross
        totals["advance"] += advance
        totals["tds"] += tds
        totals["retention"] += retention
        totals["net"] += net
        totals["paid"] += paid
        totals["balance"] += balance

        ws.append([
            inv.client.name if inv.client else "",
            inv.sub_entity or "",
            inv.purchase_order.po_no if inv.purchase_order else "UNASSIGNED",
            inv.invoice_no,
            inv_date,
            due_date,
            status,
            inv.lr_no or "",
            round(basic, 2),
            round(gst, 2),
            round(gross, 2),
            round(advance, 2),
            round(tds, 2),
            round(retention, 2),
            round(net, 2),
            round(paid, 2),
            round(balance, 2),
        ])

    ws.append([
        "TOTAL", "", "", "", "", "", "", "",
        round(totals["basic"], 2),
        round(totals["gst"], 2),
        round(totals["gross"], 2),
        round(totals["advance"], 2),
        round(totals["tds"], 2),
        round(totals["retention"], 2),
        round(totals["net"], 2),
        round(totals["paid"], 2),
        round(totals["balance"], 2),
    ])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)

    widths = [24, 18, 16, 18, 14, 14, 12, 16, 12, 10, 12, 12, 10, 12, 12, 16, 12]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + i)].width = w

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    filename = f"financial_ledger_export_{datetime.date.today().isoformat()}.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@app.get("/api/logistics/client-dispatch-workspace")
def get_logistics_client_dispatch_workspace(
    client: str = Query(..., description="Client name"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if current_user.role not in ["admin", "logistics"]:
        raise HTTPException(status_code=403, detail="Admin or Logistics access required")

    client_name = (client or "").strip()
    if not client_name:
        raise HTTPException(status_code=400, detail="client is required")

    db_client = db.query(Client).filter(Client.name == client_name).first()
    if not db_client:
        raise HTTPException(status_code=404, detail="Client not found")

    pos = db.query(PurchaseOrder).options(selectinload(PurchaseOrder.baseline_items)).filter(
        PurchaseOrder.client_id == db_client.id
    ).all()

    po_terms = []
    po_by_id = {}
    for po in pos:
        po_by_id[po.id] = po
        po_terms.append({
            "po_no": po.po_no,
            "project_name": po.project_name or "",
            "contact_person": po.contact_person or "",
            "is_completed": bool(po.is_completed),
            "is_hidden": bool(po.is_hidden),
            "completed_at": po.completed_at.isoformat() if po.completed_at else None,
            "baseline_items": [
                {
                    "description": item.description or "",
                    "ordered_qty": float(item.ordered_qty or 0.0),
                    "inspected_qty": float(item.inspected_qty or 0.0),
                    "uom": item.uom or "Nos",
                    "material_type": item.material_type or None
                } for item in (po.baseline_items or [])
            ]
        })

    invoices = db.query(Invoice).options(selectinload(Invoice.dispatch_items)).filter(
        Invoice.client_id == db_client.id,
        Invoice.is_note == False
    ).all()

    invoices_payload = []
    for inv in invoices:
        po_no = ""
        if inv.po_id and inv.po_id in po_by_id:
            po_no = po_by_id[inv.po_id].po_no
        invoices_payload.append({
            "id": inv.invoice_no,
            "invoice_no": inv.invoice_no,
            "po_no": po_no or "UNASSIGNED",
            "sub_entity": inv.sub_entity or "",
            "lr_no": inv.lr_no or "",
            "inv_date": inv.inv_date.isoformat() if inv.inv_date else None,
            "due_date": inv.due_date.isoformat() if inv.due_date else None,
            "total": float(inv.total or 0.0),
            "dispatch_items": [
                {
                    "id": d.id,
                    "description": d.description or "",
                    "qty": float(d.dispatched_qty or 0.0),
                    "inspected_qty": float(d.inspected_qty or 0.0),
                    "uom": d.uom or "Nos"
                } for d in (inv.dispatch_items or [])
            ]
        })

    return {
        "client": {
            "id": db_client.id,
            "name": db_client.name,
            "active": bool(db_client.active if db_client.active is not None else True)
        },
        "po_terms": po_terms,
        "invoices": invoices_payload
    }



def normalize_dispatch_description(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]+", " ", (value or "").upper())).strip()


def prune_orphan_baseline_items_for_po(po_id: int, db: Session) -> int:
    po = db.query(PurchaseOrder).options(selectinload(PurchaseOrder.baseline_items)).filter(PurchaseOrder.id == po_id).first()
    if not po:
        return 0

    invoices = db.query(Invoice).options(selectinload(Invoice.dispatch_items)).filter(
        Invoice.po_id == po_id,
        Invoice.is_note == False
    ).all()

    dispatch_qty_by_desc: dict[str, float] = defaultdict(float)
    for inv in invoices:
        for item in inv.dispatch_items:
            key = normalize_dispatch_description(item.description or "")
            if key:
                dispatch_qty_by_desc[key] += float(item.dispatched_qty or 0.0)

    deleted = 0
    for base in list(po.baseline_items or []):
        key = normalize_dispatch_description(base.description or "")
        ordered = float(base.ordered_qty or 0.0)
        inspected = float(base.inspected_qty or 0.0)
        dispatched = float(dispatch_qty_by_desc.get(key, 0.0))
        # Auto-generated/material-only rows should disappear once fully consumed.
        if ordered <= 0.0001 and inspected <= 0.0001 and dispatched <= 0.0001:
            db.delete(base)
            deleted += 1

    return deleted


def ensure_baseline_from_dispatch(po_id: Optional[int], dispatch_items: list[DispatchItemCreate], db: Session) -> int:
    if not po_id:
        return 0
    po = db.query(PurchaseOrder).options(selectinload(PurchaseOrder.baseline_items)).filter(PurchaseOrder.id == po_id).first()
    if not po:
        return 0

    existing_by_key: dict[tuple[str, str], PoBaselineItem] = {}
    for base in (po.baseline_items or []):
        key = (normalize_dispatch_description(base.description or ""), (base.uom or "").strip().upper())
        existing_by_key[key] = base

    inserted = 0
    for item in (dispatch_items or []):
        desc = (item.description or "").strip()
        if not desc:
            continue
        norm = normalize_dispatch_description(desc)
        uom = (item.uom or "Nos").strip()
        key = (norm, uom.upper())
        if key in existing_by_key:
            continue
        new_base = PoBaselineItem(
            po_id=po.id,
            description=desc,
            ordered_qty=0.0,
            inspected_qty=float(item.inspected_qty or 0.0),
            uom=uom
        )
        db.add(new_base)
        existing_by_key[key] = new_base
        inserted += 1
    return inserted


def best_dispatch_description_match(raw_desc: str, candidates: list[str]) -> Optional[str]:
    norm_raw = normalize_dispatch_description(raw_desc)
    if not norm_raw or not candidates:
        return None

    exact = next((c for c in candidates if normalize_dispatch_description(c) == norm_raw), None)
    if exact:
        return exact

    raw_tokens = set(norm_raw.split())
    best = None
    best_score = 0.0
    for candidate in candidates:
        norm_c = normalize_dispatch_description(candidate)
        if not norm_c:
            continue
        if norm_c in norm_raw or norm_raw in norm_c:
            score = min(len(norm_raw), len(norm_c)) / max(len(norm_raw), len(norm_c))
            if score > best_score:
                best_score = score
                best = candidate
            continue
        cand_tokens = norm_c.split()
        if not cand_tokens:
            continue
        common = sum(1 for t in cand_tokens if t in raw_tokens)
        score = common / max(1, min(len(cand_tokens), len(raw_tokens)))
        if score > best_score:
            best_score = score
            best = candidate
    return best if best_score >= 0.75 else None


def align_existing_dispatch_schema(db: Session) -> dict:
    po_list = db.query(PurchaseOrder).options(selectinload(PurchaseOrder.baseline_items)).all()
    updated_dispatch_rows = 0
    inserted_baseline_rows = 0
    scanned_dispatch_rows = 0

    for po in po_list:
        baseline_items = po.baseline_items or []
        baseline_by_norm = {}
        for b in baseline_items:
            key = normalize_dispatch_description(b.description or "")
            if key and key not in baseline_by_norm:
                baseline_by_norm[key] = b

        baseline_candidates = [b.description for b in baseline_items if b.description]
        invoices = db.query(Invoice).options(selectinload(Invoice.dispatch_items)).filter(
            Invoice.po_id == po.id,
            Invoice.is_note == False
        ).all()
        for inv in invoices:
            for item in inv.dispatch_items:
                scanned_dispatch_rows += 1
                source_desc = (item.description or "").strip()
                if not source_desc:
                    continue
                matched = best_dispatch_description_match(source_desc, baseline_candidates)
                if matched and source_desc != matched:
                    item.description = matched
                    updated_dispatch_rows += 1
                    source_desc = matched
                norm_desc = normalize_dispatch_description(source_desc)
                if norm_desc and norm_desc not in baseline_by_norm:
                    new_base = PoBaselineItem(
                        po_id=po.id,
                        description=source_desc,
                        ordered_qty=0.0,
                        inspected_qty=0.0,
                        uom=item.uom or "Nos"
                    )
                    db.add(new_base)
                    baseline_by_norm[norm_desc] = new_base
                    baseline_candidates.append(source_desc)
                    inserted_baseline_rows += 1

    pruned_baseline_rows = 0
    for po in po_list:
        pruned_baseline_rows += prune_orphan_baseline_items_for_po(po.id, db)

    db.commit()
    return {
        "success": True,
        "po_count": len(po_list),
        "scanned_dispatch_rows": scanned_dispatch_rows,
        "dispatch_rows_aligned": updated_dispatch_rows,
        "baseline_rows_added": inserted_baseline_rows,
        "baseline_rows_pruned": pruned_baseline_rows
    }


@app.post("/api/dispatch/align-schema")
def align_dispatch_schema(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return align_existing_dispatch_schema(db)


def recalculate_client_ledger(client_id: int, db: Session):
    """
    The Master Math Engine: Calculates all invoices, deducts payments, 
    and locks the true balances directly into the SQL database.
    """
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client: return

    invoices = db.query(Invoice).filter(Invoice.client_id == client_id).all()
    # Create a fast lookup dictionary mapping invoice_no string to the SQL object
    inv_map = {inv.invoice_no: inv for inv in invoices}

    # 1. Reset all invoice balances to their baseline
    for inv in invoices:
        inv.net_payable = (inv.total or 0.0) - (inv.advance_adj or 0.0) - (inv.tds_ded or 0.0) - (inv.retention_held or 0.0)
        inv.paid = 0.0
        inv.balance = inv.net_payable

    # 2. Distribute Payments
    payments = db.query(PaymentHistory).filter(PaymentHistory.client_id == client_id).all()
    total_excess = 0.0

    for pay in payments:
        allocations = db.query(PaymentAllocation).filter(PaymentAllocation.payment_id == pay.id).all()
        alloc_sum = 0.0
        
        for al in allocations:
            if al.alloc_type == 'invoice' and al.target_inv_id in inv_map:
                inv = inv_map[al.target_inv_id]
                inv.paid += al.amount
                inv.balance -= al.amount
            # Treat PO parking as allocated receipt (not unallocated excess).
            if al.alloc_type in ('invoice', 'po_advance', 'po_advance_applied', 'note_allocation'):
                alloc_sum += al.amount
        
        # 3. Calculate Unallocated / Excess Funds
        if pay.type == 'RECEIPT':
            unallocated = pay.amount - alloc_sum
            if unallocated > 0:
                total_excess += unallocated
        elif pay.type == 'UNALLOCATED_APPLIED':
            # This log consumes previously accumulated unallocated funds.
            total_excess -= alloc_sum

    # Auto-clear tiny residuals to avoid operational noise in ledger.
    for inv in invoices:
        bal = float(inv.balance or 0.0)
        if 0.0 < bal < 5.0:
            inv.paid = float(inv.paid or 0.0) + bal
            inv.balance = 0.0

    client.excess_funds = max(0.0, total_excess)
    db.commit()


def round_inr_nearest(value: Optional[float]) -> float:
    amt = Decimal(str(float(value or 0.0)))
    return float(amt.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fiscal_year_label(inv_date: Optional[datetime.date], start_month: int, start_day: int) -> Optional[str]:
    if not inv_date:
        return None
    pivot = datetime.date(inv_date.year, start_month, start_day)
    start_year = inv_date.year if inv_date >= pivot else inv_date.year - 1
    return f"FY{start_year}-{str(start_year + 1)[-2:]}"


def parse_fy_filters(raw: Optional[str]) -> set[str]:
    if not raw:
        return set()
    return {part.strip() for part in raw.split(",") if part.strip()}



@app.get("/api/invoices")
def get_invoices(fiscal_years: Optional[str] = Query(default=None, description="Comma separated FY labels"), include_completed_po: bool = True, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role == "logistics":
        raise HTTPException(status_code=403, detail="Logistics role cannot access financial invoice data.")
    invoices = db.query(Invoice).options(
        joinedload(Invoice.purchase_order),
        selectinload(Invoice.dispatch_items)
    ).all()
    settings = db.query(SystemSettings).first()
    fy_start_month = int((settings.fy_start_month if settings else 4) or 4)
    fy_start_day = int((settings.fy_start_day if settings else 1) or 1)
    fy_filters = parse_fy_filters(fiscal_years)
    note_invoice_nos = [inv.invoice_no for inv in invoices if inv.is_note]
    note_target_by_note_id: dict[str, str] = {}
    if note_invoice_nos:
        note_allocs = db.query(PaymentAllocation).filter(
            PaymentAllocation.alloc_type == "note_allocation",
            PaymentAllocation.note_id.in_(note_invoice_nos)
        ).all()
        for alloc in note_allocs:
            note_id = (alloc.note_id or "").strip()
            target_inv = (alloc.target_inv_id or "").strip()
            if note_id and target_inv and note_id not in note_target_by_note_id:
                note_target_by_note_id[note_id] = target_inv
    result = []
    for inv in invoices:
        po_str = 'UNASSIGNED'
        po_completed = False
        if inv.purchase_order:
            po_str = inv.purchase_order.po_no
            po_completed = bool(inv.purchase_order.is_completed)
        elif inv.po_id:
            po_obj = db.query(PurchaseOrder).filter(PurchaseOrder.id == inv.po_id).first()
            if po_obj:
                po_str = po_obj.po_no
                po_completed = bool(po_obj.is_completed)
        if (not include_completed_po) and po_completed:
            continue
        inv_fy = fiscal_year_label(inv.inv_date, fy_start_month, fy_start_day)
        if fy_filters and inv_fy not in fy_filters:
            continue

        # --- FETCH ATTACHED DISPATCH ITEMS ---
        d_items = []
        for item in inv.dispatch_items:
            d_items.append({
                "id": item.id,
                "description": item.description,
                "qty": item.dispatched_qty,
                "inspected_qty": item.inspected_qty,
                "uom": item.uom
            })

        result.append({
            "sql_id": inv.id,
            "client_id": inv.client_id,
            "poNo": po_str,
            "id": inv.invoice_no,
            "subEntity": inv.sub_entity,
            "lrNo": inv.lr_no,
            "invDate": inv.inv_date.isoformat() if inv.inv_date else None,
            "dueDate": inv.due_date.isoformat() if inv.due_date else None,
            "basic": inv.basic,
            "gst": inv.gst,
            "total": inv.total,
            "advance": inv.advance_adj,
            "tds": inv.tds_ded,
            "retention": inv.retention_held,
            "netPayable": inv.net_payable,
            "paid": inv.paid,
            "balance": inv.balance,
            "isNote": inv.is_note,
            "noteType": inv.note_type,
            "noteReason": inv.note_reason,
            "noteTargetInvoice": note_target_by_note_id.get(inv.invoice_no),
            "migratedV3": True,
            "fiscalYear": inv_fy,
            "dispatchItems": d_items # CRITICAL: Sends items to frontend memory
        })
    return result

@app.post("/api/invoices")
def create_invoice(inv: InvoiceCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role not in ["admin", "logistics", "user"]:
        raise HTTPException(status_code=403, detail="Authorized operations access required")
    
    existing = db.query(Invoice).filter(Invoice.invoice_no == inv.invoice_no).first()
    if existing:
        raise HTTPException(status_code=400, detail="Invoice number already exists.")

    po_id = None
    if inv.po_no and inv.po_no.strip() and inv.po_no != 'UNASSIGNED':
        po = db.query(PurchaseOrder).filter(PurchaseOrder.po_no == inv.po_no).first()
        if not po:
            po = PurchaseOrder(client_id=inv.client_id, po_no=inv.po_no)
            db.add(po)
            db.flush() 
        po_id = po.id

    inv_d = datetime.datetime.strptime(inv.inv_date, '%Y-%m-%d').date() if inv.inv_date else None
    due_d = datetime.datetime.strptime(inv.due_date, '%Y-%m-%d').date() if inv.due_date else None
    rounded_basic = round_inr_nearest(inv.basic)
    rounded_total = round_inr_nearest(inv.total)
    server_net_payable = max(
        0.0,
        float(rounded_total or 0.0)
        - float(inv.advance_adj or 0.0)
        - float(inv.tds_ded or 0.0)
        - float(inv.retention_held or 0.0)
    )
    server_balance = max(0.0, server_net_payable - float(inv.paid or 0.0))

    new_inv = Invoice(
        client_id=inv.client_id, po_id=po_id, invoice_no=inv.invoice_no,
        sub_entity=inv.sub_entity, lr_no=inv.lr_no, inv_date=inv_d, due_date=due_d,
        basic=rounded_basic, gst=inv.gst, total=rounded_total, advance_adj=inv.advance_adj,
        tds_ded=inv.tds_ded, retention_held=inv.retention_held, net_payable=server_net_payable,
        paid=inv.paid, balance=server_balance, is_note=inv.is_note, note_type=inv.note_type, note_reason=inv.note_reason
    )
    db.add(new_inv)
    
    # CRITICAL FIX: Flush to generate new_inv.id BEFORE adding dispatch items
    db.flush() 

    for item in inv.dispatch_items:
        new_dispatch = InvoiceDispatchItem(
            invoice_id=new_inv.id,
            description=item.description,
            dispatched_qty=item.qty,
            inspected_qty=item.inspected_qty,
            uom=item.uom
        )
        db.add(new_dispatch)
    ensure_baseline_from_dispatch(po_id, inv.dispatch_items, db)
        
    db.commit()
    auto_advance_applied = 0.0
    if po_id:
        po_obj = db.query(PurchaseOrder).filter(PurchaseOrder.id == po_id).first()
        if po_obj:
            historical_allocs = db.query(PaymentAllocation).join(PaymentHistory, PaymentAllocation.payment_id == PaymentHistory.id).filter(
                PaymentHistory.client_id == inv.client_id,
                PaymentAllocation.target_po_no == po_obj.po_no
            ).all()
            po_pool = 0.0
            for al in historical_allocs:
                if al.alloc_type == "po_advance":
                    po_pool += float(al.amount or 0.0)
                elif al.alloc_type == "po_advance_applied":
                    po_pool -= float(al.amount or 0.0)
            po_pool = max(0.0, po_pool)
            adv_pct = float(po_obj.adv_pct or 0.0)
            if adv_pct > 0:
                base_amt = float(new_inv.basic or 0.0) if (po_obj.ret_base or "total") == "basic" else float(new_inv.total or 0.0)
                allowed_remaining = max(0.0, (base_amt * (adv_pct / 100.0)) - float(new_inv.advance_adj or 0.0))
            else:
                # If no explicit advance cap is configured, still auto-apply available PO advance pool.
                allowed_remaining = float(new_inv.balance or 0.0)
            to_apply = min(allowed_remaining, po_pool, float(new_inv.balance or 0.0))
            if to_apply > 0:
                before_adv = float(new_inv.advance_adj or 0.0)
                pay_id = str(int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000))
                db.add(PaymentHistory(
                    id=pay_id,
                    client_id=inv.client_id,
                    date=datetime.date.today(),
                    type="ADVANCE_APPLIED",
                    amount=float(to_apply),
                    details=f"Auto-applied PO advance to {new_inv.invoice_no}",
                    note="Auto apply on invoice creation"
                ))
                db.flush()
                db.add(PaymentAllocation(
                    payment_id=pay_id,
                    alloc_type="po_advance_applied",
                    target_inv_id=new_inv.invoice_no,
                    target_po_no=po_obj.po_no,
                    amount=float(to_apply)
                ))
                # Reflect auto-applied PO advance directly on the invoice advance column.
                new_inv.advance_adj = float(new_inv.advance_adj or 0.0) + float(to_apply)
                new_inv.net_payable = max(
                    0.0,
                    float(new_inv.total or 0.0)
                    - float(new_inv.advance_adj or 0.0)
                    - float(new_inv.tds_ded or 0.0)
                    - float(new_inv.retention_held or 0.0)
                )
                new_inv.balance = max(0.0, float(new_inv.net_payable or 0.0) - float(new_inv.paid or 0.0))
                db.commit()
                auto_advance_applied = float(to_apply)
                
    recalculate_client_ledger(new_inv.client_id, db)
    return {"success": True, "id": new_inv.id, "auto_advance_applied": auto_advance_applied}

@app.put("/api/invoices/{invoice_no:path}")
def update_invoice(invoice_no: str, inv: InvoiceUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role not in ["admin", "logistics", "user"]:
        raise HTTPException(status_code=403, detail="Authorized operations access required")
    
    db_inv = db.query(Invoice).filter(Invoice.invoice_no == invoice_no).first()
    if not db_inv:
        raise HTTPException(status_code=404, detail="Invoice not found.")
        
    po_id = None
    if inv.po_no and inv.po_no.strip() and inv.po_no != 'UNASSIGNED':
        po = db.query(PurchaseOrder).filter(PurchaseOrder.po_no == inv.po_no).first()
        if not po:
            po = PurchaseOrder(client_id=inv.client_id, po_no=inv.po_no)
            db.add(po)
            db.flush() 
        po_id = po.id

    db_inv.po_id = po_id
    db_inv.sub_entity = inv.sub_entity
    db_inv.lr_no = inv.lr_no
    db_inv.inv_date = datetime.datetime.strptime(inv.inv_date, '%Y-%m-%d').date() if inv.inv_date else None
    db_inv.due_date = datetime.datetime.strptime(inv.due_date, '%Y-%m-%d').date() if inv.due_date else None
    db_inv.basic = round_inr_nearest(inv.basic)
    db_inv.gst = inv.gst
    db_inv.total = round_inr_nearest(inv.total)
    db_inv.advance_adj = inv.advance_adj
    db_inv.tds_ded = inv.tds_ded
    db_inv.retention_held = inv.retention_held
    db_inv.net_payable = max(
        0.0,
        float(db_inv.total or 0.0)
        - float(db_inv.advance_adj or 0.0)
        - float(db_inv.tds_ded or 0.0)
        - float(db_inv.retention_held or 0.0)
    )
    db_inv.paid = inv.paid
    db_inv.balance = max(0.0, float(db_inv.net_payable or 0.0) - float(db_inv.paid or 0.0))
    db_inv.is_note = inv.is_note
    db_inv.note_type = inv.note_type
    db_inv.note_reason = inv.note_reason
    
    # CRITICAL FIX: Clear old items and write new ones safely using db_inv.id
    db.query(InvoiceDispatchItem).filter(InvoiceDispatchItem.invoice_id == db_inv.id).delete()
    for item in inv.dispatch_items:
        new_dispatch = InvoiceDispatchItem(
            invoice_id=db_inv.id,
            description=item.description,
            dispatched_qty=item.qty,
            inspected_qty=item.inspected_qty,
            uom=item.uom
        )
        db.add(new_dispatch)
    ensure_baseline_from_dispatch(po_id, inv.dispatch_items, db)
        
    db.commit()
    recalculate_client_ledger(db_inv.client_id, db)
    return {"success": True}

@app.delete("/api/invoices/{invoice_no:path}")
def delete_invoice(invoice_no: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    db_inv = db.query(Invoice).filter(Invoice.invoice_no == invoice_no).first()
    if db_inv:
        client_id = db_inv.client_id
        allocs = db.query(PaymentAllocation).filter(
            PaymentAllocation.target_inv_id == db_inv.invoice_no,
            PaymentAllocation.alloc_type == "po_advance_applied"
        ).all()
        # Revert PO advance consumption when deleting an invoice that had auto-applied advance.
        linked_payment_ids = {a.payment_id for a in allocs if a.payment_id}
        for al in allocs:
            db.delete(al)
        # Cleanup dangling ADVANCE_APPLIED logs that no longer have allocations.
        for pid in linked_payment_ids:
            remaining = db.query(PaymentAllocation).filter(PaymentAllocation.payment_id == pid).count()
            if remaining == 0:
                ph = db.query(PaymentHistory).filter(PaymentHistory.id == pid, PaymentHistory.type == "ADVANCE_APPLIED").first()
                if ph:
                    db.delete(ph)
        db.flush()
        db.delete(db_inv)
        db.commit()
        recalculate_client_ledger(client_id, db)
    return {"success": True}


@app.delete("/api/dispatch-items/{dispatch_item_id}")
def delete_dispatch_item(dispatch_item_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role not in ["admin", "logistics"]:
        raise HTTPException(status_code=403, detail="Admin or Logistics access required")
    item = db.query(InvoiceDispatchItem).filter(InvoiceDispatchItem.id == dispatch_item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Dispatch entry not found.")
    invoice = db.query(Invoice).filter(Invoice.id == item.invoice_id).first()
    po_id = invoice.po_id if invoice else None
    db.delete(item)
    db.flush()
    pruned = 0
    if po_id:
        pruned = prune_orphan_baseline_items_for_po(po_id, db)
    db.commit()
    return {"success": True, "baseline_rows_pruned": pruned}


@app.put("/api/dispatch-items/{dispatch_item_id}")
def update_dispatch_item(dispatch_item_id: int, payload: DispatchItemUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role not in ["admin", "logistics", "user"]:
        raise HTTPException(status_code=403, detail="Authorized operations access required")

    item = db.query(InvoiceDispatchItem).filter(InvoiceDispatchItem.id == dispatch_item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Dispatch entry not found.")

    invoice = db.query(Invoice).filter(Invoice.id == item.invoice_id).first()
    po_id = invoice.po_id if invoice else None

    desc = (payload.description or "").strip()
    if not desc:
        raise HTTPException(status_code=400, detail="Description is required.")

    item.description = desc
    item.dispatched_qty = max(0.0, float(payload.qty or 0.0))
    item.inspected_qty = max(0.0, float(payload.inspected_qty or 0.0))
    item.uom = (payload.uom or "Nos").strip() or "Nos"

    merged = False
    merged_into_id = None
    if payload.merge_on_match:
        norm_desc = normalize_dispatch_description(item.description)
        norm_uom = (item.uom or "").strip().upper()
        siblings = db.query(InvoiceDispatchItem).filter(
            InvoiceDispatchItem.invoice_id == item.invoice_id,
            InvoiceDispatchItem.id != item.id
        ).all()
        for sibling in siblings:
            if normalize_dispatch_description(sibling.description or "") == norm_desc and (sibling.uom or "").strip().upper() == norm_uom:
                sibling.dispatched_qty = float(sibling.dispatched_qty or 0.0) + float(item.dispatched_qty or 0.0)
                sibling.inspected_qty = float(sibling.inspected_qty or 0.0) + float(item.inspected_qty or 0.0)
                merged = True
                merged_into_id = sibling.id
                db.delete(item)
                break

    if po_id:
        ensure_baseline_from_dispatch(
            po_id,
            [DispatchItemCreate(description=desc, qty=max(0.0, float(payload.qty or 0.0)), inspected_qty=max(0.0, float(payload.inspected_qty or 0.0)), uom=(payload.uom or "Nos"))],
            db
        )
        prune_orphan_baseline_items_for_po(po_id, db)

    db.commit()
    return {"success": True, "merged": merged, "merged_into_id": merged_into_id}


@app.post("/api/dispatch-items/upsert-cell")
def upsert_dispatch_cell(payload: DispatchCellUpsert, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role not in ["admin", "logistics", "user"]:
        raise HTTPException(status_code=403, detail="Authorized operations access required")

    invoice_no = (payload.invoice_no or "").strip()
    desc = (payload.description or "").strip()
    if not invoice_no:
        raise HTTPException(status_code=400, detail="invoice_no is required.")
    if not desc:
        raise HTTPException(status_code=400, detail="description is required.")

    inv = db.query(Invoice).filter(Invoice.invoice_no == invoice_no).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found.")

    qty = max(0.0, float(payload.qty or 0.0))
    uom = (payload.uom or "Nos").strip() or "Nos"
    norm_desc = normalize_dispatch_description(desc)
    norm_uom = uom.upper()

    matches = db.query(InvoiceDispatchItem).filter(InvoiceDispatchItem.invoice_id == inv.id).all()
    matched = [m for m in matches if normalize_dispatch_description(m.description or "") == norm_desc and (m.uom or "").strip().upper() == norm_uom]

    if matched:
        keeper = matched[0]
        keeper.description = desc
        keeper.uom = uom
        keeper.dispatched_qty = qty
        # Keep inspected qty bounded to dispatched qty.
        keeper.inspected_qty = min(float(keeper.inspected_qty or 0.0), qty)
        for extra in matched[1:]:
            db.delete(extra)
        dispatch_item_id = keeper.id
        created = False
    else:
        new_item = InvoiceDispatchItem(
            invoice_id=inv.id,
            description=desc,
            dispatched_qty=qty,
            inspected_qty=0.0,
            uom=uom
        )
        db.add(new_item)
        db.flush()
        dispatch_item_id = new_item.id
        created = True

    if inv.po_id:
        ensure_baseline_from_dispatch(
            inv.po_id,
            [DispatchItemCreate(description=desc, qty=qty, inspected_qty=0.0, uom=uom)],
            db
        )
        prune_orphan_baseline_items_for_po(inv.po_id, db)

    db.commit()
    return {"success": True, "created": created, "dispatch_item_id": dispatch_item_id}


@app.patch("/api/invoices/{invoice_no:path}/inline")
def update_invoice_inline_fields(invoice_no: str, payload: InvoiceInlineUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role not in ["admin", "logistics", "user"]:
        raise HTTPException(status_code=403, detail="Authorized operations access required")
    inv = db.query(Invoice).filter(Invoice.invoice_no == invoice_no).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found.")

    if payload.inv_date is not None:
        inv.inv_date = datetime.datetime.strptime(payload.inv_date, "%Y-%m-%d").date() if str(payload.inv_date).strip() else None
    if payload.lr_no is not None:
        inv.lr_no = (payload.lr_no or "").strip()
    if payload.total is not None:
        inv.total = max(0.0, round_inr_nearest(payload.total))
        # Keep payable consistency when total is edited inline.
        inv.net_payable = max(0.0, float(inv.total or 0.0) - float(inv.advance_adj or 0.0) - float(inv.tds_ded or 0.0) - float(inv.retention_held or 0.0))
        inv.balance = max(0.0, float(inv.net_payable or 0.0) - float(inv.paid or 0.0))

    db.commit()
    recalculate_client_ledger(inv.client_id, db)
    return {"success": True}


@app.delete("/api/dispatch-row/{invoice_no:path}")
def delete_dispatch_row(invoice_no: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role not in ["admin", "logistics", "user"]:
        raise HTTPException(status_code=403, detail="Authorized operations access required")
    inv = db.query(Invoice).filter(Invoice.invoice_no == invoice_no).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found.")
    po_id = inv.po_id
    deleted = db.query(InvoiceDispatchItem).filter(InvoiceDispatchItem.invoice_id == inv.id).delete()
    if po_id:
        prune_orphan_baseline_items_for_po(po_id, db)
    db.commit()
    return {"success": True, "deleted_dispatch_lines": int(deleted or 0)}


@app.post("/api/dispatch-columns/delete")
def delete_dispatch_column(payload: DispatchColumnDeleteRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role not in ["admin", "logistics", "user"]:
        raise HTTPException(status_code=403, detail="Authorized operations access required")
    po_no = (payload.po_no or "").strip()
    desc = (payload.description or "").strip()
    if not po_no or not desc:
        raise HTTPException(status_code=400, detail="po_no and description are required.")
    po = db.query(PurchaseOrder).filter(PurchaseOrder.po_no == po_no).first()
    if not po:
        raise HTTPException(status_code=404, detail="PO not found.")

    norm_desc = normalize_dispatch_description(desc)
    norm_uom = (payload.uom or "").strip().upper() if payload.uom else ""

    baseline_deleted = 0
    for base in list(po.baseline_items or []):
        if normalize_dispatch_description(base.description or "") != norm_desc:
            continue
        if norm_uom and (base.uom or "").strip().upper() != norm_uom:
            continue
        db.delete(base)
        baseline_deleted += 1

    invoices = db.query(Invoice).options(selectinload(Invoice.dispatch_items)).filter(
        Invoice.po_id == po.id,
        Invoice.is_note == False
    ).all()
    dispatch_deleted = 0
    for inv in invoices:
        for item in list(inv.dispatch_items or []):
            if normalize_dispatch_description(item.description or "") != norm_desc:
                continue
            if norm_uom and (item.uom or "").strip().upper() != norm_uom:
                continue
            db.delete(item)
            dispatch_deleted += 1

    prune_orphan_baseline_items_for_po(po.id, db)
    db.commit()
    return {"success": True, "baseline_deleted": baseline_deleted, "dispatch_deleted": dispatch_deleted}


@app.post("/api/dispatch-columns/rename")
def rename_dispatch_column(payload: DispatchColumnRenameRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Rename a dispatch material column on a PO and migrate every dispatch_item that
    used the old description to the new one. If the new description matches an
    existing column for the same UOM, quantities are merged so multiple items
    can share the same material description."""
    if current_user.role not in ["admin", "logistics", "user"]:
        raise HTTPException(status_code=403, detail="Authorized operations access required")
    po_no = (payload.po_no or "").strip()
    old_desc_raw = (payload.old_description or "").strip()
    new_desc_raw = (payload.new_description or "").strip()
    if not po_no or not old_desc_raw or not new_desc_raw:
        raise HTTPException(status_code=400, detail="po_no, old_description and new_description are required.")

    po = db.query(PurchaseOrder).filter(PurchaseOrder.po_no == po_no).first()
    if not po:
        raise HTTPException(status_code=404, detail="PO not found.")

    old_norm = normalize_dispatch_description(old_desc_raw)
    new_norm = normalize_dispatch_description(new_desc_raw)
    old_uom = (payload.old_uom or "Nos").strip() or "Nos"
    new_uom = (payload.new_uom or old_uom or "Nos").strip() or "Nos"
    old_uom_key = old_uom.upper()
    new_uom_key = new_uom.upper()

    baseline_renamed = 0
    baseline_merged = 0
    keeper_baseline = None
    target_baseline = None
    other_baselines: list = []
    for base in list(po.baseline_items or []):
        norm = normalize_dispatch_description(base.description or "")
        u = (base.uom or "Nos").strip().upper()
        if norm == new_norm and u == new_uom_key:
            keeper_baseline = base
        elif norm == old_norm and u == old_uom_key:
            other_baselines.append(base)

    if keeper_baseline is None and other_baselines:
        target_baseline = other_baselines.pop(0)
        target_baseline.description = new_desc_raw
        target_baseline.uom = new_uom
        if hasattr(target_baseline, "material_type") and not target_baseline.material_type:
            target_baseline.material_type = target_baseline.material_type
        keeper_baseline = target_baseline
        baseline_renamed += 1

    if keeper_baseline is not None:
        for base in other_baselines:
            keeper_baseline.ordered_qty = float(keeper_baseline.ordered_qty or 0.0) + float(base.ordered_qty or 0.0)
            keeper_baseline.inspected_qty = float(keeper_baseline.inspected_qty or 0.0) + float(base.inspected_qty or 0.0)
            if not keeper_baseline.material_type and getattr(base, "material_type", None):
                keeper_baseline.material_type = base.material_type
            db.delete(base)
            baseline_merged += 1

    invoices = db.query(Invoice).options(selectinload(Invoice.dispatch_items)).filter(
        Invoice.po_id == po.id,
        Invoice.is_note == False
    ).all()
    dispatch_renamed = 0
    dispatch_merged = 0
    for inv in invoices:
        keeper_item = None
        renames: list = []
        for item in list(inv.dispatch_items or []):
            norm = normalize_dispatch_description(item.description or "")
            u = (item.uom or "Nos").strip().upper()
            if norm == new_norm and u == new_uom_key:
                keeper_item = item
            elif norm == old_norm and u == old_uom_key:
                renames.append(item)
        if not renames:
            continue
        if keeper_item is None:
            keeper_item = renames.pop(0)
            keeper_item.description = new_desc_raw
            keeper_item.uom = new_uom
            dispatch_renamed += 1
        for extra in renames:
            keeper_item.dispatched_qty = float(keeper_item.dispatched_qty or 0.0) + float(extra.dispatched_qty or 0.0)
            keeper_item.inspected_qty = float(keeper_item.inspected_qty or 0.0) + float(extra.inspected_qty or 0.0)
            db.delete(extra)
            dispatch_merged += 1

    prune_orphan_baseline_items_for_po(po.id, db)
    db.commit()
    return {
        "success": True,
        "baseline_renamed": baseline_renamed,
        "baseline_merged": baseline_merged,
        "dispatch_renamed": dispatch_renamed,
        "dispatch_merged": dispatch_merged,
    }


@app.post("/api/payments/allocate")
def allocate_payment(payment: PaymentAllocateRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    if payment.advance_only and not payment.apply_adv:
        raise HTTPException(status_code=400, detail="advance_only requires apply_adv=true.")
    if payment.fund_source not in ("receipt", "unallocated"):
        raise HTTPException(status_code=400, detail="fund_source must be 'receipt' or 'unallocated'.")
    if payment.fund_source == "receipt" and not payment.advance_only and payment.amount <= 0:
        raise HTTPException(status_code=400, detail="Payment amount must be greater than zero.")
    if payment.clear_po_pool and not payment.po_no:
        raise HTTPException(status_code=400, detail="po_no is required when clear_po_pool=true.")
    if db.query(PaymentHistory).filter(PaymentHistory.id == payment.id).first():
        raise HTTPException(status_code=400, detail="Payment id already exists.")

    date_obj = datetime.datetime.strptime(payment.date, '%Y-%m-%d').date()
    invoices = db.query(Invoice).filter(
        Invoice.client_id == payment.client_id,
        Invoice.is_note == False
    ).all()
    inv_map = {inv.invoice_no: inv for inv in invoices}
    po_by_id = {po.id: po for po in db.query(PurchaseOrder).filter(PurchaseOrder.client_id == payment.client_id).all()}

    selected: list[tuple[Invoice, float]] = []
    if payment.mode == "targeted" and payment.targets:
        for t in payment.targets:
            inv = inv_map.get(t.inv_id)
            if inv and (inv.balance or 0) > 0:
                if payment.advance_only:
                    selected.append((inv, -1.0))
                else:
                    selected.append((inv, max(0.0, float(t.amount or 0.0))))
    else:
        selected = [(inv, -1.0) for inv in sorted(
            [i for i in invoices if (i.balance or 0) > 0],
            key=lambda x: x.inv_date or datetime.date(9999, 12, 31)
        )]

    client = db.query(Client).filter(Client.id == payment.client_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    available_unallocated = float(client.excess_funds or 0.0)
    if payment.fund_source == "unallocated":
        if available_unallocated <= 0:
            raise HTTPException(status_code=400, detail="No unallocated funds available.")
        if payment.amount > 0:
            remaining = min(float(payment.amount), available_unallocated)
        else:
            remaining = available_unallocated
    else:
        remaining = float(payment.amount)

    advance_applied_total = 0.0
    po_advance_allocs: list[tuple[str, float, str]] = []
    allocs_for_db = []
    log_details = []

    po_pool: dict[str, float] = defaultdict(float)
    historical_allocs = db.query(PaymentAllocation).join(PaymentHistory, PaymentAllocation.payment_id == PaymentHistory.id).filter(
        PaymentHistory.client_id == payment.client_id
    ).all()
    for al in historical_allocs:
        if not al.target_po_no:
            continue
        if al.alloc_type == 'po_advance':
            po_pool[al.target_po_no] += float(al.amount or 0.0)
        elif al.alloc_type == 'po_advance_applied':
            po_pool[al.target_po_no] -= float(al.amount or 0.0)
    for po_no in list(po_pool.keys()):
        if po_pool[po_no] < 0:
            po_pool[po_no] = 0.0

    if payment.clear_po_pool:
        po_key = payment.po_no.strip()
        amount_to_clear = float(po_pool.get(po_key, 0.0))
        if amount_to_clear <= 0:
            raise HTTPException(status_code=400, detail="No remaining PO advance found to clear.")
        po_advance_allocs.append(("WRITTEN_OFF", amount_to_clear, po_key))
    elif payment.fund_source == "receipt" and payment.move_to_po and payment.move_to_po.strip():
        move_po = payment.move_to_po.strip()
        amount_to_move = remaining
        if amount_to_move <= 0:
            raise HTTPException(status_code=400, detail="Amount to move must be greater than zero.")
        allocs_for_db.append(("__PO__", amount_to_move))
        remaining = 0.0
    elif payment.fund_source == "unallocated" and payment.move_to_po and payment.move_to_po.strip():
        move_po = payment.move_to_po.strip()
        amount_to_move = remaining
        if amount_to_move <= 0:
            raise HTTPException(status_code=400, detail="Amount to move must be greater than zero.")
        allocs_for_db.append(("__PO__", amount_to_move))
        remaining = 0.0
    else:
        if payment.po_no and payment.po_no.strip():
            scoped_po = payment.po_no.strip()
            selected = [
                (inv, req) for inv, req in selected
                if (po_by_id.get(inv.po_id).po_no if inv.po_id and po_by_id.get(inv.po_id) else None) == scoped_po
            ]
        for inv, requested in selected:
            if payment.apply_adv and not payment.only_gst:
                po_obj = po_by_id.get(inv.po_id) if inv.po_id else None
                po_no = po_obj.po_no if po_obj else None
                adv_pct = float(po_obj.adv_pct or 0.0) if po_obj else 0.0
                base_key = (po_obj.ret_base or "total") if po_obj else "total"
                available_pool = float(po_pool.get(po_no, 0.0)) if po_no else 0.0
                if po_no and adv_pct > 0 and available_pool > 0:
                    base_amt = float(inv.basic or 0.0) if base_key == "basic" else float(inv.total or 0.0)
                    max_allowed = base_amt * (adv_pct / 100.0)
                    current_advance = float(inv.advance_adj or 0.0)
                    shortfall = max(0.0, max_allowed - current_advance)
                    inv_balance_for_adv = float(inv.balance or 0.0)
                    to_apply_adv = min(shortfall, available_pool, inv_balance_for_adv)
                    if to_apply_adv > 0:
                        inv.advance_adj = current_advance + to_apply_adv
                        po_pool[po_no] = available_pool - to_apply_adv
                        po_advance_allocs.append((inv.invoice_no, to_apply_adv, po_no))
                        advance_applied_total += to_apply_adv

            if payment.advance_only:
                continue
            if remaining <= 0:
                break
            inv_balance = float(inv.balance or 0.0)
            if inv_balance <= 0:
                continue

            if payment.only_gst:
                allocatable = min(float(inv.gst or 0.0), inv_balance)
            else:
                target_bal = 0.0
                if payment.hold_ret:
                    target_bal += float(inv.retention_held or 0.0)
                if payment.hold_gst:
                    target_bal += float(inv.gst or 0.0)
                allocatable = max(0.0, inv_balance - target_bal)

            if allocatable <= 0:
                continue

            desired = remaining if requested < 0 else requested
            amount_to_apply = min(float(desired), allocatable, remaining)
            if amount_to_apply <= 0:
                continue

            allocs_for_db.append((inv.invoice_no, amount_to_apply))
            log_details.append(f"{inv.invoice_no} ({amount_to_apply:.2f})")
            remaining -= amount_to_apply

    if remaining > 0 and payment.fund_source == "receipt" and payment.excess_action == "allocate_pending":
        selected_ids = {inv.invoice_no for inv, _ in selected}
        pending_invoices = sorted(
            [i for i in invoices if (i.balance or 0) > 0 and i.invoice_no not in selected_ids],
            key=lambda x: x.inv_date or datetime.date(9999, 12, 31)
        )
        for inv in pending_invoices:
            if remaining <= 0:
                break
            inv_balance = float(inv.balance or 0.0)
            if inv_balance <= 0:
                continue
            amount_to_apply = min(inv_balance, remaining)
            allocs_for_db.append((inv.invoice_no, amount_to_apply))
            log_details.append(f"{inv.invoice_no} ({amount_to_apply:.2f})")
            remaining -= amount_to_apply

    if payment.clear_po_pool:
        payment_type = 'ADVANCE_APPLIED'
        payment_amount = po_advance_allocs[0][1] if po_advance_allocs else 0.0
    elif payment.advance_only:
        payment_type = 'ADVANCE_APPLIED'
        payment_amount = advance_applied_total
    elif payment.fund_source == "unallocated":
        payment_type = 'UNALLOCATED_APPLIED'
        payment_amount = (available_unallocated - remaining) if payment.amount <= 0 else min(float(payment.amount), available_unallocated) - remaining
    else:
        payment_type = 'RECEIPT'
        payment_amount = float(payment.amount)
    details = ", ".join(log_details) if log_details else ("PO advance mapping" if payment.advance_only else "Unallocated receipt")

    new_pay = PaymentHistory(
        id=payment.id,
        client_id=payment.client_id,
        date=date_obj,
        type=payment_type,
        amount=payment_amount,
        details=details,
        note=payment.note
    )
    db.add(new_pay)
    db.flush()

    if payment.fund_source == "receipt" and remaining > 0:
        db.add(UnallocatedPaymentRegister(
            client_id=payment.client_id,
            source_payment_id=new_pay.id,
            created_on=date_obj,
            amount=float(remaining),
            balance=float(remaining),
            status="open",
            note=payment.note
        ))

    for inv_no, amt in allocs_for_db:
        if inv_no == "__PO__":
            moved_amt = float(amt or 0.0)
            if moved_amt > 0:
                db.add(UnallocatedAdvanceRegister(
                    client_id=payment.client_id,
                    source_payment_id=new_pay.id,
                    po_no=payment.move_to_po.strip() if payment.move_to_po else None,
                    created_on=date_obj,
                    amount=moved_amt,
                    balance=moved_amt,
                    status="open",
                    note=payment.note
                ))
            db.add(PaymentAllocation(
                payment_id=new_pay.id,
                alloc_type='po_advance',
                target_inv_id=None,
                target_po_no=payment.move_to_po.strip() if payment.move_to_po else None,
                note_id=None,
                amount=amt
            ))
        else:
            db.add(PaymentAllocation(
                payment_id=new_pay.id,
                alloc_type='invoice',
                target_inv_id=inv_no,
                target_po_no=None,
                note_id=None,
                amount=amt
            ))

    for inv_no, amt, po_no in po_advance_allocs:
        db.add(PaymentAllocation(
            payment_id=new_pay.id,
            alloc_type='po_advance_applied',
            target_inv_id=inv_no,
            target_po_no=po_no,
            note_id=None,
            amount=amt
        ))

    db.commit()
    recalculate_client_ledger(payment.client_id, db)
    bank_allocated = 0.0 if (payment.advance_only or payment.fund_source == "unallocated") else (float(payment.amount) - remaining)
    return {
        "success": True,
        "allocated": bank_allocated,
        "po_advance_applied": advance_applied_total,
        "remaining": remaining,
        "allocation_count": len(allocs_for_db),
        "advance_allocation_count": len(po_advance_allocs),
        "unallocated_consumed": payment_amount if payment.fund_source == "unallocated" else 0.0
    }

@app.get("/api/payments")
def get_payments(fiscal_years: Optional[str] = Query(default=None, description="Comma separated FY labels"), current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role == "logistics":
        raise HTTPException(status_code=403, detail="Logistics role cannot access payment data.")
    payments = db.query(PaymentHistory).options(selectinload(PaymentHistory.allocations)).all()
    settings = db.query(SystemSettings).first()
    fy_start_month = int((settings.fy_start_month if settings else 4) or 4)
    fy_start_day = int((settings.fy_start_day if settings else 1) or 1)
    fy_filters = parse_fy_filters(fiscal_years)
    result = []
    
    for p in payments:
        pay_fy = fiscal_year_label(p.date, fy_start_month, fy_start_day)
        if fy_filters and pay_fy not in fy_filters:
            continue
        alloc_list = []
        for a in p.allocations:
            alloc_list.append({
                "type": a.alloc_type,
                "invId": a.target_inv_id,
                "po": a.target_po_no,
                "noteId": a.note_id,
                "amount": a.amount
            })
            
        result.append({
            "id": p.id,
            "client_id": p.client_id,
            "date": p.date.isoformat() if p.date else None,
            "type": p.type,
            "amount": p.amount,
            "details": p.details,
            "note": p.note,
            "fiscalYear": pay_fy,
            "allocations": alloc_list
        })
        
    return result


@app.get("/api/registers/unallocated-payments")
def get_unallocated_payment_register(client_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role == "logistics":
        raise HTTPException(status_code=403, detail="Logistics role cannot access payment data.")
    rows = db.query(UnallocatedPaymentRegister).filter(
        UnallocatedPaymentRegister.client_id == client_id
    ).order_by(UnallocatedPaymentRegister.id.desc()).all()
    return [{
        "id": r.id,
        "created_on": r.created_on.isoformat() if r.created_on else None,
        "amount": float(r.amount or 0.0),
        "balance": float(r.balance or 0.0),
        "status": r.status,
        "source_payment_id": r.source_payment_id,
        "note": r.note
    } for r in rows]


@app.post("/api/registers/unallocated-payments/{entry_id}/reverse")
def reverse_unallocated_payment_entry(entry_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    row = db.query(UnallocatedPaymentRegister).filter(UnallocatedPaymentRegister.id == entry_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Register entry not found.")
    row.status = "reversed"
    row.balance = 0.0
    db.commit()
    recalculate_client_ledger(row.client_id, db)
    return {"success": True}


@app.get("/api/registers/unallocated-advances")
def get_unallocated_advance_register(client_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role == "logistics":
        raise HTTPException(status_code=403, detail="Logistics role cannot access financial advance data.")
    rows = db.query(UnallocatedAdvanceRegister).filter(
        UnallocatedAdvanceRegister.client_id == client_id
    ).order_by(UnallocatedAdvanceRegister.id.desc()).all()
    return [{
        "id": r.id,
        "created_on": r.created_on.isoformat() if r.created_on else None,
        "amount": float(r.amount or 0.0),
        "balance": float(r.balance or 0.0),
        "status": r.status,
        "po_no": r.po_no,
        "source_payment_id": r.source_payment_id,
        "note": r.note
    } for r in rows]


@app.post("/api/registers/unallocated-advances/{entry_id}/reverse")
def reverse_unallocated_advance_entry(entry_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    row = db.query(UnallocatedAdvanceRegister).filter(UnallocatedAdvanceRegister.id == entry_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Register entry not found.")
    row.status = "reversed"
    row.balance = 0.0
    db.commit()
    recalculate_client_ledger(row.client_id, db)
    return {"success": True}

@app.delete("/api/payments/{payment_id}")
def delete_payment(payment_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    
    db_pay = db.query(PaymentHistory).filter(PaymentHistory.id == payment_id).first()
    if db_pay:
        client_id = db_pay.client_id
        db.delete(db_pay)
        db.commit()
        recalculate_client_ledger(client_id, db)
    return {"success": True}

@app.post("/api/payments/{payment_id}/redistribute")
def redistribute_payment(payment_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    db_pay = db.query(PaymentHistory).options(selectinload(PaymentHistory.allocations)).filter(PaymentHistory.id == payment_id).first()
    if not db_pay:
        raise HTTPException(status_code=404, detail="Payment not found")

    response_payload = {
        "id": db_pay.id,
        "type": db_pay.type,
        "amount": float(db_pay.amount or 0.0),
        "note": db_pay.note or ""
    }
    client_id = db_pay.client_id
    db.delete(db_pay)
    db.commit()
    recalculate_client_ledger(client_id, db)
    return {"success": True, "payment": response_payload}

@app.put("/api/payments/{payment_id}")
def update_payment(payment_id: str, pay_update: PaymentUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    db_pay = db.query(PaymentHistory).filter(PaymentHistory.id == payment_id).first()
    if not db_pay:
        raise HTTPException(status_code=404, detail="Payment not found")
    db_pay.amount = pay_update.amount
    db_pay.note = pay_update.note
    db.commit()
    recalculate_client_ledger(db_pay.client_id, db)
    return {"success": True}

@app.post("/api/invoices/{invoice_no:path}/transfer")
def transfer_invoice(invoice_no: str, req: TransferRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    db_inv = db.query(Invoice).filter(Invoice.invoice_no == invoice_no).first()
    if not db_inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    
    old_client_id = db_inv.client_id

    if req.action == "copy":
        new_inv = Invoice(
            invoice_no=f"{db_inv.invoice_no}-COPY", client_id=req.new_client_id, po_id=None,
            sub_entity=db_inv.sub_entity, lr_no=db_inv.lr_no, inv_date=db_inv.inv_date, due_date=db_inv.due_date,
            basic=db_inv.basic, gst=db_inv.gst, total=db_inv.total, advance_adj=0, tds_ded=db_inv.tds_ded,
            retention_held=db_inv.retention_held, net_payable=db_inv.net_payable, paid=0, balance=db_inv.net_payable,
            is_note=db_inv.is_note, note_type=db_inv.note_type, note_reason=db_inv.note_reason
        )
        db.add(new_inv)
        db.flush()
        for item in db_inv.dispatch_items:
            new_item = InvoiceDispatchItem(
                invoice_id=new_inv.id, description=item.description, dispatched_qty=item.dispatched_qty,
                inspected_qty=item.inspected_qty, uom=item.uom
            )
            db.add(new_item)
        db.commit()
        recalculate_client_ledger(req.new_client_id, db)
    elif req.action == "move":
        db_inv.client_id = req.new_client_id
        db_inv.po_id = None
        db_inv.advance_adj = 0
        db_inv.paid = 0
        db.commit()
        recalculate_client_ledger(old_client_id, db)
        recalculate_client_ledger(req.new_client_id, db)
        
    return {"success": True}

@app.post("/api/notes/issue")
def issue_note(req: NoteIssueRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than zero.")
    if req.note_type not in ("CN", "DN"):
        raise HTTPException(status_code=400, detail="Invalid note type.")
    if db.query(Invoice).filter(Invoice.invoice_no == req.note_no).first():
        raise HTTPException(status_code=400, detail="Document Number already exists.")

    note_date = datetime.datetime.strptime(req.date, '%Y-%m-%d').date() if req.date else datetime.date.today()
    total_amt = -req.amount if req.note_type == "CN" else req.amount
    target_invoice = None
    note_po_id = None
    note_target_po_no = None
    if req.target_invoice_id:
        target_invoice = db.query(Invoice).filter(
            Invoice.invoice_no == req.target_invoice_id,
            Invoice.client_id == req.client_id
        ).first()
        if not target_invoice:
            raise HTTPException(status_code=404, detail="Target invoice not found for the selected client.")
        note_po_id = target_invoice.po_id
        if target_invoice.purchase_order:
            note_target_po_no = target_invoice.purchase_order.po_no
        elif note_po_id:
            po_obj = db.query(PurchaseOrder).filter(PurchaseOrder.id == note_po_id).first()
            if po_obj:
                note_target_po_no = po_obj.po_no

    new_note = Invoice(
        client_id=req.client_id,
        po_id=note_po_id,
        invoice_no=req.note_no,
        sub_entity='-',
        lr_no='-',
        inv_date=note_date,
        due_date=note_date,
        basic=0.0,
        gst=0.0,
        total=total_amt,
        advance_adj=0.0,
        tds_ded=0.0,
        retention_held=0.0,
        net_payable=0.0,
        paid=0.0,
        balance=0.0,
        is_note=True,
        note_type=req.note_type,
        note_reason=req.reason
    )
    db.add(new_note)

    if req.target_invoice_id:
        pay_id = str(int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000))
        db_pay = PaymentHistory(
            id=pay_id,
            client_id=req.client_id,
            date=note_date,
            type='NOTE_APPLIED',
            amount=float(req.amount),
            details=f"{'Credit' if req.note_type == 'CN' else 'Debit'} Note {req.note_no} applied to {req.target_invoice_id}",
            note=req.reason
        )
        db.add(db_pay)
        db.flush()
        db.add(PaymentAllocation(
            payment_id=db_pay.id,
            alloc_type='note_allocation',
            target_inv_id=req.target_invoice_id,
            target_po_no=note_target_po_no,
            note_id=req.note_no,
            amount=float(req.amount)
        ))

    db.commit()
    recalculate_client_ledger(req.client_id, db)
    return {"success": True}




# ... (All your schemas and API endpoints must be ABOVE this point) ...

# --- AI PDF Extraction Route ---
@app.post("/api/upload-invoice")
async def upload_invoice(invoice_pdf: list[UploadFile] = File(...), current_user: User = Depends(get_current_user)):
    if current_user.role not in ["admin", "logistics", "user"]:
        raise HTTPException(status_code=403, detail="Authorized operations access required")
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=503, detail="AI extraction is not configured.")
    enforce_upload_rate_limit(f"{current_user.id}:upload_invoice")
    require_pdf_files(invoice_pdf)
    results = []
    model = genai.GenerativeModel("gemini-2.5-flash")
    for file in invoice_pdf:
        content = await file.read()
        if len(content) > MAX_UPLOAD_FILE_SIZE_BYTES:
            raise HTTPException(status_code=400, detail=f"{file.filename} exceeds the maximum allowed size.")
        file_part = {"mime_type": "application/pdf", "data": content}
        prompt = """
        Extract details from this invoice. 
        CRITICAL INSTRUCTION FOR ITEMS: For the 'desc' field, you MUST capture the ENTIRE paragraph and full multi-line description corresponding to each serial number exactly as written. Do not summarize, truncate, or shorten the description.
        CRITICAL INSTRUCTION FOR CLIENT: For the 'clientName' field, capture the BUYER / CONSIGNEE / "Bill To" company name from the invoice (the customer who is receiving the goods, NOT the seller). Return the full registered company name exactly as printed (e.g., "Vedanta Limited - BALCO"). If not present, return an empty string.
        Return strictly JSON matching this structure:
        {
          "clientName": "Buyer / Bill-To company name exactly as printed",
          "invNo": "Invoice Number",
          "poNo": "PO Number",
          "lrNo": "LR Number",
          "date": "YYYY-MM-DD",
          "basic": 1234.50,
          "items": [
             {"desc": "ENTIRE paragraph of the goods description exactly as written on the document", "qty": 10.5, "uom": "MT/Nos"}
          ]
        }
        Do not include any markdown formatting or backticks in your response, just the raw JSON.
        """
        try:
            response = await asyncio.to_thread(model.generate_content, [prompt, file_part])
            results.append({"filename": file.filename, "success": True, "raw_data": response.text})
        except Exception as e:
            results.append({"filename": file.filename, "success": False, "error": str(e)})
    return {"success": True, "results": results}
@app.post("/api/upload-po")
async def upload_po(po_pdf: list[UploadFile] = File(...), current_user: User = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=503, detail="AI extraction is not configured.")
    enforce_upload_rate_limit(f"{current_user.id}:upload_po")
    require_pdf_files(po_pdf)
    results = []
    model = genai.GenerativeModel("gemini-2.5-flash")
    for file in po_pdf:
        content = await file.read()
        if len(content) > MAX_UPLOAD_FILE_SIZE_BYTES:
            raise HTTPException(status_code=400, detail=f"{file.filename} exceeds the maximum allowed size.")
        file_part = {"mime_type": "application/pdf", "data": content}
        prompt = """
        Extract details from this Purchase Order. 
        CRITICAL INSTRUCTION FOR ITEMS: For the 'desc' field, you MUST capture the ENTIRE paragraph and full multi-line description corresponding to each serial number exactly as written. Do not summarize, truncate, or shorten the description.
        Return strictly JSON matching this structure:
        {
          "poNo": "PO Number",
          "items": [
             {"desc": "ENTIRE paragraph of the ordered goods description exactly as written on the document", "qty": 100, "uom": "MT/Nos"}
          ]
        }
        Do not include any markdown formatting or backticks in your response, just the raw JSON.
        """
        try:
            response = await asyncio.to_thread(model.generate_content, [prompt, file_part])
            results.append({"filename": file.filename, "success": True, "raw_data": response.text})
        except Exception as e:
            results.append({"filename": file.filename, "success": False, "error": str(e)})
    return {"success": True, "results": results}

# --- Static File Routing (CRITICAL: MUST BE THE ABSOLUTE LAST LINES OF THE FILE) ---
app.mount("/static", StaticFiles(directory="public"), name="static")

@app.get("/")
def serve_frontend():
    return FileResponse("public/index.html")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="192.168.0.143", port=3000)