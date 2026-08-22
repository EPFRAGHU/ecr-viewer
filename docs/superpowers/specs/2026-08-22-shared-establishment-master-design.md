# Shared Establishment Master: Admin Upload for est_master + ecr-viewer

Date: 2026-08-22
Status: Approved for planning

## 1. Overview

Today, two independent Flask apps each know about EPFO establishments:

- **ecr-viewer** (`E:\Users\Dell\Downloads\ecr-viewer(5)\ecr-viewer`) — has a working admin
  upload page (`/admin/upload`) that background-processes a CSV into a Postgres
  `establishments` table, with a progress bar and an `upload_log` audit trail. Its
  establishment data is a small, purpose-built subset (15 columns) used to support ECR
  lookups.
- **est_master** (`E:\Users\Dell\Downloads\est_master`) — a search/browse UI over the full raw
  EPFO MIS "establishment master" export (~38 columns: LIN code, PAN/CIN, both address
  lines, DSC/eSign/Form-5A flags, Aadhaar/bank/PAN seeding flags, etc.). Today it loads this
  from a CSV file committed to git and manually replaced/redeployed. It has no admin upload,
  no auth, no database.

The goal: give est_master its own admin upload page — same background batch-processing +
progress bar + audit log pattern as ecr-viewer — and have both apps read/write **the same**
Postgres `establishments` table, so uploading a fresh MIS export from either app's admin page
updates data visible in both.

## 2. Non-goals

- No shared Python package/library between the two repos — they stay independent git repos;
  some duplication of `db.py`/`csv_upload.py` logic between them is accepted.
- No automatic cross-app cache invalidation. If a file is uploaded via ecr-viewer, est_master's
  in-memory DataFrame won't reflect it until est_master's own next upload, restart, or a manual
  refresh (see §9).
- No change to est_master's existing search/filter/sort/pagination/detail UI or logic — only
  its data *source* changes.
- No automated test suite is introduced (neither app currently has one); verification is manual
  (local run + browser check), consistent with how prior changes in both repos have been
  verified.
- Render's Postgres free-tier 30-day expiry is a known operational risk, not something this
  design solves.

## 3. Shared data model

Widen ecr-viewer's existing `establishments` table (`db.py`) with the ~24 columns est_master
needs, using its existing `_add_missing_columns()` auto-migration (adds nullable columns to an
existing table without a migration framework — already used for exactly this purpose per its
git history). Storage column names stay `snake_case`; all are nullable `String`/`Text` (this
project treats all CSV fields as strings — no numeric coercion, matching both apps' existing
`dtype=str` convention).

| DB column (existing) | Raw MIS CSV header(s) accepted | Notes |
|---|---|---|
| `est_id` (PK) | `EST_ID` | unchanged |
| `office_id` | `OFFICE_ID` | unchanged |
| `est_name` | `EST_NAME` | unchanged |
| `address1` | `ADDRESS_LINE1`, `INCROP_ADDRESS1` | widen accepted input names |
| `city` | `CITY`, `INCROP_CITY` | widen accepted input names |
| `district` | `DISTRICT_NAME`, `INCROP_DIST` | widen accepted input names |
| `pin` | `PIN_CODE`, `INCROP_PIN` | widen accepted input names |
| `cover_date` | `COVER_DATE` | unchanged |
| `industry` | `INDUSTRY`, `IND_GROUP_NAME` | widen accepted input names |
| `coverage_section` | `COVERAGE_SECTION`, `COVER_SECTION_NAME` | widen accepted input names |
| `email` | `PRIMARY_EMAIL` | unchanged |
| `task_id` | `ACC_TASK_ID` | unchanged |
| `dsc` | `DSC` | unchanged |
| `esn` | `ESN` | unchanged |
| `form_5a` | `F5A` | unchanged |

| DB column (new) | Raw MIS CSV header | Notes |
|---|---|---|
| `lin_code` | `LIN_CODE` | |
| `est_cin` | `EST_CIN` | |
| `pan` | `PAN` | |
| `address2` | `INCROP_ADDRESS2` | |
| `exemption_status` | `EXEMPTION_STATUS_NAME` | |
| `est_status` | `EST_STATUS_NAME` | |
| `est_type` | `EST_TYPE_NAME` | |
| `actionable_status` | `ACTIONABLE_STATUS_NAME` | |
| `cont_rate` | `CONT_RATE_NAME` | |
| `acc_year` | `ACC_YEAR_NAME` | |
| `ind_code` | `IND_CODE_NAME` | |
| `ins_group_id` | `INS_GROUP_ID` | |
| `ins_task_id` | `INS_TASK_ID` | |
| `enf_group_id` | `ENF_GROUP_ID` | |
| `enf_task_id` | `ENF_TASK_ID` | |
| `acc_grp_id` | `ACC_GRP_ID` | |
| `uans` | `UANS` | |
| `er_portal_registered` | `REGISTERED_ON_ER_PORTAL` | |
| `accts` | `ACCTS` | |
| `aadhaar_seeded` | `AADHAAR_SEEDED` | |
| `aadhaar_verified` | `AADHAAR_VERIFIED` | |
| `bank_seeded` | `BANK_SEEDED` | |
| `pan_seeded` | `PAN_SEEDED` | |
| `mobile_seeded` | `MOBILE_SEEDED` | |

