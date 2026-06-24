# Database Migrations

Simple SQL migration system for the sipat-ml Supabase database.

## Prerequisites

Run the setup SQL once via Supabase SQL Editor:

```sql
-- From migrations/000_prereq_exec_sql.sql
CREATE OR REPLACE FUNCTION exec_sql(query TEXT)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
    EXECUTE query;
END;
$$;
```

## How It Works

- Migrations are `.sql` files in this directory, applied in filename order
- Applied migrations are tracked in the `_migrations` table
- Each migration runs once (idempotent via `IF NOT EXISTS` / `IF EXISTS` patterns)

## Creating a New Migration

1. Create a `.sql` file with a descriptive name (e.g., `add_index_to_detections.sql`)
2. Use idempotent SQL patterns:
   ```sql
   ALTER TABLE my_table ADD COLUMN IF NOT EXISTS new_col TEXT;
   CREATE INDEX IF NOT EXISTS idx_name ON my_table(col);
   ```
3. Run `python -m processing.migrations.runner apply`

## CLI Commands

```bash
# Apply all pending migrations
python -m processing.migrations.runner apply

# Show migration status
python -m processing.migrations.runner status
```

## Python API

```python
from processing.migrations.runner import apply_migrations, get_status

# Apply pending migrations
count = apply_migrations()

# Check status
status = get_status()
print(status["pending"])
```

## Notes

- SQL files are version-controlled (unlike the old `*.sql` gitignore rule)
- The `_migrations` table is created automatically on first run
- All migrations use PostgreSQL syntax for Supabase compatibility
