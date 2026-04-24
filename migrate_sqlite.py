import sqlite3
import json
import datetime
import hashlib
import base64
import secrets
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import (
    Base, User, Client, PurchaseOrder, Invoice, 
    PaymentHistory, PaymentAllocation, SystemSettings
)

# 1. Connect to NEW strict database
SQLALCHEMY_DATABASE_URL = "sqlite:///./erp_database.sqlite"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base.metadata.create_all(bind=engine)

PBKDF2_ROUNDS = 210000

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"

def parse_date(date_str):
    if not date_str or date_str.strip() == "":
        return None
    try:
        return datetime.datetime.strptime(date_str.split('T')[0], '%Y-%m-%d').date()
    except:
        return None

def run_migration():
    new_db = SessionLocal()
    print("🚀 Initiating Direct SQLite Migration...")

    try:
        # 2. Connect to OLD database
        old_conn = sqlite3.connect('old_erp.sqlite')
        old_cursor = old_conn.cursor()

        # --- MIGRATE USERS ---
        print("👤 Transferring User Accounts...")
        old_cursor.execute("SELECT username, password, role FROM users")
        old_users = old_cursor.fetchall()
        for u in old_users:
            existing = new_db.query(User).filter(User.username == u[0]).first()
            if not existing:
                new_user = User(username=u[0], hashed_password=hash_password(u[1] or ""), role=u[2])
                new_db.add(new_user)
        new_db.commit()

        # --- EXTRACT JSON BLOB ---
        print("📦 Extracting legacy JSON blob...")
        # Order by ID DESC to get the most recently saved state
        old_cursor.execute("SELECT json_data FROM erp_data ORDER BY id DESC LIMIT 1")
        row = old_cursor.fetchone()
        
        if not row or not row[0]:
            print("❌ No JSON data found in the old database.")
            return

        app_data = json.loads(row[0])

        # --- MIGRATE SETTINGS ---
        print("⚙️ Migrating Global Settings...")
        settings_data = app_data.get('_settings', {})
        sys_settings = new_db.query(SystemSettings).first()
        if not sys_settings:
            sys_settings = SystemSettings()
            new_db.add(sys_settings)
        sys_settings.exchange_rate = settings_data.get('exchangeRate', 83.0)
        sys_settings.custom_columns = json.dumps(settings_data.get('customColumns', []))
        new_db.commit()

        # --- MIGRATE CLIENTS, POs, INVOICES, PAYMENTS ---
        for client_name, data in app_data.items():
            if client_name == '_settings':
                continue
                
            print(f"🏢 Building Relational Ledger for: {client_name}")
            
            # Client
            new_client = Client(name=client_name, active=data.get('active', True), excess_funds=data.get('excess', 0.0))
            new_db.add(new_client)
            new_db.flush() 
            client_id = new_client.id
            po_id_map = {}

            # Purchase Orders
            po_terms = data.get('poTerms', {})
            for po_no, terms in po_terms.items():
                new_po = PurchaseOrder(
                    client_id=client_id, po_no=po_no,
                    adv_pct=terms.get('advPct', terms.get('adv', 0)),
                    ret_pct=terms.get('retPct', terms.get('ret', 0)),
                    ret_base=terms.get('retBase', terms.get('base', 'basic')),
                    tds_enabled=terms.get('tdsEnabled', False),
                    tds_rate=terms.get('tdsRate', 0),
                    tds_threshold=terms.get('tdsThreshold', 0)
                )
                new_db.add(new_po)
                new_db.flush()
                po_id_map[po_no] = new_po.id

            # Invoices
            invoices = data.get('invoices', [])
            for inv in invoices:
                po_no = inv.get('poNo', 'UNASSIGNED')
                po_id = po_id_map.get(po_no)

                if not po_id and po_no != 'UNASSIGNED':
                    lazy_po = PurchaseOrder(client_id=client_id, po_no=po_no)
                    new_db.add(lazy_po)
                    new_db.flush()
                    po_id = lazy_po.id
                    po_id_map[po_no] = po_id

                new_inv = Invoice(
                    client_id=client_id,
                    po_id=po_id,
                    invoice_no=inv.get('id'),
                    sub_entity=inv.get('subEntity', ''),
                    lr_no=inv.get('lrNo', ''),
                    inv_date=parse_date(inv.get('invDate')),
                    due_date=parse_date(inv.get('dueDate')),
                    basic=inv.get('basic', 0.0),
                    gst=inv.get('gst', 0.0),
                    total=inv.get('total', 0.0),
                    advance_adj=inv.get('advance', 0.0),
                    tds_ded=inv.get('tds', 0.0),
                    retention_held=inv.get('retention', 0.0),
                    net_payable=inv.get('netPayable', 0.0),
                    paid=inv.get('paid', 0.0),
                    balance=inv.get('balance', 0.0),
                    is_note=inv.get('isNote', False),
                    note_type=inv.get('noteType', ''),
                    note_reason=inv.get('noteReason', '')
                )
                new_db.add(new_inv)

            # Payments & Allocations
            payments = data.get('paymentHistory', [])
            for pay in payments:
                new_pay = PaymentHistory(
                    id=str(pay.get('id')),
                    client_id=client_id,
                    date=parse_date(pay.get('date')) or datetime.date.today(),
                    type=pay.get('type', 'RECEIPT'),
                    amount=pay.get('amount', 0.0),
                    details=pay.get('details', ''),
                    note=pay.get('note', '')
                )
                new_db.add(new_pay)
                new_db.flush()

                for alloc in pay.get('allocations', []):
                    new_alloc = PaymentAllocation(
                        payment_id=new_pay.id,
                        alloc_type=alloc.get('type', 'invoice'),
                        target_inv_id=alloc.get('invId', alloc.get('id')),
                        target_po_no=alloc.get('po'),
                        note_id=alloc.get('noteId'),
                        amount=alloc.get('amount', 0.0)
                    )
                    new_db.add(new_alloc)

        new_db.commit()
        print("\n✅ MIGRATION COMPLETE! Your Achint Chemicals data is officially live in the new engine.")

    except Exception as e:
        new_db.rollback()
        print(f"\n❌ Migration Failed: {e}")
    finally:
        old_conn.close()
        new_db.close()

if __name__ == "__main__":
    run_migration()