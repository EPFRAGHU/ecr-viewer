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
    create_engine, MetaData, Table, Column, Integer, String, Numeric, DateTime,
    UniqueConstraint, select, insert, func, text
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
    Column("cover_date", String),
    Column("industry", String),
    Column("coverage_section", String),
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

upload_log = Table(
    "upload_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("admin_user", String, nullable=False),
    Column("data_type", String, nullable=False),   # 'master' or 'ecr'
    Column("filename", String, nullable=False),
    Column("fy_year", Integer),    # display FY start year; null for master uploads
    Column("fy_month", Integer),   # calendar month 1-12; null for master uploads
    Column("rows_read", Integer, nullable=False),
    Column("rows_inserted", Integer, nullable=False),
    Column("rows_updated", Integer, nullable=False),
    Column("rows_skipped", Integer, nullable=False),
    Column("uploaded_at", DateTime, nullable=False, server_default=func.now()),
)


def init_db():
    metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns():
    """Best-effort schema patch for tables that already existed before a
    column was added to this file (no migration framework here) - adds any
    missing nullable columns in place so older deployed databases don't
    break on the next deploy."""
    from sqlalchemy import inspect
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    for table in metadata.tables.values():
        if table.name not in existing_tables:
            continue
        existing_cols = {c["name"] for c in insp.get_columns(table.name)}
        missing = [c for c in table.columns if c.name not in existing_cols]
        if not missing:
            continue
        with engine.begin() as conn:
            for col in missing:
                type_sql = col.type.compile(engine.dialect)
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {type_sql}'))


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
    "pin", "cover_date", "industry", "coverage_section",
    "email", "task_id", "dsc", "esn", "form_5a",
]

ECR_COLUMNS = ["est_id", "year", "month", "ecr_count", "employees", "contribution"]


def bulk_upsert_establishments(conn, rows):
    """rows: list of dicts with keys matching ESTABLISHMENT_COLUMNS."""
    _bulk_upsert(conn, "establishments", ESTABLISHMENT_COLUMNS, ["est_id"], rows, coalesce=True)


def bulk_upsert_ecr_rows(conn, rows):
    """rows: list of dicts with keys matching ECR_COLUMNS."""
    _bulk_upsert(conn, "ecr_monthly", ECR_COLUMNS, ["est_id", "year", "month"], rows, coalesce=False)


def log_upload(conn, *, admin_user, data_type, filename, fy_year, fy_month,
                rows_read, rows_inserted, rows_updated, rows_skipped):
    conn.execute(insert(upload_log).values(
        admin_user=admin_user, data_type=data_type, filename=filename,
        fy_year=fy_year, fy_month=fy_month, rows_read=rows_read,
        rows_inserted=rows_inserted, rows_updated=rows_updated, rows_skipped=rows_skipped,
    ))


def get_data_stats():
    """Per-data-type row count + last admin-upload timestamp, for the admin upload page."""
    with engine.connect() as conn:
        master_count = conn.execute(select(func.count()).select_from(establishments)).scalar()
        ecr_count = conn.execute(select(func.count()).select_from(ecr_monthly)).scalar()
        master_last = conn.execute(
            select(func.max(upload_log.c.uploaded_at)).where(upload_log.c.data_type == "master")
        ).scalar()
        ecr_last = conn.execute(
            select(func.max(upload_log.c.uploaded_at)).where(upload_log.c.data_type == "ecr")
        ).scalar()
    return {
        "master": {"count": master_count, "last_updated": master_last},
        "ecr": {"count": ecr_count, "last_updated": ecr_last},
    }


def get_recent_uploads(limit=10):
    with engine.connect() as conn:
        rows = conn.execute(
            select(upload_log).order_by(upload_log.c.uploaded_at.desc(), upload_log.c.id.desc()).limit(limit)
        ).mappings().all()
    return [dict(r) for r in rows]


def get_overall_upload_status():
    """Latest admin-upload timestamp across all data + total upload count,
    for the "data as of ..." note on the main viewer page."""
    with engine.connect() as conn:
        last = conn.execute(select(func.max(upload_log.c.uploaded_at))).scalar()
        count = conn.execute(select(func.count()).select_from(upload_log)).scalar()
    return {"last_updated": last, "version": count}


def existing_est_ids(conn, est_ids, chunk_size=900):
    """Which of the given est_ids already exist in `establishments` - used
    to classify admin-upload rows as inserted vs. updated."""
    existing = set()
    est_ids = list(est_ids)
    for start in range(0, len(est_ids), chunk_size):
        chunk = est_ids[start:start + chunk_size]
        rows = conn.execute(
            select(establishments.c.est_id).where(establishments.c.est_id.in_(chunk))
        ).scalars().all()
        existing.update(rows)
    return existing


def existing_ecr_est_ids(conn, year, month, est_ids, chunk_size=900):
    """Which of the given est_ids already have an ecr_monthly row for
    (year, month) - used to classify admin-upload ECR rows as inserted vs.
    updated."""
    existing = set()
    est_ids = list(est_ids)
    for start in range(0, len(est_ids), chunk_size):
        chunk = est_ids[start:start + chunk_size]
        rows = conn.execute(
            select(ecr_monthly.c.est_id).where(
                ecr_monthly.c.year == year,
                ecr_monthly.c.month == month,
                ecr_monthly.c.est_id.in_(chunk),
            )
        ).scalars().all()
        existing.update(rows)
    return existing
