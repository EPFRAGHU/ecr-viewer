"""
CSV processing for the admin upload page (/admin/upload).

save_upload(file_storage, dest_dir) validates a file and saves it to disk,
returning a path for background processing - this lets the HTTP request
return immediately instead of blocking (and potentially timing out) while
a 50,000-row file is processed.

process_master_csv(path, filename, admin_user, progress_cb=None) and
process_ecr_csv(path, filename, admin_user, fy_year, fy_month, calendar_year, progress_cb=None)
then do the actual work, each returning a summary dict. Both read and
upsert the file in fixed-size batches (own transaction per batch) rather
than one giant transaction, so memory use and lock duration stay bounded
regardless of file size, and progress_cb(rows_done, rows_total) can be
used to drive a progress bar. Rows with problems are skipped and reported
back with a reason rather than aborting the whole file.
"""
import os
import uuid

import pandas as pd

from db import (
    engine, log_upload,
    bulk_upsert_establishments, bulk_upsert_ecr_rows,
    existing_est_ids, existing_ecr_est_ids,
)
from import_data import _clean, _to_num

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_ERRORS_SHOWN = 50
BATCH_SIZE = 1000

ECR_CANDIDATE_COLUMNS = {
    "est_id": ["EST_ID", "ESTABLISHMENT_ID", "ESTABLISHMENT_CODE"],
    "ecr_count": ["ECR_COUNT", "ECR", "NO_OF_ECR"],
    "employees": ["EMPLOYEES", "MEMBERS", "MEM", "EMPLOYEE_COUNT", "NO_OF_MEMBERS"],
    "contribution": ["CONTRIBUTION", "AMOUNT", "AMT", "CONTRIBUTION_AMOUNT", "TOTAL_AMOUNT"],
}


class UploadError(Exception):
    """A problem that aborts the whole file (bad extension, unreadable CSV,
    missing required column) - as opposed to a single bad row."""


def save_upload(file_storage, dest_dir):
    """Validate a Flask FileStorage and save it to dest_dir, returning the
    saved path. Must be called within the request (FileStorage isn't valid
    once the request ends) - the returned path is then handed to a
    background thread for the actual processing."""
    if not file_storage or not file_storage.filename:
        raise UploadError("No file selected.")
    if not file_storage.filename.lower().endswith(".csv"):
        raise UploadError("Only .csv files are accepted.")
    file_storage.stream.seek(0, 2)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > MAX_UPLOAD_BYTES:
        raise UploadError(f"File is {size / 1024 / 1024:.1f} MB - the limit is {MAX_UPLOAD_BYTES // (1024 * 1024)}MB.")

    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, f"{uuid.uuid4().hex}.csv")
    file_storage.save(path)
    return path


