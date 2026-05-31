"""
migrate.py — applies any unapplied database migrations, in order.

Usage:
    python migrate.py

Migrations live in the migrations/ folder and are named:
    001_description.sql
    002_description.sql
    ...

Each migration is run exactly once. A table called schema_migrations records
which files have already been applied. Running migrate.py when everything is
already up to date does nothing and is safe.
"""

import os
import sys
import glob
import psycopg2
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL is not set. Check your .env file.")
    sys.exit(1)

MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")


def get_connection():
    return psycopg2.connect(DATABASE_URL)


def ensure_migrations_table(conn):
    """Create the schema_migrations tracking table if it doesn't exist yet."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename   TEXT        PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
    conn.commit()


def already_applied(conn, filename):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM schema_migrations WHERE filename = %s", (filename,)
        )
        return cur.fetchone() is not None


def record_migration(conn, filename):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schema_migrations (filename) VALUES (%s)", (filename,)
        )
    conn.commit()


def run_migrations():
    # Find all .sql files in migrations/, sorted by name (so 001 runs before 002, etc.)
    pattern = os.path.join(MIGRATIONS_DIR, "*.sql")
    files = sorted(glob.glob(pattern))

    if not files:
        print("No migration files found in migrations/")
        return

    conn = get_connection()
    ensure_migrations_table(conn)

    applied = 0
    skipped = 0

    for filepath in files:
        filename = os.path.basename(filepath)

        if already_applied(conn, filename):
            print(f"  [skip]    {filename}  (already applied)")
            skipped += 1
            continue

        print(f"  [running] {filename} ...", end="", flush=True)

        with open(filepath, "r") as f:
            sql = f.read()

        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            # The migration SQL itself contains BEGIN/COMMIT, but psycopg2 also
            # wraps everything in a transaction. Record the migration inside the
            # same commit so it's atomic with the schema change.
            record_migration(conn, filename)
            print(" done.")
            applied += 1
        except Exception as e:
            conn.rollback()
            print(f"\n\nERROR applying {filename}:\n{e}")
            print("\nThe database has been rolled back to its previous state.")
            print("Fix the issue and run migrate.py again.")
            conn.close()
            sys.exit(1)

    conn.close()

    print()
    if applied == 0:
        print("Already up to date. Nothing to apply.")
    else:
        print(f"{applied} migration(s) applied successfully.")


if __name__ == "__main__":
    print(f"Looking for migrations in: {MIGRATIONS_DIR}\n")
    run_migrations()
