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


def upsert_establishment(conn, est_id, office_id, est_name, address1=None, city=None,
                          district=None, pin=None, email=None, task_id=None,
                          dsc=None, esn=None, form_5a=None):
    """Insert an establishment, or update its details if it already exists."""
    dialect = engine.dialect.name
    params = {
        "est_id": est_id, "office_id": office_id, "est_name": est_name,
        "address1": address1, "city": city, "district": district, "pin": pin,
        "email": email, "task_id": task_id, "dsc": dsc, "esn": esn, "form_5a": form_5a,
    }
    if dialect == "sqlite":
        conn.execute(text("""
            INSERT INTO establishments (est_id, office_id, est_name, address1, city,
                district, pin, email, task_id, dsc, esn, form_5a)
            VALUES (:est_id, :office_id, :est_name, :address1, :city,
                :district, :pin, :email, :task_id, :dsc, :esn, :form_5a)
            ON CONFLICT(est_id) DO UPDATE SET
                office_id = COALESCE(excluded.office_id, establishments.office_id),
                est_name = COALESCE(excluded.est_name, establishments.est_name),
                address1 = COALESCE(excluded.address1, establishments.address1),
                city = COALESCE(excluded.city, establishments.city),
                district = COALESCE(excluded.district, establishments.district),
                pin = COALESCE(excluded.pin, establishments.pin),
                email = COALESCE(excluded.email, establishments.email),
                task_id = COALESCE(excluded.task_id, establishments.task_id),
                dsc = COALESCE(excluded.dsc, establishments.dsc),
                esn = COALESCE(excluded.esn, establishments.esn),
                form_5a = COALESCE(excluded.form_5a, establishments.form_5a)
        """), params)
    else:  # postgres
        conn.execute(text("""
            INSERT INTO establishments (est_id, office_id, est_name, address1, city,
                district, pin, email, task_id, dsc, esn, form_5a)
            VALUES (:est_id, :office_id, :est_name, :address1, :city,
                :district, :pin, :email, :task_id, :dsc, :esn, :form_5a)
            ON CONFLICT (est_id) DO UPDATE SET
                office_id = COALESCE(EXCLUDED.office_id, establishments.office_id),
                est_name = COALESCE(EXCLUDED.est_name, establishments.est_name),
                address1 = COALESCE(EXCLUDED.address1, establishments.address1),
                city = COALESCE(EXCLUDED.city, establishments.city),
                district = COALESCE(EXCLUDED.district, establishments.district),
                pin = COALESCE(EXCLUDED.pin, establishments.pin),
                email = COALESCE(EXCLUDED.email, establishments.email),
                task_id = COALESCE(EXCLUDED.task_id, establishments.task_id),
                dsc = COALESCE(EXCLUDED.dsc, establishments.dsc),
                esn = COALESCE(EXCLUDED.esn, establishments.esn),
                form_5a = COALESCE(EXCLUDED.form_5a, establishments.form_5a)
        """), params)


def upsert_ecr_row(conn, est_id, year, month, ecr_count, employees, contribution):
    """Insert one establishment/year/month ECR record, or overwrite it if a
    later CSV re-supplies the same month (last import wins)."""
    dialect = engine.dialect.name
    params = {
        "est_id": est_id, "year": year, "month": month,
        "ecr_count": ecr_count, "employees": employees,
        "contribution": contribution,
    }
    if dialect == "sqlite":
        conn.execute(text("""
            INSERT INTO ecr_monthly (est_id, year, month, ecr_count, employees, contribution)
            VALUES (:est_id, :year, :month, :ecr_count, :employees, :contribution)
            ON CONFLICT(est_id, year, month) DO UPDATE SET
                ecr_count = excluded.ecr_count,
                employees = excluded.employees,
                contribution = excluded.contribution
        """), params)
    else:
        conn.execute(text("""
            INSERT INTO ecr_monthly (est_id, year, month, ecr_count, employees, contribution)
            VALUES (:est_id, :year, :month, :ecr_count, :employees, :contribution)
            ON CONFLICT (est_id, year, month) DO UPDATE SET
                ecr_count = EXCLUDED.ecr_count,
                employees = EXCLUDED.employees,
                contribution = EXCLUDED.contribution
        """), params)