This is purely additive to ecr-viewer's schema — no existing column is renamed or removed, so
ecr-viewer's current establishment-related behavior (whatever consumes `address1`, `city`,
etc. today) is unaffected. As a pre-implementation check, grep ecr-viewer's `app.py`/templates
for consumers of the `establishments` table to confirm nothing assumes a fixed column count.

## 4. ecr-viewer changes

- **`db.py`**: add the 24 new `Column(..., nullable=True)` definitions to the `establishments`
  `Table`. `_add_missing_columns()` picks them up automatically on next `init_db()` call — no
  manual migration needed.
- **`db.py` → `bulk_upsert_establishments`**: extend the upserted column list to include the
  new fields, keeping the existing `COALESCE(excluded.col, table.col)` per-column merge
  semantics (new non-blank value wins; blank/missing doesn't clobber an existing value).
- **`csv_upload.py` → `process_master_csv`**: replace the current single-name column lookups
  (`row.get("ADDRESS_LINE1")` etc.) with the same `_match_column(candidates)` alias-matching
  pattern already used for the ECR importer, using the "accepted" header lists from §3's table,
  plus a straight 1:1 mapping for the 24 new columns (only one accepted header each, the raw
  MIS name). This keeps old-format CSVs (its current expected format) working unchanged while
  also accepting the raw MIS export est_master uses.

## 5. est_master changes

New files (closely mirroring ecr-viewer's equivalents, adapted):

- **`db.py`** — SQLAlchemy Core engine + the *same* `establishments` `Table` definition as
  ecr-viewer (all ~39 columns), pointed at `DATABASE_URL`. Falls back to a local
  `sqlite:///est_master.db` when `DATABASE_URL` is unset, exactly like ecr-viewer — this keeps
  solo local development possible without a reachable Postgres instance; the "shared data"
  behavior only actually applies once both apps' `DATABASE_URL` point at the same Postgres
  instance in production. Includes `bulk_upsert_establishments`, `existing_est_ids`,
  `log_upload`, `get_recent_uploads`, and an `establishments_to_dataframe()` helper that runs
  `SELECT * FROM establishments` and returns a DataFrame with columns renamed back to the raw
  MIS names (`address1` → `INCROP_ADDRESS1`, etc.) so the rest of `app.py` is untouched.
- **`jobs.py`** — direct port of ecr-viewer's in-memory job store (`threading.Lock`-guarded
  dict, self-pruning after 30 minutes).
- **`csv_upload.py`** — `process_master_csv(path, filename, admin_user, progress_cb)`: same
  chunked-read (`pandas.read_csv(..., chunksize=1000)`), same per-chunk transaction + upsert +
  `progress_cb(rows_done, total)` pattern as ecr-viewer, but validates/maps the *full* raw MIS
  column set from §3 rather than the 15-column subset. Same row-skip-and-report behavior for
  missing `EST_ID`; caps shown errors at 50.
- **`auth.py`** (or inline in `app.py`) — `admin_required` decorator gating `/admin/*` routes
  via `session["is_admin"]`; `/admin/login` (GET/POST) and `/admin/logout` routes, checking
  `ADMIN_USERNAME` / `ADMIN_PASSWORD_HASH` from `.env` with `werkzeug.security.check_password_hash`.
- **`templates/admin_login.html`**, **`templates/admin_upload.html`** — same visual language as
  est_master's existing `index.html` (same CSS variables/palette), single upload panel (no
  FY/month selects needed — this is master-data-only), progress bar, summary box, "Recent
  Uploads" table from `get_recent_uploads(limit=10)`.
- **`app.py` changes**:
  - `load_dotenv()` at startup; `SECRET_KEY` for session signing.
  - Replace the startup `DF = load_master_csv(DATA_FILE)` with
    `DF = db.establishments_to_dataframe()`, with the same downstream "ensure expected columns
    exist" fallback logic kept as a safety net.
  - New routes: `GET /admin/upload` (page + stats + recent uploads), `POST /admin/upload/master`
    (save file via `save_upload()`, spawn `threading.Thread` background job, return `job_id`),
    `GET /admin/upload/status/<job_id>` (poll endpoint).
  - After a job completes successfully, reload `DF` in-process
    (`DF = db.establishments_to_dataframe()`) so the running app reflects the upload immediately
    without a restart.
  - Add an "Admin" link in the page header, matching ecr-viewer's "Home links" pattern.
