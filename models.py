from sqlalchemy import Column, Integer, String, Float, Boolean, ForeignKey, Date, DateTime, Text
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    role = Column(String, default="user")

class Client(Base):
    __tablename__ = "clients"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    active = Column(Boolean, default=True)
    excess_funds = Column(Float, default=0.0)
    display_currency = Column(String, default="INR")
    exchange_rate = Column(Float, default=83.0)
    
    invoices = relationship("Invoice", back_populates="client", cascade="all, delete-orphan")
    purchase_orders = relationship("PurchaseOrder", back_populates="client", cascade="all, delete-orphan")
    payments = relationship("PaymentHistory", back_populates="client", cascade="all, delete-orphan")

class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), index=True)
    po_no = Column(String, index=True, nullable=False, unique=True)
    contact_person = Column(String, nullable=True)
    project_name = Column(String, nullable=True)
    is_completed = Column(Boolean, default=False)
    is_hidden = Column(Boolean, default=False)
    completed_at = Column(Date, nullable=True)
    
    adv_pct = Column(Float, default=0.0)
    ret_pct = Column(Float, default=0.0)
    ret_base = Column(String, default="total")
    tds_base = Column(String, default="basic")
    tds_enabled = Column(Boolean, default=False)
    tds_rate = Column(Float, default=0.0)
    tds_threshold = Column(Float, default=0.0)
    
    advance_pool = Column(Float, default=0.0) 

    client = relationship("Client", back_populates="purchase_orders")
    invoices = relationship("Invoice", back_populates="purchase_order")
    baseline_items = relationship("PoBaselineItem", back_populates="purchase_order", cascade="all, delete-orphan")


class PoBaselineItem(Base):
    __tablename__ = "po_baseline_items"
    id = Column(Integer, primary_key=True, index=True)
    po_id = Column(Integer, ForeignKey("purchase_orders.id"), index=True)
    description = Column(String, nullable=False)
    ordered_qty = Column(Float, default=0.0)
    inspected_qty = Column(Float, default=0.0) # NEW COLUMN
    uom = Column(String, nullable=True)
    material_type = Column(String, nullable=True)  # brick | castable_mortar
    dispatch_alias = Column(String, nullable=True)
    dispatch_rate = Column(Float, default=0.0)
    
    purchase_order = relationship("PurchaseOrder", back_populates="baseline_items")

class InvoiceDispatchItem(Base):
    __tablename__ = "invoice_dispatch_items"
    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), index=True)
    description = Column(String, nullable=False)
    dispatched_qty = Column(Float, default=0.0)
    uom = Column(String, nullable=True)
    inspected_qty = Column(Float, default=0.0) # NEW COLUMN
    rate_per_uom = Column(Float, default=0.0)
    
    invoice = relationship("Invoice", back_populates="dispatch_items")

class Invoice(Base):
    __tablename__ = "invoices"
    id = Column(Integer, primary_key=True, index=True)
    invoice_no = Column(String, index=True, nullable=False, unique=True) 
    client_id = Column(Integer, ForeignKey("clients.id"), index=True)
    po_id = Column(Integer, ForeignKey("purchase_orders.id"), nullable=True, index=True)
    
    sub_entity = Column(String, nullable=True)
    lr_no = Column(String, nullable=True)
    inv_date = Column(Date, nullable=True)
    due_date = Column(Date, nullable=True)
    
    basic = Column(Float, default=0.0)
    gst = Column(Float, default=0.0)
    total = Column(Float, default=0.0)
    advance_adj = Column(Float, default=0.0)
    tds_ded = Column(Float, default=0.0)
    retention_held = Column(Float, default=0.0)
    net_payable = Column(Float, default=0.0)
    paid = Column(Float, default=0.0)
    balance = Column(Float, default=0.0)
    
    is_note = Column(Boolean, default=False)
    note_type = Column(String, nullable=True) 
    note_reason = Column(Text, nullable=True)
    
    client = relationship("Client", back_populates="invoices")
    purchase_order = relationship("PurchaseOrder", back_populates="invoices")
    dispatch_items = relationship("InvoiceDispatchItem", back_populates="invoice", cascade="all, delete-orphan")

