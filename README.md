# EPFO ECR Viewer

Search any establishment (out of your ~50,000 master list) and see its ECR
remittance history as one table per financial year (**Apr to Mar**), with
months across the top and **ECR count / Employees / Contribution** stacked
as rows underneath. A month with no data is shown as a red "missed" cell so
gaps in remittance are visible at a glance.

## How it works

- All 7 of your yearly ECR CSVs are imported into **one** normalized table
  (`est_id, year, month, ecr_count, employees, contribution`) — the app
  doesn't care which of the 7 files a month came from.
- Each display "year" runs **April of year Y to March of year Y+1** (the
  standard EPFO financial year) — this is done in code (`bucket_year()`
  in `app.py`), grouping by calendar month/year regardless of which of
  your 7 source CSVs a given month came from.
- Re-running the import on the same or updated CSVs is safe — it's an
  upsert, so a later file always overwrites a month if it appears twice.

## 1. Run locally

```bash
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Import your master + all 7 ECR CSVs (any order, all in one command is fine)
python import_data.py --master master.csv --ecr ecr_2019_20.csv ecr_2020_21.csv ecr_2021_22.csv ecr_2022_23.csv ecr_2023_24.csv ecr_2024_25.csv ecr_2025_26.csv

# Run the app
python app.py
```

Open http://127.0.0.1:5000, type an establishment ID or name, pick it from
the dropdown, and the yearly tables load automatically.

By default this uses a local SQLite file `ecr_viewer.db`. That's fine for
local use.

## 2. Deploy to Render

Since Render's free-tier disk is wiped on every redeploy/restart (the same
issue you hit with `salary_app`), **use a Postgres database** (e.g. your
existing Neon project) rather than local SQLite for anything hosted:

1. Push this folder to a new GitHub repo (e.g. `EPFRAGHU/ecr-viewer`).
2. On Render: **New → Web Service** → connect the repo.
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app` (already in the `Procfile`, Render
     will pick it up automatically)
3. In Render's **Environment** tab, add:
   - `DATABASE_URL` = your Neon Postgres connection string
     (`postgresql://user:pass@host/dbname`)
4. Deploy. Then run the import **once** against that same `DATABASE_URL`
   (e.g. run `import_data.py` locally with `DATABASE_URL` set in your shell
   env, pointing at the same Neon DB — this is the same pattern as your
   other EPFO apps) so the hosted app has data without re-uploading CSVs
   through the browser.

   ```bash
   export DATABASE_URL="postgresql://user:pass@host/dbname"   # Windows: set DATABASE_URL=...
   python import_data.py --master master.csv --ecr ecr_2019_20.csv ...
   ```

5. Set the admin env vars below so you can update data from the browser
   instead of re-running `import_data.py` by hand every time.

## Admin upload page

`/admin/upload` lets you upload an updated Establishment Master CSV or a
single month's ECR CSV from the browser instead of running
`import_data.py` on the command line. It's protected by a single hardcoded
admin login (session-based) configured entirely via environment variables
— there's no user table:

```bash
SECRET_KEY=<any long random string>       # signs the login session cookie
ADMIN_USERNAME=<your admin username>
ADMIN_PASSWORD_HASH=<a werkzeug password hash, see below>
```

Generate the password hash once (don't put the plain password in an env var):

```bash
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('your-password-here'))"
```

Then go to `/admin/login`, sign in, and use `/admin/upload` to upload:
- **Establishment Master CSV** — upserted by establishment code (`EST_ID`), same columns as the CLI importer.
- **ECR CSV** — a *single month's* remittance file (one row per establishment: `EST_ID`, ECR count, employee count, contribution amount). You pick the Financial Year and Month it belongs to in the form, since the file itself doesn't say. This is different from the wide, multi-month CSVs `import_data.py` expects — see "Column format expected" below.

Every upload is logged (admin user, filename, timestamp, rows read/inserted/updated/skipped) and the last 10 show on the upload page. The main viewer page shows "Data as of &lt;date&gt; · v&lt;count&gt;" in the top right, based on that log.

## Files

| File | Purpose |
|---|---|
| `app.py` | Flask app: search API, establishment year-view API, main page, admin auth + upload routes |
| `db.py` | SQLAlchemy schema, works with SQLite (local) or Postgres (Render) |
| `import_data.py` | One-off / repeatable CLI CSV importer for master + wide-format ECR files |
| `csv_upload.py` | Row-by-row validating importer used by the `/admin/upload` page (per-row errors, insert/update counts, upload_log) |
| `templates/_base.html` | Shared header/layout for all pages |
| `templates/index.html` | Single-page UI: search box + year-wise tables |
| `templates/admin_login.html` | Admin login form |
| `templates/admin_upload.html` | Admin upload forms + stats + recent upload log |
| `sample_data/` | Example CSVs matching your real column format, for testing |

## Column format expected

**Master CSV (CLI importer and admin upload):** requires `EST_ID`; recognizes
`OFFICE_ID, EST_NAME, ADDRESS_LINE1, CITY, DISTRICT_NAME, PIN_CODE,
COVER_DATE, INDUSTRY, COVERAGE_SECTION, PRIMARY_EMAIL, ACC_TASK_ID, DSC,
ESN, F5A` (extra columns ignored) — matches the real EPFO MIS export
columns.

**ECR CSVs, CLI importer (`import_data.py`) — wide format, any subset of
months, any order, months can start in any month (Mar-Feb, Apr-Mar, etc.
since columns are matched by name, not position):**
```
OFFICE_ID, EST_ID, EST_NAME,
MAR_20_ECR, MAR_20_MEM, MAR_20_AMT,
FEB_20_ECR, FEB_20_MEM, FEB_20_AMT,
... (any MON_YY_ECR / MON_YY_MEM / MON_YY_AMT columns)
```
Blank cells = establishment didn't file that month (shown as "missed" in
red on screen).

**ECR CSV, admin upload page — single month, long format** (the Financial
Year and Month are picked in the form, not read from the file):
```
EST_ID, ECR_COUNT, EMPLOYEES, CONTRIBUTION
```
Column names are matched case-insensitively with a few common aliases
(e.g. `AMOUNT`/`AMT` for contribution, `MEMBERS`/`MEM` for employees) —
see `ECR_CANDIDATE_COLUMNS` in `csv_upload.py`.
