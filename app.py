import os
import tempfile
import threading
from datetime import date
from functools import wraps

from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from sqlalchemy import select, or_, and_
from werkzeug.security import check_password_hash

import csv_upload
import jobs
from db import (
    engine, init_db, establishments, ecr_monthly,
    get_data_stats, get_recent_uploads, get_overall_upload_status,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
init_db()

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH")

UPLOAD_TMP_DIR = os.path.join(tempfile.gettempdir(), "ecr_viewer_uploads")

# Display order: Apr, May, ... Dec, Jan, Feb, Mar
DISPLAY_MONTHS = [4, 5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3]
MONTH_LABEL = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}


def bucket_year(calendar_year: int, calendar_month: int) -> int:
    """A 'display year' Y runs from Apr-Y to Mar-(Y+1) (standard EPFO
    financial year). Jan/Feb/Mar of a calendar year belong to the display
    year that started the previous April."""
    if calendar_month in (1, 2, 3):
        return calendar_year - 1
    return calendar_year


def calendar_year_for(fy_year: int, calendar_month: int) -> int:
    """Inverse of bucket_year: the calendar year a given month falls in
    within display/financial year `fy_year` (Apr fy_year - Mar fy_year+1)."""
    if calendar_month in (1, 2, 3):
        return fy_year + 1
    return fy_year


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.route("/")
def index():
    status = get_overall_upload_status()
    header_info = None
    if status["last_updated"]:
        header_info = {
            "date": status["last_updated"].strftime("%d %b %Y"),
            "version": status["version"],
        }
    return render_template("index.html", header_info=header_info)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if (ADMIN_USERNAME and ADMIN_PASSWORD_HASH
                and username == ADMIN_USERNAME
                and check_password_hash(ADMIN_PASSWORD_HASH, password)):
            session["is_admin"] = True
            session["admin_user"] = username
            return redirect(request.args.get("next") or url_for("admin_upload_page"))
        error = "Invalid username or password."
    return render_template("admin_login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


def _render_admin_upload(summary=None):
    stats = get_data_stats()
    recent = get_recent_uploads(10)
    today = date.today()
    current_fy_start = bucket_year(today.year, today.month)
    fy_years = list(range(current_fy_start - 9, current_fy_start + 1))[::-1]
    return render_template(
        "admin_upload.html",
        stats=stats, recent=recent, summary=summary, fy_years=fy_years,
        display_months=[(m, MONTH_LABEL[m]) for m in DISPLAY_MONTHS],
        admin_user=session.get("admin_user"), month_label=MONTH_LABEL,
    )


@app.route("/admin/upload", methods=["GET"])
@admin_required
def admin_upload_page():
    summary = None
    job_id = request.args.get("job")
    if job_id:
        job = jobs.get_job(job_id)
        if job and job["status"] == "done":
            summary = job["summary"]
        elif job and job["status"] == "error":
            summary = {"type": job["type"], "error": job["error"]}
    return _render_admin_upload(summary=summary)


def _run_master_job(job_id, path, filename, admin_user):
    def progress_cb(done, total):
        jobs.update_job(job_id, processed=done, total=total)
    try:
        summary = csv_upload.process_master_csv(path, filename, admin_user, progress_cb=progress_cb)
        summary["type"] = "master"
        jobs.update_job(job_id, status="done", summary=summary)
    except csv_upload.UploadError as e:
        jobs.update_job(job_id, status="error", error=str(e))
    except Exception as e:
        app.logger.exception("Unexpected error processing master CSV upload")
        jobs.update_job(job_id, status="error", error=f"Unexpected error while processing the file: {e}")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _run_ecr_job(job_id, path, filename, admin_user, fy_year, fy_month, calendar_year):
    def progress_cb(done, total):
        jobs.update_job(job_id, processed=done, total=total)
    try:
        summary = csv_upload.process_ecr_csv(
            path, filename, admin_user, fy_year, fy_month, calendar_year, progress_cb=progress_cb
        )
        summary["type"] = "ecr"
        if fy_year and fy_month:
            summary["fy_label"] = f"{fy_year}-{str(fy_year + 1)[-2:]}"
            summary["month_label"] = MONTH_LABEL.get(fy_month, "?")
        else:
            summary["fy_label"] = "Multiple months"
            summary["month_label"] = summary.get("months_processed") or "(auto-detected from file)"
        jobs.update_job(job_id, status="done", summary=summary)
    except csv_upload.UploadError as e:
        jobs.update_job(job_id, status="error", error=str(e))
    except Exception as e:
        app.logger.exception("Unexpected error processing ECR CSV upload")
        jobs.update_job(job_id, status="error", error=f"Unexpected error while processing the file: {e}")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


@app.route("/admin/upload/master", methods=["POST"])
@admin_required
def admin_upload_master():
    file = request.files.get("file")
    try:
        path = csv_upload.save_upload(file, UPLOAD_TMP_DIR)
    except csv_upload.UploadError as e:
        return jsonify({"error": str(e)}), 400

    job_id = jobs.new_job("master")
    threading.Thread(
        target=_run_master_job,
        args=(job_id, path, file.filename, session.get("admin_user", "admin")),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


@app.route("/admin/upload/ecr", methods=["POST"])
@admin_required
def admin_upload_ecr():
    file = request.files.get("file")
    fy_year = fy_month = calendar_year = None
    raw_fy_year = (request.form.get("fy_year") or "").strip()
    raw_fy_month = (request.form.get("fy_month") or "").strip()
    if raw_fy_year or raw_fy_month:
        try:
            fy_year = int(raw_fy_year)
            fy_month = int(raw_fy_month)
        except (TypeError, ValueError):
            return jsonify({"error": "Please select a valid Financial Year and Month, or leave both blank for a multi-month file."}), 400
        calendar_year = calendar_year_for(fy_year, fy_month)
    try:
        path = csv_upload.save_upload(file, UPLOAD_TMP_DIR)
    except csv_upload.UploadError as e:
        return jsonify({"error": str(e)}), 400

    job_id = jobs.new_job("ecr")
    threading.Thread(
        target=_run_ecr_job,
        args=(job_id, path, file.filename, session.get("admin_user", "admin"), fy_year, fy_month, calendar_year),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


@app.route("/admin/upload/status/<job_id>")
@admin_required
def admin_upload_status(job_id):
    job = jobs.get_job(job_id)
    if not job:
        return jsonify({"status": "not_found"}), 404
    return jsonify(job)


@app.route("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])
    like = f"%{q}%"
    stmt = (
        select(establishments.c.est_id, establishments.c.est_name, establishments.c.office_id)
        .where(or_(establishments.c.est_id.ilike(like), establishments.c.est_name.ilike(like)))
        .limit(20)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return jsonify([dict(r) for r in rows])


@app.route("/api/establishment/<est_id>")
def api_establishment(est_id):
    est_id = est_id.strip().upper()
    with engine.connect() as conn:
        est = conn.execute(
            select(establishments).where(establishments.c.est_id == est_id)
        ).mappings().first()
        if not est:
            return jsonify({"error": "not found"}), 404

        rows = conn.execute(
            select(ecr_monthly).where(ecr_monthly.c.est_id == est_id)
        ).mappings().all()

    # Bucket rows into display years -> month -> data
    years = {}
    for r in rows:
        dy = bucket_year(r["year"], r["month"])
        years.setdefault(dy, {})[r["month"]] = {
            "ecr_count": r["ecr_count"],
            "employees": r["employees"],
            "contribution": float(r["contribution"]) if r["contribution"] is not None else None,
        }

    # Sort years descending (most recent display-year first, per user request:
    # 2026 row above 2025 above 2024, etc.)
    result_years = []
    for dy in sorted(years.keys(), reverse=True):
        months_data = years[dy]
        month_cells = []
        for m in DISPLAY_MONTHS:
            cell = months_data.get(m)
            calendar_year = dy + 1 if m in (1, 2, 3) else dy
            month_cells.append({
                "month": f"{MONTH_LABEL[m].upper()}-{calendar_year}",
                "filed": cell is not None,
                "ecr_count": cell["ecr_count"] if cell else None,
                "employees": cell["employees"] if cell else None,
                "contribution": cell["contribution"] if cell else None,
            })
        result_years.append({
            "label": f"{dy}-{str(dy + 1)[-2:]}",
            "months": month_cells,
        })

    return jsonify({
        "est_id": est["est_id"],
        "est_name": est["est_name"],
        "office_id": est["office_id"],
        "address1": est["address1"],
        "city": est["city"],
        "district": est["district"],
        "pin": est["pin"],
        "cover_date": est["cover_date"],
        "industry": est["industry"],
        "coverage_section": est["coverage_section"],
        "email": est["email"],
        "task_id": est["task_id"],
        "dsc": est["dsc"],
        "esn": est["esn"],
        "form_5a": est["form_5a"],
        "years": result_years,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=True, port=port)
