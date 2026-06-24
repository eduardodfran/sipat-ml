"""Simple migration runner for Supabase PostgreSQL database.

Tracks applied migrations in a `_migrations` table and applies pending
SQL files in filename order.

Prerequisites:
    Run migrations/000_prereq_exec_sql.sql via Supabase SQL Editor first.

Usage:
    python -m processing.migrations.runner apply
    python -m processing.migrations.runner status
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from processing.services.supabase_client import SupabaseService

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"
MIGRATIONS_TABLE = "_migrations"


def _exec_sql(db: SupabaseService, sql: str) -> None:
    """Execute raw SQL via the exec_sql RPC function."""
    db.client.rpc("exec_sql", {"query": sql}).execute()


def _ensure_migrations_table(db: SupabaseService) -> None:
    """Create the _migrations table if it doesn't exist."""
    sql = f"""
    CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} (
        id SERIAL PRIMARY KEY,
        filename TEXT UNIQUE NOT NULL,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """
    _exec_sql(db, sql)


def _get_applied(db: SupabaseService) -> set[str]:
    """Return set of already-applied migration filenames."""
    try:
        result = db.select(MIGRATIONS_TABLE, "filename")
        return {row["filename"] for row in result}
    except Exception:
        return set()


def _scan_migrations() -> list[Path]:
    """Return sorted list of .sql files in the migrations directory."""
    if not MIGRATIONS_DIR.exists():
        return []
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def _apply_migration(db: SupabaseService, path: Path) -> bool:
    """Apply a single migration file. Returns True on success."""
    sql = path.read_text(encoding="utf-8").strip()
    if not sql:
        logger.warning("Skipping empty migration: %s", path.name)
        return True

    logger.info("Applying migration: %s", path.name)
    try:
        _exec_sql(db, sql)
        db.insert(MIGRATIONS_TABLE, {"filename": path.name})
        logger.info("Applied: %s", path.name)
        return True
    except Exception as exc:
        logger.error("Failed to apply %s: %s", path.name, exc)
        return False


def apply_migrations(db: SupabaseService | None = None) -> int:
    """Apply all pending migrations. Returns count of applied migrations."""
    if db is None:
        db = SupabaseService()

    _ensure_migrations_table(db)
    applied = _get_applied(db)
    all_files = _scan_migrations()

    pending = [f for f in all_files if f.name not in applied]
    if not pending:
        logger.info("No pending migrations.")
        return 0

    logger.info("Found %d pending migration(s): %s", len(pending), [f.name for f in pending])

    count = 0
    for path in pending:
        if not _apply_migration(db, path):
            logger.error("Aborting due to failed migration: %s", path.name)
            break
        count += 1

    return count


def get_pending_migrations(db: SupabaseService | None = None) -> list[str]:
    """Return list of pending migration filenames."""
    if db is None:
        db = SupabaseService()

    _ensure_migrations_table(db)
    applied = _get_applied(db)
    all_files = _scan_migrations()
    return [f.name for f in all_files if f.name not in applied]


def get_status(db: SupabaseService | None = None) -> dict[str, Any]:
    """Return migration status summary."""
    if db is None:
        db = SupabaseService()

    _ensure_migrations_table(db)
    applied = _get_applied(db)
    all_files = _scan_migrations()
    pending = [f.name for f in all_files if f.name not in applied]

    return {
        "total": len(all_files),
        "applied": sorted(applied),
        "pending": pending,
    }


def main() -> None:
    """CLI entry point."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    command = sys.argv[1] if len(sys.argv) > 1 else "status"

    if command == "apply":
        count = apply_migrations()
        print(f"Applied {count} migration(s).")
    elif command == "status":
        status = get_status()
        print(f"Total migrations: {status['total']}")
        print(f"Applied: {len(status['applied'])}")
        print(f"Pending: {len(status['pending'])}")
        if status["pending"]:
            print("\nPending:")
            for name in status["pending"]:
                print(f"  - {name}")
    else:
        print(f"Unknown command: {command}")
        print("Usage: python -m processing.migrations.runner [apply|status]")
        sys.exit(1)


if __name__ == "__main__":
    main()
