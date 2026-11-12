"""
Database Migration: Add local_path column to generations table if missing
"""

import sqlite3
from pathlib import Path

DB_PATH = Path("v2fun_data/v2fun.db")

def migrate():
    """Add local_path column if it doesn't exist"""
    if not DB_PATH.exists():
        print("Database doesn't exist yet. Run init_db() first.")
        return
    
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    # Check if column exists
    cursor.execute("PRAGMA table_info(generations)")
    columns = [row[1] for row in cursor.fetchall()]
    
    if 'local_path' not in columns:
        print("Adding local_path column to generations table...")
        cursor.execute("ALTER TABLE generations ADD COLUMN local_path TEXT")
        conn.commit()
        print("[OK] Migration complete: local_path column added")
    else:
        print("[OK] Column local_path already exists, no migration needed")
    
    conn.close()

if __name__ == "__main__":
    migrate()
