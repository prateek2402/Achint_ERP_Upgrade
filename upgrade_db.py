import sqlite3

def upgrade():
    print("⏳ Upgrading database schema...")
    conn = sqlite3.connect('erp_database.sqlite')
    c = conn.cursor()
    try:
        c.execute("ALTER TABLE po_baseline_items ADD COLUMN inspected_qty FLOAT DEFAULT 0.0;")
        print("✅ Success: Added inspected_qty to PO Baseline Items.")
    except Exception as e:
        print(f"Notice: {e}")
    try:
        c.execute("ALTER TABLE invoice_dispatch_items ADD COLUMN inspected_qty FLOAT DEFAULT 0.0;")
        print("✅ Success: Added inspected_qty to Invoice Dispatch Items.")
    except Exception as e:
        print(f"Notice: {e}")
    conn.commit()
    conn.close()

if __name__ == "__main__":
    upgrade()
