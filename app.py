from flask import Flask, request, jsonify, render_template
from sqlalchemy import select, or_, and_

from db import engine, init_db, establishments, ecr_monthly

app = Flask(__name__)
init_db()

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


@app.route("/")
def index():
    return render_template("index.html")


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
            month_cells.append({
                "month": MONTH_LABEL[m],
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
        "email": est["email"],
        "task_id": est["task_id"],
        "dsc": est["dsc"],
        "esn": est["esn"],
        "form_5a": est["form_5a"],
        "years": result_years,
    })


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=True, port=port)