- **`requirements.txt`**: add `SQLAlchemy`, `psycopg2-binary`, `python-dotenv` (matching
  ecr-viewer's pinned versions where practical).
- **`.env.example`**: document `DATABASE_URL`, `SECRET_KEY`, `ADMIN_USERNAME`,
  `ADMIN_PASSWORD_HASH`, with the same password-hash generation snippet ecr-viewer's
  `.env.example` uses.
- **`README.md`**: update the "Updating the data" section to describe the new admin upload flow
  as the primary path, keeping the manual CSV-replace-and-push instructions as a fallback.

## 6. Upload semantics (recap)

- Full file per upload; **upsert by `EST_ID`** — new establishments are inserted, existing ones
  are merged column-by-column (new non-blank value replaces old; blank doesn't clobber),
  establishments absent from the new file are left untouched.
- 50MB upload limit, `.csv` extension required, 1000-row batches, one DB transaction per batch.
- Per-row problems (missing `EST_ID`) are skipped and logged in the job summary (capped at 50
  shown); file-level problems (missing `EST_ID` column entirely, unparseable CSV) abort the
  whole upload before any writes.
- Every completed upload writes one row to the shared `upload_log` table (`admin_user`,
  `data_type="master"`, `filename`, row counts, timestamp) — reusing ecr-viewer's existing log
  table and its "Recent Uploads" query, so uploads from either app show up together in a shared
  history if desired (each app's own admin page still only queries/display its own recent
  uploads unless asked otherwise — no cross-app UI requirement stated).

## 7. Configuration

Both apps need `DATABASE_URL` set to the **same** Postgres instance in production (Render). I
cannot read or set Render dashboard secrets myself — the user will need to copy ecr-viewer's
`DATABASE_URL` value into est_master's Render environment variables, and into a local `.env` for
local testing against the same shared instance if desired.

est_master's `.env` additions:
```
DATABASE_URL=<same value as ecr-viewer's Render Postgres, e.g. postgres://...>
SECRET_KEY=<random secret>
ADMIN_USERNAME=<chosen admin username>
ADMIN_PASSWORD_HASH=<generated via werkzeug generate_password_hash>
```

## 8. Deployment

- Both Render web services keep `--workers 1` (already the case in both `Procfile`s) — required
  because both the in-memory job store and the in-memory DataFrame cache are process-local.
- Render's free Postgres tier expires after 30 days unless upgraded — flagged as an existing
  operational risk on ecr-viewer already; now est_master inherits the same dependency.
- Rollout order: (1) ship ecr-viewer's schema-widening + importer changes and confirm its
  existing upload still works unchanged, (2) get the shared `DATABASE_URL` value into
  est_master's config, (3) ship est_master's new admin upload feature, (4) verify end-to-end:
  upload via est_master, confirm rows land in Postgres, confirm est_master's own UI reflects
  them immediately, and confirm ecr-viewer sees them after its own next restart/reload.

## 9. Known limitation: cross-app cache staleness

Each app caches the full establishments table in memory (a pandas DataFrame) for query
performance and to avoid rewriting either app's existing query logic. An upload via app A
updates the shared DB immediately, but app B's in-memory cache only refreshes on app B's next
upload, restart, or (if added later) a manual "refresh from DB" trigger. This is called out as
an accepted limitation, not solved here — a future enhancement could add a lightweight
`/admin/refresh` action or short TTL-based reload, but that's out of scope (YAGNI) until it's
actually a problem in practice.

## 10. Error handling & edge cases

- CSV missing `EST_ID` column entirely → whole upload aborted, no rows written, job status
  `error` with message shown in UI.
- Individual row missing `EST_ID` → row skipped, counted, reason logged in job summary (capped
  at 50 shown, `errors_truncated` flag if more).
- File too large (>50MB) or wrong extension → rejected before any background processing starts.
- Concurrent uploads from both apps hitting the same establishment: normal Postgres
  read-committed transaction semantics apply per batch; last committed batch wins for any
  column both uploads touch. No additional locking is introduced — acceptable given this is a
  low-concurrency, single-admin-at-a-time workflow.
- Unexpected exceptions during background processing are logged via `app.logger.exception(...)`
  and surfaced to the UI only as a generic error string, matching ecr-viewer's existing
  behavior.

## 11. Testing plan (manual, matching existing project conventions)

1. ecr-viewer: after schema/importer changes, run locally, log in as admin, upload a small
   sample CSV in both the old simplified format and the new raw-MIS-header format, confirm both
   are accepted and land correctly, confirm existing ECR upload still works.
2. est_master: run locally against a local SQLite fallback first (no `DATABASE_URL` set) to
   verify the upload/job/progress-bar/log mechanics work in isolation.
3. est_master: point at a real (or shared) Postgres instance, upload the real
   `establishment_master.csv`, confirm the home page search/filter/sort/detail view all still
   work identically to today (this is the regression check — the UI code doesn't change, only
   the data source).
4. Cross-app check: upload via est_master, then confirm (after a restart or next load) that
   ecr-viewer's own admin "Recent Uploads" / establishment lookups reflect the new data, and
   vice versa.
