"""
Import script for the ECR Viewer.

Usage:
    python import_data.py --master master.csv --ecr ecr_2019_20.csv ecr_2020_21.csv ...

- --master   : establishment master CSV. Expected columns (case-insensitive):
               OFFICE_ID, EST_ID, EST_NAME  (extra columns are ignored)

- --ecr      : one or more wide-format ECR CSVs. Expected columns:
               OFFICE_ID, EST_ID, EST_NAME,
               <MON>_<YY>_ECR, <MON>_<YY>_MEM, <MON>_<YY>_AMT   (repeated per month)
               e.g. MAR_20_ECR, MAR_20_MEM, MAR_20_AMT, FEB_20_ECR, ...
               Column order doesn't matter and you can pass all 7 yearly
               files in one command - months are matched by name, not position.

You can re-run this any time you get a new/updated CSV - it's safe to
import the same file twice (upsert), and a later file always overwrites
a month if it appears in more than one CSV.
"""
import argparse
import re
import sys
import pandas as pd

from db import engine, init_db, upsert_establishment, upsert_ecr_row

MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# Matches column names like MAR_20_ECR / mar_20_mem / Feb_20_Amt
COL_PATTERN = re.compile(
    r"^(?P<mon>[A-Za-z]{3})_(?P<yy>\d{2,4})_(?P<field>ECR|MEM|AMT)$", re.IGNORECASE
)


def yy_to_year(yy: str) -> int:
    """Convert a 2-digit (or 4-digit) year suffix to a full calendar year.
    Assumes 20xx - adjust here if you ever have data from the 1900s CSVs."""
    yy = int(yy)
    if yy >= 100:
        return yy
    return 2000 + yy


def _clean(val):
    """Return a stripped string, or None for blank/NaN cells."""
    if val is None:
        return None
    s = str(val).strip()
    if s == "" or s.lower() == "nan":
        return None
    return s


def load_master(path):
    df = pd.read_csv(path, dtype=str)
    df.columns = [c.strip().upper() for c in df.columns]
    required = {"OFFICE_ID", "EST_ID", "EST_NAME"}
    missing = required - set(df.columns)
    if missing:
        sys.exit(f"Master CSV is missing required columns: {missing}")

    init_db()
    with engine.begin() as conn:
        for _, row in df.iterrows():
            est_id = str(row["EST_ID"]).strip().upper()
            if not est_id or est_id.lower() == "nan":
                continue
            upsert_establishment(
                conn,
                est_id=est_id,
                office_id=_clean(row.get("OFFICE_ID")),
                est_name=_clean(row.get("EST_NAME")),
                address1=_clean(row.get("ADDRESS_LINE1")),
                city=_clean(row.get("CITY")),
                district=_clean(row.get("DISTRICT_NAME")),
                pin=_clean(row.get("PIN_CODE")),
                email=_clean(row.get("PRIMARY_EMAIL")),
                task_id=_clean(row.get("ACC_TASK_ID")),
                dsc=_clean(row.get("DSC")),
                esn=_clean(row.get("ESN")),
                form_5a=_clean(row.get("F5A")),
            )
    print(f"Master: upserted {len(df)} establishments from {path}")


def _to_num(val):
    """Parse a CSV cell into a number, treating blank/NaN/'-' as None (no ECR filed)."""
    if val is None:
        return None
    s = str(val).strip()
    if s == "" or s.lower() == "nan" or s == "-":
        return None
    s = s.replace(",", "")
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return None


def load_ecr_file(path):
    df = pd.read_csv(path, dtype=str)
    df.columns = [c.strip().upper() for c in df.columns]
    if "EST_ID" not in df.columns:
        sys.exit(f"{path}: missing EST_ID column")

    # Group the wide columns by (month, year) -> {ecr: col, mem: col, amt: col}
    month_cols = {}
    for col in df.columns:
        m = COL_PATTERN.match(col)
        if not m:
            continue
        mon = m.group("mon").upper()
        if mon not in MONTH_MAP:
            continue
        year = yy_to_year(m.group("yy"))
        month = MONTH_MAP[mon]
        key = (year, month)
        month_cols.setdefault(key, {})[m.group("field").upper()] = col

    if not month_cols:
        sys.exit(f"{path}: no MON_YY_ECR/MEM/AMT style columns found - check the header")

    rows_written = 0
    with engine.begin() as conn:
        # Make sure every establishment in this file exists (in case it's
        # missing from the master, e.g. transferred/new establishments)
        for _, row in df.iterrows():
            est_id = str(row["EST_ID"]).strip().upper()
            if not est_id or est_id.lower() == "nan":
                continue
            upsert_establishment(
                conn,
                est_id=est_id,
                office_id=str(row.get("OFFICE_ID", "")).strip(),
                est_name=str(row.get("EST_NAME", "")).strip(),
            )
            for (year, month), fields in month_cols.items():
                ecr_count = _to_num(row.get(fields.get("ECR")))
                employees = _to_num(row.get(fields.get("MEM")))
                contribution = _to_num(row.get(fields.get("AMT")))
                # Skip completely empty months (establishment simply wasn't
                # due / didn't file - leave no row rather than a row of blanks)
                if ecr_count is None and employees is None and contribution is None:
                    continue
                upsert_ecr_row(conn, est_id, year, month, ecr_count, employees, contribution)
                rows_written += 1
    print(f"ECR file {path}: {len(df)} establishments, "
          f"{len(month_cols)} month-columns detected, {rows_written} monthly records written")


def main():
    parser = argparse.ArgumentParser(description="Import EPFO establishment master + ECR CSVs")
    parser.add_argument("--master", help="Path to establishment master CSV")
    parser.add_argument("--ecr", nargs="*", default=[], help="Path(s) to ECR CSV files")
    args = parser.parse_args()

    init_db()

    if args.master:
        load_master(args.master)

    for path in args.ecr:
        load_ecr_file(path)

    if not args.master and not args.ecr:
        parser.print_help()


if __name__ == "__main__":
    main()
