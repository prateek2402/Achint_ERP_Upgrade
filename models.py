from sqlalchemy import Column, Integer, String, Float, Boolean, ForeignKey, Date, Text
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
    
    invoices = relationship("Invoice", back_populates="client", cascade="all, delete-orphan")
    purchase_orders = relationship("PurchaseOrder", back_populates="client", cascade="all, delete-orphan")
    payments = relationship("PaymentHistory", back_populates="client", cascade="all, delete-orphan")

class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), index=True)
    po_no = Column(String, index=True, nullable=False, unique=True)
    
    adv_pct = Column(Float, default=0.0)
    ret_pct = Column(Float, default=0.0)
    ret_base = Column(String, default="total")
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
    
    purchase_order = relationship("PurchaseOrder", back_populates="baseline_items")

class InvoiceDispatchItem(Base):
    __tablename__ = "invoice_dispatch_items"
    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), index=True)
    description = Column(String, nullable=False)
    dispatched_qty = Column(Float, default=0.0)
    uom = Column(String, nullable=True)
    inspected_qty = Column(Float, default=0.0) # NEW COLUMN
    
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

class SystemSettings(Base):
    __tablename__ = "system_settings"
    id = Column(Integer, primary_key=True, index=True)
    exchange_rate = Column(Float, default=83.0)
    custom_columns = Column(Text, default="[]") # Stored safely as a JSON string