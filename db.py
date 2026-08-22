"""
Shared database setup for the ECR Viewer app.

Works with plain SQLite locally (default) and with Postgres (e.g. Neon)
on Render by setting the DATABASE_URL environment variable, e.g.:

    postgresql://user:password@host/dbname

If DATABASE_URL is not set, falls back to a local SQLite file
`ecr_viewer.db` in the current directory. NOTE: on Render's free tier,
local disk is ephemeral (wiped on redeploy/restart) - so for anything
you care about keeping, set DATABASE_URL to a real Postgres database.
"""
import os
from sqlalchemy import (
    create_engine, MetaData, Table, Column, Integer, String, Numeric,
    UniqueConstraint, select, insert, text
)

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///ecr_viewer.db")

# Render/Heroku-style URLs sometimes start with postgres:// which SQLAlchemy
# 1.4+/2.x no longer accepts directly - normalize it.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
metadata = MetaData()

establishments = Table(
    "establishments", metadata,
    Column("est_id", String, primary_key=True),
    Column("office_id", String),
    Column("est_name", String),
    Column("address1", String),
    Column("city", String),
    Column("district", String),
    Column("pin", String),
    Column("email", String),
    Column("task_id", String),
    Column("dsc", String),
    Column("esn", String),
    Column("form_5a", String),
)

ecr_monthly = Table(
    "ecr_monthly", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("est_id", String, index=True, nullable=False),
    Column("year", Integer, nullable=False),   # calendar year, e.g. 2020
    Column("month", Integer, nullable=False),  # calendar month, 1-12
    Column("ecr_count", Integer),
    Column("employees", Integer),
    Column("contribution", Numeric),
    UniqueConstraint("est_id", "year", "month", name="uq_est_year_month"),
)


def init_db():
    metadata.create_all(engine)


def _bulk_upsert(conn, table_name, columns, conflict_cols, rows, coalesce=True, chunk_size=500):
    """Insert/update many rows in one round-trip per chunk instead of one
    round-trip per row - this is what makes importing 50,000+ establishments
    over the internet (Neon) fast instead of taking hours."""
    if not rows:
        return
    dialect = engine.dialect.name
    excluded = "excluded" if dialect == "sqlite" else "EXCLUDED"
    update_cols = [c for c in columns if c not in conflict_cols]
    if coalesce:
        set_clause = ", ".join(f"{c} = COALESCE({excluded}.{c}, {table_name}.{c})" for c in update_cols)
    else:
        set_clause = ", ".join(f"{c} = {excluded}.{c}" for c in update_cols)
    conflict_clause = ", ".join(conflict_cols)

    for start in range(0, len(rows), chunk_size):
        chunk = rows[start:start + chunk_size]
        placeholders = []
        params = {}
        for i, row in enumerate(chunk):
            row_ph = []
            for col in columns:
                key = f"{col}_{i}"
                row_ph.append(f":{key}")
                params[key] = row.get(col)
            placeholders.append("(" + ", ".join(row_ph) + ")")
        sql = (
            f"INSERT INTO {table_name} ({', '.join(columns)}) "
            f"VALUES {', '.join(placeholders)} "
            f"ON CONFLICT ({conflict_clause}) DO UPDATE SET {set_clause}"
        )
        conn.execute(text(sql), params)


ESTABLISHMENT_COLUMNS = [
    "est_id", "office_id", "est_name", "address1", "city", "district",
    "pin", "email", "task_id", "dsc", "esn", "form_5a",
]

ECR_COLUMNS = ["est_id", "year", "month", "ecr_count", "employees", "contribution"]


def bulk_upsert_establishments(conn, rows):
    """rows: list of dicts with keys matching ESTABLISHMENT_COLUMNS."""
    _bulk_upsert(conn, "establishments", ESTABLISHMENT_COLUMNS, ["est_id"], rows, coalesce=True)


def bulk_upsert_ecr_rows(conn, rows):
    """rows: list of dicts with keys matching ECR_COLUMNS."""
    _bulk_upsert(conn, "ecr_monthly", ECR_COLUMNS, ["est_id", "year", "month"], rows, coalesce=False)
