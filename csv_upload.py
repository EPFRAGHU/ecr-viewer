"""
CSV processing for the admin upload page (/admin/upload).

Two entry points, each returning a summary dict:
    process_master_csv(file_storage, admin_user)
    process_ecr_csv(file_storage, admin_user, fy_year, fy_month)

Both validate the file, parse it with pandas, upsert valid rows inside a
single transaction (so a bad file never leaves partial updates), and log
the upload to `upload_log`. Rows with problems are skipped and reported
back with a reason rather than aborting the whole file.
"""
import pandas as pd

from db import (
    engine, establishments, log_upload,
    bulk_upsert_establishments, bulk_upsert_ecr_rows,
    existing_est_ids, existing_ecr_est_ids,
)
from import_data import _clean, _to_num

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_ERRORS_SHOWN = 50

ECR_CANDIDATE_COLUMNS = {
    "est_id": ["EST_ID", "ESTABLISHMENT_ID", "ESTABLISHMENT_CODE"],
    "ecr_count": ["ECR_COUNT", "ECR", "NO_OF_ECR"],
    "employees": ["EMPLOYEES", "MEMBERS", "MEM", "EMPLOYEE_COUNT", "NO_OF_MEMBERS"],
    "contribution": ["CONTRIBUTION", "AMOUNT", "AMT", "CONTRIBUTION_AMOUNT", "TOTAL_AMOUNT"],
}


class UploadError(Exception):
    """A problem that aborts the whole file (bad extension, unreadable CSV,
    missing required column) - as opposed to a single bad row."""


def _validate_csv_file(file_storage):
    if not file_storage or not file_storage.filename:
        raise UploadError("No file selected.")
    if not file_storage.filename.lower().endswith(".csv"):
        raise UploadError("Only .csv files are accepted.")
    file_storage.stream.seek(0, 2)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > MAX_UPLOAD_BYTES:
        raise UploadError(f"File is {size / 1024 / 1024:.1f} MB - the limit is 20MB.")


def _read_csv(file_storage):
    try:
        df = pd.read_csv(file_storage, dtype=str)
    except Exception as e:
        raise UploadError(f"Could not parse CSV: {e}")
    df.columns = [c.strip().upper() for c in df.columns]
    return df


def _match_column(columns, candidates):
    for c in candidates:
        if c in columns:
            return c
    return None


def process_master_csv(file_storage, admin_user):
    _validate_csv_file(file_storage)
    df = _read_csv(file_storage)
    if "EST_ID" not in df.columns:
        raise UploadError("Missing required column: EST_ID")

    rows_read = len(df)
    batch = []
    errors = []
    seen_ids = set()

    for i, row in enumerate(df.to_dict("records"), start=2):  # row 1 is the header
        est_id = _clean(row.get("EST_ID"))
        if not est_id:
            errors.append(f"Row {i}: missing establishment code")
            continue
        est_id = est_id.upper()
        seen_ids.add(est_id)
        batch.append({
            "est_id": est_id,
            "office_id": _clean(row.get("OFFICE_ID")),
            "est_name": _clean(row.get("EST_NAME")),
            "address1": _clean(row.get("ADDRESS_LINE1")),
            "city": _clean(row.get("CITY")),
            "district": _clean(row.get("DISTRICT_NAME")),
            "pin": _clean(row.get("PIN_CODE")),
            "cover_date": _clean(row.get("COVER_DATE")),
            "industry": _clean(row.get("INDUSTRY")),
            "coverage_section": _clean(row.get("COVERAGE_SECTION")),
            "email": _clean(row.get("PRIMARY_EMAIL")),
            "task_id": _clean(row.get("ACC_TASK_ID")),
            "dsc": _clean(row.get("DSC")),
            "esn": _clean(row.get("ESN")),
            "form_5a": _clean(row.get("F5A")),
        })

    inserted = updated = 0
    with engine.begin() as conn:
        already_there = existing_est_ids(conn, seen_ids)
        bulk_upsert_establishments(conn, batch)
        for r in batch:
            if r["est_id"] in already_there:
                updated += 1
            else:
                inserted += 1
        log_upload(
            conn, admin_user=admin_user, data_type="master", filename=file_storage.filename,
            fy_year=None, fy_month=None, rows_read=rows_read,
            rows_inserted=inserted, rows_updated=updated, rows_skipped=len(errors),
        )

    return {
        "rows_read": rows_read, "inserted": inserted, "updated": updated,
        "skipped": len(errors), "errors": errors[:MAX_ERRORS_SHOWN],
        "errors_truncated": len(errors) > MAX_ERRORS_SHOWN,
    }


def process_ecr_csv(file_storage, admin_user, fy_year, fy_month, calendar_year):
    _validate_csv_file(file_storage)
    df = _read_csv(file_storage)

    col_est = _match_column(df.columns, ECR_CANDIDATE_COLUMNS["est_id"])
    if not col_est:
        raise UploadError("Could not find an establishment code column (expected EST_ID).")
    col_ecr = _match_column(df.columns, ECR_CANDIDATE_COLUMNS["ecr_count"])
    col_mem = _match_column(df.columns, ECR_CANDIDATE_COLUMNS["employees"])
    col_amt = _match_column(df.columns, ECR_CANDIDATE_COLUMNS["contribution"])
    if not (col_ecr or col_mem or col_amt):
        raise UploadError("Could not find ECR count / employees / contribution columns.")

    rows_read = len(df)
    batch = []
    errors = []
    seen_ids = set()

    for i, row in enumerate(df.to_dict("records"), start=2):
        est_id = _clean(row.get(col_est))
        if not est_id:
            errors.append(f"Row {i}: missing establishment code")
            continue
        est_id = est_id.upper()

        row_bad = False
        ecr_count = employees = contribution = None
        if col_ecr:
            raw = row.get(col_ecr)
            ecr_count = _to_num(raw)
            if ecr_count is None and _clean(raw) is not None:
                errors.append(f"Row {i}: invalid ECR count")
                row_bad = True
        if col_mem:
            raw = row.get(col_mem)
            employees = _to_num(raw)
            if employees is None and _clean(raw) is not None:
                errors.append(f"Row {i}: invalid employee count")
                row_bad = True
        if col_amt:
            raw = row.get(col_amt)
            contribution = _to_num(raw)
            if contribution is None and _clean(raw) is not None:
                errors.append(f"Row {i}: invalid contribution amount")
                row_bad = True
        if row_bad:
            continue
        if ecr_count is None and employees is None and contribution is None:
            errors.append(f"Row {i}: no ECR/employee/contribution data")
            continue

        seen_ids.add(est_id)
        batch.append({
            "est_id": est_id, "year": calendar_year, "month": fy_month,
            "ecr_count": ecr_count, "employees": employees, "contribution": contribution,
        })

    inserted = updated = 0
    with engine.begin() as conn:
        already_there = existing_ecr_est_ids(conn, calendar_year, fy_month, seen_ids)
        bulk_upsert_ecr_rows(conn, batch)
        for r in batch:
            if r["est_id"] in already_there:
                updated += 1
            else:
                inserted += 1
        log_upload(
            conn, admin_user=admin_user, data_type="ecr", filename=file_storage.filename,
            fy_year=fy_year, fy_month=fy_month, rows_read=rows_read,
            rows_inserted=inserted, rows_updated=updated, rows_skipped=len(errors),
        )

    return {
        "rows_read": rows_read, "inserted": inserted, "updated": updated,
        "skipped": len(errors), "errors": errors[:MAX_ERRORS_SHOWN],
        "errors_truncated": len(errors) > MAX_ERRORS_SHOWN,
    }
