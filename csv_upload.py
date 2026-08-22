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
from collections import defaultdict

import pandas as pd

from db import (
    engine, log_upload,
    bulk_upsert_establishments, bulk_upsert_ecr_rows,
    existing_est_ids, existing_ecr_est_ids,
)
from import_data import _clean, _to_num, MONTH_MAP, COL_PATTERN, yy_to_year

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_ERRORS_SHOWN = 50
BATCH_SIZE = 1000

ECR_CANDIDATE_COLUMNS = {
    "est_id": ["EST_ID", "ESTABLISHMENT_ID", "ESTABLISHMENT_CODE"],
    "ecr_count": ["ECR_COUNT", "ECR", "NO_OF_ECR"],
    "employees": ["EMPLOYEES", "MEMBERS", "MEM", "EMPLOYEE_COUNT", "NO_OF_MEMBERS"],
    "contribution": ["CONTRIBUTION", "AMOUNT", "AMT", "CONTRIBUTION_AMOUNT", "TOTAL_AMOUNT"],
}

MONTH_LABEL = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
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


def process_ecr_csv(path, filename, admin_user, fy_year=None, fy_month=None, calendar_year=None, progress_cb=None):
    """Accepts two CSV shapes:

    - "Wide" annual format (what the MIS portal actually exports): one row per
      establishment, with a MON_YY_ECR / MON_YY_MEM / MON_YY_AMT triple of
      columns per month (e.g. MAR_20_ECR, MAR_20_MEM, MAR_20_AMT, FEB_20_ECR,
      ...). Year and month are read straight from each column's own name, so
      fy_year/fy_month/calendar_year aren't needed and every month present in
      the file is imported in one upload.
    - Legacy "narrow" single-month format (one ECR_COUNT/EMPLOYEES/CONTRIBUTION-
      style column per file) - used only when no MON_YY_* columns are found;
      requires fy_year/fy_month/calendar_year (picked on the upload form) to
      know which month the file's single column set belongs to.
    """
    total_rows = _count_data_rows(path)
    rows_read = 0
    inserted = updated = skipped = 0
    errors = []
    months_seen = set()

    try:
        chunks = pd.read_csv(path, dtype=str, chunksize=BATCH_SIZE)
        col_est = None
        month_cols = None  # {(year, month): {"ECR": col, "MEM": col, "AMT": col}}

        for chunk_idx, chunk in enumerate(chunks):
            chunk.columns = [c.strip().upper() for c in chunk.columns]

            if chunk_idx == 0:
                col_est = _match_column(chunk.columns, ECR_CANDIDATE_COLUMNS["est_id"])
                if not col_est:
                    raise UploadError("Could not find an establishment code column (expected EST_ID).")

                month_cols = {}
                for col in chunk.columns:
                    m = COL_PATTERN.match(col)
                    if not m:
                        continue
                    mon = m.group("mon").upper()
                    if mon not in MONTH_MAP:
                        continue
                    key = (yy_to_year(m.group("yy")), MONTH_MAP[mon])
                    month_cols.setdefault(key, {})[m.group("field").upper()] = col

                if not month_cols:
                    col_ecr = _match_column(chunk.columns, ECR_CANDIDATE_COLUMNS["ecr_count"])
                    col_mem = _match_column(chunk.columns, ECR_CANDIDATE_COLUMNS["employees"])
                    col_amt = _match_column(chunk.columns, ECR_CANDIDATE_COLUMNS["contribution"])
                    if not (col_ecr or col_mem or col_amt):
                        raise UploadError(
                            "Could not find ECR count / employees / contribution columns, and no "
                            "MON_YY_ECR/MEM/AMT style columns (e.g. MAR_20_ECR) were found either."
                        )
                    if not (fy_year and fy_month and calendar_year):
                        raise UploadError(
                            "This file has a single month's columns - please also select the "
                            "Financial Year and Month for it."
                        )
                    month_cols = {(calendar_year, fy_month): {"ECR": col_ecr, "MEM": col_mem, "AMT": col_amt}}

            start_row = rows_read + 2
            batch = []
            for offset, row in enumerate(chunk.to_dict("records")):
                i = start_row + offset
                est_id = _clean(row.get(col_est))
                if not est_id:
                    errors.append(f"Row {i}: missing establishment code")
                    skipped += 1
                    continue
                est_id = est_id.upper()

                for (year, month), fields in month_cols.items():
                    label = f"{MONTH_LABEL.get(month, month)} {year}"
                    row_bad = False
                    ecr_count = employees = contribution = None
                    if fields.get("ECR"):
                        raw = row.get(fields["ECR"])
                        ecr_count = _to_num(raw)
                        if ecr_count is None and _clean(raw) not in (None, "-"):
                            errors.append(f"Row {i} ({label}): invalid ECR count")
                            row_bad = True
                    if fields.get("MEM"):
                        raw = row.get(fields["MEM"])
                        employees = _to_num(raw)
                        if employees is None and _clean(raw) not in (None, "-"):
                            errors.append(f"Row {i} ({label}): invalid employee count")
                            row_bad = True
                    if fields.get("AMT"):
                        raw = row.get(fields["AMT"])
                        contribution = _to_num(raw)
                        if contribution is None and _clean(raw) not in (None, "-"):
                            errors.append(f"Row {i} ({label}): invalid contribution amount")
                            row_bad = True
                    if row_bad:
                        skipped += 1
                        continue
                    if ecr_count is None and employees is None and contribution is None:
                        # Establishment simply has no data this month - not an error.
                        continue

                    months_seen.add((year, month))
                    batch.append({
                        "est_id": est_id, "year": year, "month": month,
                        "ecr_count": ecr_count, "employees": employees, "contribution": contribution,
                    })
            rows_read += len(chunk)

            if batch:
                groups = defaultdict(list)
                for r in batch:
                    groups[(r["year"], r["month"])].append(r["est_id"])
                with engine.begin() as conn:
                    already_there = set()
                    for (yr, mo), ids in groups.items():
                        for eid in existing_ecr_est_ids(conn, yr, mo, set(ids)):
                            already_there.add((eid, yr, mo))
                    bulk_upsert_ecr_rows(conn, batch)
                for r in batch:
                    if (r["est_id"], r["year"], r["month"]) in already_there:
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

    months_label = ", ".join(f"{MONTH_LABEL.get(m, m)} {y}" for y, m in sorted(months_seen))

    return {
        "rows_read": rows_read, "inserted": inserted, "updated": updated,
        "skipped": skipped, "errors": errors[:MAX_ERRORS_SHOWN],
        "errors_truncated": len(errors) > MAX_ERRORS_SHOWN,
        "months_processed": months_label,
    }