def _count_data_rows(path):
    """Fast line count so the progress bar has a denominator up front,
    without loading the whole file into pandas."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        total = sum(1 for _ in f)
    return max(total - 1, 0)  # minus the header row


def _match_column(columns, candidates):
    for c in candidates:
        if c in columns:
            return c
    return None


def process_master_csv(path, filename, admin_user, progress_cb=None):
    total_rows = _count_data_rows(path)
    rows_read = 0
    inserted = updated = skipped = 0
    errors = []

    try:
        chunks = pd.read_csv(path, dtype=str, chunksize=BATCH_SIZE)
        for chunk_idx, chunk in enumerate(chunks):
            chunk.columns = [c.strip().upper() for c in chunk.columns]
            if chunk_idx == 0 and "EST_ID" not in chunk.columns:
                raise UploadError("Missing required column: EST_ID")

            start_row = rows_read + 2  # row 1 is the header
            batch = []
            seen_ids = set()
            for offset, row in enumerate(chunk.to_dict("records")):
                i = start_row + offset
                est_id = _clean(row.get("EST_ID"))
                if not est_id:
                    errors.append(f"Row {i}: missing establishment code")
                    skipped += 1
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
            rows_read += len(chunk)

            if batch:
                with engine.begin() as conn:
                    already_there = existing_est_ids(conn, seen_ids)
                    bulk_upsert_establishments(conn, batch)
                for r in batch:
                    if r["est_id"] in already_there:
                        updated += 1
                    else:
                        inserted += 1

            if progress_cb:
                progress_cb(rows_read, total_rows)
    except UploadError:
        raise
    except Exception as e:
        raise UploadError(f"Could not parse CSV: {e}")

    with engine.begin() as conn:
        log_upload(
            conn, admin_user=admin_user, data_type="master", filename=filename,
            fy_year=None, fy_month=None, rows_read=rows_read,
            rows_inserted=inserted, rows_updated=updated, rows_skipped=skipped,
        )

    return {
        "rows_read": rows_read, "inserted": inserted, "updated": updated,
        "skipped": skipped, "errors": errors[:MAX_ERRORS_SHOWN],
        "errors_truncated": len(errors) > MAX_ERRORS_SHOWN,
    }


def process_ecr_csv(path, filename, admin_user, fy_year, fy_month, calendar_year, progress_cb=None):
    total_rows = _count_data_rows(path)
    rows_read = 0
    inserted = updated = skipped = 0
    errors = []

    try:
        chunks = pd.read_csv(path, dtype=str, chunksize=BATCH_SIZE)
        col_est = col_ecr = col_mem = col_amt = None
        for chunk_idx, chunk in enumerate(chunks):
            chunk.columns = [c.strip().upper() for c in chunk.columns]
            if chunk_idx == 0:
                col_est = _match_column(chunk.columns, ECR_CANDIDATE_COLUMNS["est_id"])
                if not col_est:
                    raise UploadError("Could not find an establishment code column (expected EST_ID).")
                col_ecr = _match_column(chunk.columns, ECR_CANDIDATE_COLUMNS["ecr_count"])
                col_mem = _match_column(chunk.columns, ECR_CANDIDATE_COLUMNS["employees"])
                col_amt = _match_column(chunk.columns, ECR_CANDIDATE_COLUMNS["contribution"])
                if not (col_ecr or col_mem or col_amt):
                    raise UploadError("Could not find ECR count / employees / contribution columns.")

            start_row = rows_read + 2
            batch = []
            seen_ids = set()
            for offset, row in enumerate(chunk.to_dict("records")):
                i = start_row + offset
                est_id = _clean(row.get(col_est))
                if not est_id:
                    errors.append(f"Row {i}: missing establishment code")
                    skipped += 1
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
                    skipped += 1
                    continue
                if ecr_count is None and employees is None and contribution is None:
                    errors.append(f"Row {i}: no ECR/employee/contribution data")
                    skipped += 1
                    continue

                seen_ids.add(est_id)
                batch.append({
                    "est_id": est_id, "year": calendar_year, "month": fy_month,
                    "ecr_count": ecr_count, "employees": employees, "contribution": contribution,
                })
            rows_read += len(chunk)

            if batch:
                with engine.begin() as conn:
                    already_there = existing_ecr_est_ids(conn, calendar_year, fy_month, seen_ids)
                    bulk_upsert_ecr_rows(conn, batch)
                for r in batch:
                    if r["est_id"] in already_there:
                        updated += 1
                    else:
                        inserted += 1

            if progress_cb:
                progress_cb(rows_read, total_rows)
    except UploadError:
        raise
    except Exception as e:
        raise UploadError(f"Could not parse CSV: {e}")

    with engine.begin() as conn:
        log_upload(
            conn, admin_user=admin_user, data_type="ecr", filename=filename,
            fy_year=fy_year, fy_month=fy_month, rows_read=rows_read,
            rows_inserted=inserted, rows_updated=updated, rows_skipped=skipped,
        )

    return {
        "rows_read": rows_read, "inserted": inserted, "updated": updated,
        "skipped": skipped, "errors": errors[:MAX_ERRORS_SHOWN],
        "errors_truncated": len(errors) > MAX_ERRORS_SHOWN,
    }
