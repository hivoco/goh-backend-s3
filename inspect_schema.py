"""
READ-ONLY schema check. Connects to the DB, lists every table + column, and
compares against what the SQLAlchemy models expect — printing exactly which
columns the code needs but the DB is missing (and any extra/old columns).

Run this after `migrations/001_share_and_admin_tables.sql` to confirm the
database and the code agree. Makes NO changes.

    python inspect_schema.py
"""
import os

# Load .env into the environment (so app config + DATABASE_URL are available).
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
with open(ENV_PATH) as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

from sqlalchemy import create_engine, inspect

engine = create_engine(os.environ["DATABASE_URL"])
insp = inspect(engine)

# What the DB actually has.
db_cols: dict[str, set[str]] = {}
print("\n===== TABLES & COLUMNS CURRENTLY IN THE DATABASE =====")
for table in sorted(insp.get_table_names()):
    cols = insp.get_columns(table)
    db_cols[table] = {c["name"] for c in cols}
    print(f"\n[{table}]")
    for c in cols:
        null = "NULL" if c["nullable"] else "NOT NULL"
        print(f"  - {c['name']}: {c['type']} {null}")

# What the models expect (import every model module so they register on Base).
import importlib
import pkgutil
import app.models
from app.core.database import Base

for mod in pkgutil.iter_modules(app.models.__path__):
    importlib.import_module(f"app.models.{mod.name}")

print("\n\n===== DIFFERENCES (code vs database) =====")
problems = False
for tablename, table in sorted(Base.metadata.tables.items()):
    model_cols = {c.name for c in table.columns}
    have = db_cols.get(tablename)
    if have is None:
        print(f"\n[{tablename}]  ❌ TABLE MISSING ENTIRELY")
        problems = True
        continue
    missing = model_cols - have          # code needs these, DB lacks them
    extra = have - model_cols            # DB has these, code no longer uses them
    if missing or extra:
        problems = True
        print(f"\n[{tablename}]")
        if missing:
            print(f"  ❌ MISSING (add these): {sorted(missing)}")
        if extra:
            print(f"  ⚠️  extra/old (harmless, code ignores): {sorted(extra)}")

if not problems:
    print("\n✅ Database matches the code — nothing to change.")
print()
