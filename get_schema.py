import sqlite3

def extract_schema():
    print("🔍 Scanning old_erp.sqlite...\n")
    try:
        conn = sqlite3.connect('old_erp.sqlite')
        cursor = conn.cursor()
        
        # Get all table creation statements
        cursor.execute("SELECT name, sql FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
        
        for table in tables:
            print(f"--- TABLE: {table[0]} ---")
            print(table[1])
            print("\n")
            
        conn.close()
    except Exception as e:
        print(f"Error reading database: {e}")

if __name__ == "__main__":
    extract_schema()
