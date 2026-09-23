from pathlib import Path
import os
from sqlalchemy import text
LOCK_ID=734821901

def run_migrations(engine)->None:
    directory=Path(os.getenv("MIGRATIONS_DIR","/app/migrations"))
    if not directory.exists(): directory=Path(__file__).resolve().parents[2]/"migrations"
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(:v)"),{"v":LOCK_ID})
        connection.exec_driver_sql("CREATE TABLE IF NOT EXISTS schema_migrations(name text PRIMARY KEY,applied_at timestamptz NOT NULL DEFAULT now())")
        applied=set(connection.execute(text("SELECT name FROM schema_migrations")).scalars())
        raw=connection.connection.driver_connection
        for path in sorted(directory.glob("*.sql")):
            if path.name in applied: continue
            with raw.cursor() as cursor: cursor.execute(path.read_text(encoding="utf-8"),prepare=False)
            connection.execute(text("INSERT INTO schema_migrations(name) VALUES (:name)"),{"name":path.name})
