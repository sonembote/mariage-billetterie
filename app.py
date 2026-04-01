import os
import secrets
import sqlite3
import csv
from io import StringIO
from urllib.parse import quote_plus
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, g, redirect, render_template, request, url_for
try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "tickets.db"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

ADMIN_PASSWORD = os.getenv("WEDDING_ADMIN_PASSWORD", "admin123")
USING_POSTGRES = DATABASE_URL.startswith("postgresql://")

app = Flask(__name__)


def get_db():
    if "db" not in g:
        if USING_POSTGRES:
            if psycopg is None:
                raise RuntimeError("psycopg is required when DATABASE_URL is configured.")
            g.db = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        else:
            g.db = sqlite3.connect(DB_PATH)
            g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_error):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    if USING_POSTGRES:
        if psycopg is None:
            raise RuntimeError("psycopg is required when DATABASE_URL is configured.")
        db = psycopg.connect(DATABASE_URL)
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id BIGSERIAL PRIMARY KEY,
                guest_name TEXT NOT NULL,
                token TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'not_used',
                used_at TEXT
            )
            """
        )
        db.commit()
        db.close()
        return

    db = sqlite3.connect(DB_PATH)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guest_name TEXT NOT NULL,
            token TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'not_used',
            used_at TEXT
        )
        """
    )
    db.commit()
    db.close()


def sql(sqlite_query: str, postgres_query: str) -> str:
    return postgres_query if USING_POSTGRES else sqlite_query


def fetchall(sqlite_query: str, postgres_query: str, params=()):
    db = get_db()
    cur = db.cursor()
    cur.execute(sql(sqlite_query, postgres_query), params)
    rows = cur.fetchall()
    cur.close()
    return rows


def fetchone(sqlite_query: str, postgres_query: str, params=()):
    db = get_db()
    cur = db.cursor()
    cur.execute(sql(sqlite_query, postgres_query), params)
    row = cur.fetchone()
    cur.close()
    return row


def execute(sqlite_query: str, postgres_query: str, params=()):
    db = get_db()
    cur = db.cursor()
    cur.execute(sql(sqlite_query, postgres_query), params)
    cur.close()
    db.commit()


init_db()


def is_admin(request_obj):
    return request_obj.args.get("password") == ADMIN_PASSWORD


@app.route("/")
def home():
    return render_template("home.html")


@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password != ADMIN_PASSWORD:
            return render_template("admin_login.html", error="Mot de passe incorrect.")
        return redirect(url_for("dashboard", password=password))
    return render_template("admin_login.html", error=None)


@app.route("/dashboard")
def dashboard():
    if not is_admin(request):
        abort(403)
    rows = fetchall(
        "SELECT id, guest_name, token, status, used_at FROM tickets ORDER BY id DESC",
        "SELECT id, guest_name, token, status, used_at FROM tickets ORDER BY id DESC",
    )
    return render_template("dashboard.html", tickets=rows, password=request.args["password"])


@app.route("/create-ticket", methods=["POST"])
def create_ticket():
    password = request.form.get("password", "")
    if password != ADMIN_PASSWORD:
        abort(403)

    guest_name = request.form.get("guest_name", "").strip()
    if not guest_name:
        return redirect(url_for("dashboard", password=password))

    token = secrets.token_urlsafe(16)

    execute(
        "INSERT INTO tickets (guest_name, token) VALUES (?, ?)",
        "INSERT INTO tickets (guest_name, token) VALUES (%s, %s)",
        (guest_name, token),
    )
    return redirect(url_for("dashboard", password=password))


@app.route("/import-csv", methods=["POST"])
def import_csv():
    password = request.form.get("password", "")
    if password != ADMIN_PASSWORD:
        abort(403)

    file = request.files.get("csv_file")
    if file is None or not file.filename:
        return redirect(url_for("dashboard", password=password))

    content = file.stream.read().decode("utf-8-sig")
    reader = csv.reader(StringIO(content))
    rows = list(reader)
    if not rows:
        return redirect(url_for("dashboard", password=password))

    # Supports files with or without header.
    start_idx = 1 if rows[0] and rows[0][0].strip().lower() in {"guest_name", "name", "invite"} else 0

    for row in rows[start_idx:]:
        if not row:
            continue
        guest_name = row[0].strip()
        if not guest_name:
            continue
        token = secrets.token_urlsafe(16)
        execute(
            "INSERT INTO tickets (guest_name, token) VALUES (?, ?)",
            "INSERT INTO tickets (guest_name, token) VALUES (%s, %s)",
            (guest_name, token),
        )
    return redirect(url_for("dashboard", password=password))


@app.route("/qr/<token>")
def qr_image(token):
    row = fetchone(
        "SELECT token FROM tickets WHERE token = ?",
        "SELECT token FROM tickets WHERE token = %s",
        (token,),
    )
    if row is None:
        abort(404)

    checkin_url = request.url_root.rstrip("/") + url_for("checkin", token=token)
    qr_service = "https://api.qrserver.com/v1/create-qr-code/?size=300x300&data="
    return redirect(qr_service + quote_plus(checkin_url))


@app.route("/ticket/<token>")
def ticket_page(token):
    row = fetchone(
        "SELECT guest_name, token, status, used_at FROM tickets WHERE token = ?",
        "SELECT guest_name, token, status, used_at FROM tickets WHERE token = %s",
        (token,),
    )
    if row is None:
        abort(404)
    return render_template("ticket.html", ticket=row)


@app.route("/ticket/<token>/pdf")
def ticket_pdf(token):
    row = fetchone(
        "SELECT guest_name, token, status, used_at FROM tickets WHERE token = ?",
        "SELECT guest_name, token, status, used_at FROM tickets WHERE token = %s",
        (token,),
    )
    if row is None:
        abort(404)

    return render_template("ticket_print.html", ticket=row)


@app.route("/checkin/<token>", methods=["GET", "POST"])
def checkin(token):
    row = fetchone(
        "SELECT id, guest_name, status, used_at FROM tickets WHERE token = ?",
        "SELECT id, guest_name, status, used_at FROM tickets WHERE token = %s",
        (token,),
    )
    if row is None:
        abort(404)

    if request.method == "POST":
        if row["status"] == "used":
            return render_template("checkin_result.html", ticket=row, already_used=True)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        execute(
            "UPDATE tickets SET status = 'used', used_at = ? WHERE id = ?",
            "UPDATE tickets SET status = 'used', used_at = %s WHERE id = %s",
            (now, row["id"]),
        )
        updated = fetchone(
            "SELECT guest_name, status, used_at FROM tickets WHERE id = ?",
            "SELECT guest_name, status, used_at FROM tickets WHERE id = %s",
            (row["id"],),
        )
        return render_template("checkin_result.html", ticket=updated, already_used=False)

    return render_template("checkin.html", ticket=row)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