class PaymentHistory(Base):
    __tablename__ = "payment_history"
    id = Column(String, primary_key=True, index=True) # The frontend uses Date.now().toString() for IDs
    client_id = Column(Integer, ForeignKey("clients.id"), index=True)
    
    date = Column(Date, nullable=False)
    type = Column(String, nullable=False) 
    amount = Column(Float, default=0.0)
    details = Column(Text, nullable=True)
    note = Column(Text, nullable=True)
    
    client = relationship("Client", back_populates="payments")
    allocations = relationship("PaymentAllocation", back_populates="payment", cascade="all, delete-orphan")

class PaymentAllocation(Base):
    __tablename__ = "payment_allocations"
    id = Column(Integer, primary_key=True, index=True)
    payment_id = Column(String, ForeignKey("payment_history.id"), index=True)
    
    alloc_type = Column(String, nullable=False) 
    target_inv_id = Column(String, nullable=True)
    target_po_no = Column(String, nullable=True)
    note_id = Column(String, nullable=True) 
    
    amount = Column(Float, default=0.0)

    payment = relationship("PaymentHistory", back_populates="allocations")


class UnallocatedPaymentRegister(Base):
    __tablename__ = "unallocated_payment_register"
    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), index=True, nullable=False)
    source_payment_id = Column(String, ForeignKey("payment_history.id"), index=True, nullable=True)
    created_on = Column(Date, nullable=False)
    amount = Column(Float, default=0.0)
    balance = Column(Float, default=0.0)
    status = Column(String, default="open")
    note = Column(Text, nullable=True)


class UnallocatedAdvanceRegister(Base):
    __tablename__ = "unallocated_advance_register"
    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), index=True, nullable=False)
    source_payment_id = Column(String, ForeignKey("payment_history.id"), index=True, nullable=True)
    po_no = Column(String, nullable=True)
    created_on = Column(Date, nullable=False)
    amount = Column(Float, default=0.0)
    balance = Column(Float, default=0.0)
    status = Column(String, default="open")
    note = Column(Text, nullable=True)

class SystemSettings(Base):
    __tablename__ = "system_settings"
    id = Column(Integer, primary_key=True, index=True)
    exchange_rate = Column(Float, default=83.0)
    custom_columns = Column(Text, default="[]") # Stored safely as a JSON string
    fy_start_month = Column(Integer, default=4)
    fy_start_day = Column(Integer, default=1)


class UploadedDocument(Base):
    """Cache + dedupe table for AI-extracted invoice/PO PDF uploads.

    The same PDF bytes can land in the system multiple times (re-sends,
    re-uploads, sync glitches). Storing a SHA-256 of the bytes lets us
    short-circuit Gemini calls on duplicates and surface 'already seen'
    semantics to the UI.
    """

    __tablename__ = "uploaded_documents"
    id = Column(Integer, primary_key=True, index=True)
    sha256 = Column(String(64), nullable=False, index=True)
    kind = Column(String(32), nullable=False, index=True)  # invoice | po
    original_filename = Column(String, nullable=True)
    byte_size = Column(Integer, nullable=True)
    uploaded_by = Column(String, nullable=True)
    uploaded_at = Column(DateTime, nullable=False)
    parsed_invoice_no = Column(String, nullable=True)  # for invoices, when known
    parsed_po_no = Column(String, nullable=True)  # for POs, when known
    status = Column(String(32), nullable=False, default="extracted")  # extracted | parse_error
    raw_data = Column(Text, nullable=True)
    parsed_json = Column(Text, nullable=True)
    warnings_json = Column(Text, nullable=True)
    parse_error = Column(String, nullable=True)


class AuditLog(Base):
    """Append-only audit trail for financial/dispatch mutations.

    Designed for "who changed what, when" forensics. Writes are best-effort:
    failure to record an audit row never aborts the underlying business
    operation (recorded via record_audit() in main.py).
    """

    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True, index=True)
    at_utc = Column(DateTime, nullable=False, index=True)
    user_id = Column(Integer, nullable=True, index=True)
    username = Column(String, nullable=True, index=True)
    role = Column(String, nullable=True)
    entity_type = Column(String, nullable=False, index=True)
    entity_id = Column(String, nullable=True, index=True)
    action = Column(String, nullable=False, index=True)  # create | update | delete | misc
    summary = Column(String, nullable=True)
    details = Column(Text, nullable=True)  # JSON-encoded payload
    ip_address = Column(String, nullable=True)