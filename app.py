import os
import re
import secrets
import sqlite3
from datetime import date, datetime, time, timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    Response,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

BASE_DIR = Path(__file__).resolve().parent


def load_dotenv(path: Path):
    """Tiny .env loader so local setup needs no extra package."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_dotenv(BASE_DIR / ".env")

DB_PATH = Path(os.environ.get("DATABASE_PATH", BASE_DIR / "walks.db"))
APP_NAME = os.environ.get("APP_NAME", "Walks with Jack")
TIMEZONE_NAME = os.environ.get("TIMEZONE", "America/Toronto")
TZ = ZoneInfo(TIMEZONE_NAME)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me-admin-password")
CALENDAR_TOKEN = os.environ.get("CALENDAR_TOKEN", "change-me-calendar-token")
BOOKING_DAYS_AHEAD = int(os.environ.get("BOOKING_DAYS_AHEAD", "60"))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me-secret-key")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=90)

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


# -----------------------------
# Database
# -----------------------------
def get_db():
    if "db" not in g:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'booked',
            created_at TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_active_slot
        ON bookings(date, start_time)
        WHERE status = 'booked';

        CREATE INDEX IF NOT EXISTS idx_bookings_email_date
        ON bookings(email, date);

        CREATE INDEX IF NOT EXISTS idx_bookings_date_time
        ON bookings(date, start_time);
        """
    )

    columns = {row["name"] for row in db.execute("PRAGMA table_info(bookings)").fetchall()}
    if "contact_type" not in columns:
        db.execute("ALTER TABLE bookings ADD COLUMN contact_type TEXT")
    if "contact" not in columns:
        db.execute("ALTER TABLE bookings ADD COLUMN contact TEXT")
    if "manage_token" not in columns:
        db.execute("ALTER TABLE bookings ADD COLUMN manage_token TEXT")

    # Backfill legacy email-only bookings without losing existing data.
    db.execute(
        """
        UPDATE bookings
        SET contact_type = 'email', contact = lower(email)
        WHERE contact_type IS NULL OR trim(contact_type) = '' OR contact IS NULL OR trim(contact) = ''
        """
    )
    legacy_contacts = db.execute(
        """
        SELECT DISTINCT contact_type, contact
        FROM bookings
        WHERE manage_token IS NULL OR trim(manage_token) = ''
        """
    ).fetchall()
    for row in legacy_contacts:
        token = secrets.token_urlsafe(24)
        db.execute(
            """
            UPDATE bookings SET manage_token = ?
            WHERE contact_type = ? AND contact = ?
              AND (manage_token IS NULL OR trim(manage_token) = '')
            """,
            (token, row["contact_type"], row["contact"]),
        )

    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_bookings_contact_date ON bookings(contact_type, contact, date)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_bookings_manage_token ON bookings(manage_token)"
    )
    db.commit()


with app.app_context():
    init_db()


# -----------------------------
# Helpers
# -----------------------------
def now_local():
    return datetime.now(TZ)


def parse_date(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def parse_hhmm(value: str):
    try:
        return datetime.strptime(value, "%H:%M").time()
    except (TypeError, ValueError):
        return None


def within_booking_window(day: date):
    today = now_local().date()
    return today <= day <= today + timedelta(days=BOOKING_DAYS_AHEAD)


def valid_slot_time(slot: time):
    start = datetime.combine(date.today(), time(8, 0))
    cutoff = datetime.combine(date.today(), time(14, 15))
    candidate = datetime.combine(date.today(), slot)
    return candidate >= start and candidate + timedelta(minutes=5) <= cutoff and slot.minute % 5 == 0


def slot_rows(day: date):
    db = get_db()
    taken = {
        row["start_time"]
        for row in db.execute(
            "SELECT start_time FROM bookings WHERE date = ? AND status = 'booked'",
            (day.isoformat(),),
        ).fetchall()
    }

    slots = []
    current = datetime.combine(day, time(8, 0), tzinfo=TZ)
    cutoff = datetime.combine(day, time(14, 15), tzinfo=TZ)
    now = now_local()

    while current + timedelta(minutes=5) <= cutoff:
        hhmm = current.strftime("%H:%M")
        if hhmm not in taken and current > now:
            slots.append(
                {
                    "value": hhmm,
                    "label": current.strftime("%I:%M %p").lstrip("0"),
                }
            )
        current += timedelta(minutes=5)
    return slots


def format_time(hhmm: str):
    return datetime.strptime(hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0")


def format_date(iso_date: str):
    dt = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{dt.strftime('%A, %B')} {dt.day}, {dt.year}"


def normalize_contact(contact_method: str, raw_contact: str):
    method = (contact_method or "").strip().lower()
    raw = (raw_contact or "").strip()
    if method == "email":
        value = raw.lower()
        if not EMAIL_RE.match(value) or len(value) > 160:
            return None
        return value
    if method == "phone":
        digits = re.sub(r"\D", "", raw)
        if len(digits) < 7 or len(digits) > 15:
            return None
        return ("+" if raw.startswith("+") else "") + digits
    return None


def format_phone(value: str):
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+1 ({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    return value


def contact_display(row):
    method = row["contact_type"] if "contact_type" in row.keys() else "email"
    contact = row["contact"] if "contact" in row.keys() else row["email"]
    if method == "phone":
        return format_phone(contact)
    return contact


def booking_is_upcoming(row):
    if row["status"] != "booked":
        return False
    start_dt = datetime.strptime(
        f"{row['date']} {row['start_time']}", "%Y-%m-%d %H:%M"
    ).replace(tzinfo=TZ)
    return start_dt > now_local()


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def escape_ics(value: str):
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def booking_datetimes(row):
    start_dt = datetime.strptime(
        f"{row['date']} {row['start_time']}", "%Y-%m-%d %H:%M"
    )
    end_dt = datetime.strptime(
        f"{row['date']} {row['end_time']}", "%Y-%m-%d %H:%M"
    )
    return start_dt, end_dt


def single_booking_ics(row):
    start_dt, end_dt = booking_datetimes(row)
    stamp = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Walks with Jack//Booking//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:walk-{row['id']}@walks-with-jack",
        f"DTSTAMP:{stamp}",
        f"DTSTART;TZID={TIMEZONE_NAME}:{start_dt.strftime('%Y%m%dT%H%M%S')}",
        f"DTEND;TZID={TIMEZONE_NAME}:{end_dt.strftime('%Y%m%dT%H%M%S')}",
        f"SUMMARY:{escape_ics('Walk with Jack')}",
        "DESCRIPTION:Free 5-minute walk booked through Walks with Jack.",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def google_calendar_url(row):
    start_dt, end_dt = booking_datetimes(row)
    params = {
        "action": "TEMPLATE",
        "text": "Walk with Jack",
        "dates": f"{start_dt.strftime('%Y%m%dT%H%M%S')}/{end_dt.strftime('%Y%m%dT%H%M%S')}",
        "details": "Free 5-minute walk booked through Walks with Jack.",
        "ctz": TIMEZONE_NAME,
    }
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


@app.context_processor
def template_helpers():
    return {
        "app_name": APP_NAME,
        "format_time": format_time,
        "format_date": format_date,
        "contact_display": contact_display,
        "booking_is_upcoming": booking_is_upcoming,
        "google_calendar_url": google_calendar_url,
    }


# -----------------------------
# Public booking
# -----------------------------
@app.get("/")
def index():
    today = now_local().date()
    selected = parse_date(request.args.get("date")) or today
    if not within_booking_window(selected):
        selected = today

    return render_template(
        "index.html",
        selected_date=selected.isoformat(),
        min_date=today.isoformat(),
        max_date=(today + timedelta(days=BOOKING_DAYS_AHEAD)).isoformat(),
        slots=slot_rows(selected),
    )


@app.get("/api/slots")
def api_slots():
    day = parse_date(request.args.get("date"))
    if day is None or not within_booking_window(day):
        return jsonify({"error": "Choose a valid booking date."}), 400
    return jsonify({"date": day.isoformat(), "slots": slot_rows(day)})


@app.post("/book")
def book():
    name = " ".join(request.form.get("name", "").strip().split())
    contact_method = request.form.get("contact_method", "email").strip().lower()
    contact = normalize_contact(contact_method, request.form.get("contact", ""))
    day = parse_date(request.form.get("date"))
    start = parse_hhmm(request.form.get("start_time"))

    if len(name) < 2 or len(name) > 80:
        flash("Please enter your name.", "error")
        return redirect(url_for("index", date=request.form.get("date", "")))
    if contact is None:
        label = "email address" if contact_method == "email" else "phone number"
        flash(f"Please enter a valid {label}.", "error")
        return redirect(url_for("index", date=request.form.get("date", "")))
    if day is None or not within_booking_window(day):
        flash("That date is not available for booking.", "error")
        return redirect(url_for("index"))
    if start is None or not valid_slot_time(start):
        flash("Please choose a valid 5-minute walk time.", "error")
        return redirect(url_for("index", date=day.isoformat()))

    start_dt = datetime.combine(day, start, tzinfo=TZ)
    if start_dt <= now_local():
        flash("That time has already passed.", "error")
        return redirect(url_for("index", date=day.isoformat()))

    end_dt = start_dt + timedelta(minutes=5)
    db = get_db()

    try:
        db.execute("BEGIN IMMEDIATE")

        daily_count = db.execute(
            """
            SELECT COUNT(*) AS count
            FROM bookings
            WHERE contact_type = ? AND contact = ? AND date = ? AND status = 'booked'
            """,
            (contact_method, contact, day.isoformat()),
        ).fetchone()["count"]

        if daily_count >= 2:
            db.rollback()
            flash("You can only book two walks per day.", "error")
            return redirect(url_for("index", date=day.isoformat()))

        taken = db.execute(
            """
            SELECT 1 FROM bookings
            WHERE date = ? AND start_time = ? AND status = 'booked'
            """,
            (day.isoformat(), start.strftime("%H:%M")),
        ).fetchone()

        if taken:
            db.rollback()
            flash("Someone just booked that time. Please choose another slot.", "error")
            return redirect(url_for("index", date=day.isoformat()))

        existing = db.execute(
            """
            SELECT manage_token FROM bookings
            WHERE contact_type = ? AND contact = ? AND manage_token IS NOT NULL
            ORDER BY id DESC LIMIT 1
            """,
            (contact_method, contact),
        ).fetchone()
        manage_token = existing["manage_token"] if existing else secrets.token_urlsafe(24)

        # Keep the original email column populated for compatibility with the old schema.
        email_value = contact if contact_method == "email" else ""
        cursor = db.execute(
            """
            INSERT INTO bookings
                (name, email, date, start_time, end_time, status, created_at,
                 contact_type, contact, manage_token)
            VALUES (?, ?, ?, ?, ?, 'booked', ?, ?, ?, ?)
            """,
            (
                name,
                email_value,
                day.isoformat(),
                start.strftime("%H:%M"),
                end_dt.strftime("%H:%M"),
                now_local().isoformat(),
                contact_method,
                contact,
                manage_token,
            ),
        )
        booking_id = cursor.lastrowid
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        flash("Someone just booked that time. Please choose another slot.", "error")
        return redirect(url_for("index", date=day.isoformat()))

    session.permanent = True
    session["walker_token"] = manage_token
    return redirect(url_for("booking_success", booking_id=booking_id, token=manage_token))


@app.get("/booking/<int:booking_id>")
def booking_success(booking_id):
    booking = get_db().execute(
        "SELECT * FROM bookings WHERE id = ?", (booking_id,)
    ).fetchone()
    token = request.args.get("token") or session.get("walker_token")
    if booking is None or not token or token != booking["manage_token"]:
        return "Booking not found", 404
    session.permanent = True
    session["walker_token"] = token
    return render_template("success.html", booking=booking, manage_token=token)


# -----------------------------
# Walker dashboard
# -----------------------------
@app.get("/my")
def my_bookings():
    token = session.get("walker_token")
    if token:
        return redirect(url_for("walker_dashboard", token=token))
    return render_template("my_missing.html")


@app.get("/my/<token>")
def walker_dashboard(token):
    db = get_db()
    rows = db.execute(
        """
        SELECT * FROM bookings
        WHERE manage_token = ?
        ORDER BY date DESC, start_time DESC
        """,
        (token,),
    ).fetchall()
    if not rows:
        return "Dashboard not found", 404

    session.permanent = True
    session["walker_token"] = token
    upcoming = [row for row in rows if booking_is_upcoming(row)]
    history = [row for row in rows if not booking_is_upcoming(row)]
    profile = rows[0]
    return render_template(
        "my_bookings.html",
        manage_token=token,
        profile=profile,
        upcoming=sorted(upcoming, key=lambda r: (r["date"], r["start_time"])),
        history=history,
    )


@app.post("/my/<token>/bookings/<int:booking_id>/cancel")
def walker_cancel_booking(token, booking_id):
    db = get_db()
    booking = db.execute(
        "SELECT * FROM bookings WHERE id = ? AND manage_token = ?",
        (booking_id, token),
    ).fetchone()
    if booking is None:
        flash("Booking not found.", "error")
    elif booking["status"] == "cancelled":
        flash("That booking is already cancelled.", "error")
    elif not booking_is_upcoming(booking):
        flash("Past walks cannot be cancelled.", "error")
    else:
        db.execute(
            "UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,)
        )
        db.commit()
        flash("Your walk was cancelled and the time is available again.", "success")
    return redirect(url_for("walker_dashboard", token=token))


@app.get("/my/<token>/bookings/<int:booking_id>/calendar.ics")
def booking_calendar_download(token, booking_id):
    booking = get_db().execute(
        "SELECT * FROM bookings WHERE id = ? AND manage_token = ? AND status = 'booked'",
        (booking_id, token),
    ).fetchone()
    if booking is None:
        return "Booking not found", 404
    body = single_booking_ics(booking)
    filename = f"walk-with-jack-{booking['date']}-{booking['start_time'].replace(':', '')}.ics"
    return Response(
        body,
        mimetype="text/calendar",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )


# -----------------------------
# Jack's dashboard
# -----------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == ADMIN_PASSWORD:
            walker_token = session.get("walker_token")
            session.clear()
            if walker_token:
                session["walker_token"] = walker_token
            session["admin_logged_in"] = True
            return redirect(request.args.get("next") or url_for("admin_dashboard"))
        flash("Incorrect password.", "error")
    return render_template("admin_login.html")


@app.post("/admin/logout")
def admin_logout():
    walker_token = session.get("walker_token")
    session.clear()
    if walker_token:
        session["walker_token"] = walker_token
    return redirect(url_for("index"))


@app.get("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    now = now_local()
    today = now.date().isoformat()
    current_time = now.strftime("%H:%M")

    total_bookings = db.execute(
        "SELECT COUNT(*) AS count FROM bookings WHERE status = 'booked'"
    ).fetchone()["count"]
    today_bookings = db.execute(
        "SELECT COUNT(*) AS count FROM bookings WHERE date = ? AND status = 'booked'",
        (today,),
    ).fetchone()["count"]
    upcoming_count = db.execute(
        """
        SELECT COUNT(*) AS count FROM bookings
        WHERE status = 'booked'
          AND (date > ? OR (date = ? AND start_time >= ?))
        """,
        (today, today, current_time),
    ).fetchone()["count"]
    unique_walkers = db.execute(
        """
        SELECT COUNT(DISTINCT contact_type || ':' || contact) AS count
        FROM bookings WHERE status = 'booked'
        """
    ).fetchone()["count"]

    upcoming = db.execute(
        """
        SELECT * FROM bookings
        WHERE status = 'booked'
          AND (date > ? OR (date = ? AND start_time >= ?))
        ORDER BY date ASC, start_time ASC
        LIMIT 40
        """,
        (today, today, current_time),
    ).fetchall()

    next_booking = upcoming[0] if upcoming else None

    daily_counts = []
    max_count = 1
    for offset in range(6, -1, -1):
        d = now.date() - timedelta(days=offset)
        count = db.execute(
            "SELECT COUNT(*) AS count FROM bookings WHERE date = ? AND status = 'booked'",
            (d.isoformat(),),
        ).fetchone()["count"]
        max_count = max(max_count, count)
        daily_counts.append({"label": d.strftime("%a"), "count": count})

    for item in daily_counts:
        item["height"] = max(5, round((item["count"] / max_count) * 100)) if item["count"] else 3

    calendar_url = request.host_url.rstrip("/") + url_for(
        "calendar_feed", token=CALENDAR_TOKEN
    )

    return render_template(
        "admin.html",
        total_bookings=total_bookings,
        today_bookings=today_bookings,
        upcoming_count=upcoming_count,
        unique_walkers=unique_walkers,
        upcoming=upcoming,
        next_booking=next_booking,
        daily_counts=daily_counts,
        calendar_url=calendar_url,
        timezone_name=TIMEZONE_NAME,
    )


@app.post("/admin/bookings/<int:booking_id>/cancel")
@admin_required
def cancel_booking(booking_id):
    db = get_db()
    booking = db.execute(
        "SELECT * FROM bookings WHERE id = ?", (booking_id,)
    ).fetchone()
    if booking is None:
        flash("Booking not found.", "error")
    elif booking["status"] == "cancelled":
        flash("That booking is already cancelled.", "error")
    else:
        db.execute(
            "UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,)
        )
        db.commit()
        flash("Booking cancelled. The time slot is available again.", "success")
    return redirect(url_for("admin_dashboard"))


# -----------------------------
# Calendar subscription
# -----------------------------
@app.get("/calendar/jack.ics")
def calendar_feed():
    token = request.args.get("token", "")
    if token != CALENDAR_TOKEN:
        return "Not found", 404

    db = get_db()
    today = now_local().date().isoformat()
    rows = db.execute(
        """
        SELECT * FROM bookings
        WHERE status = 'booked' AND date >= ?
        ORDER BY date ASC, start_time ASC
        """,
        (today,),
    ).fetchall()

    stamp = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Walks with Jack//Booking Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape_ics(APP_NAME)}",
        f"X-WR-TIMEZONE:{TIMEZONE_NAME}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT15M",
        "X-PUBLISHED-TTL:PT15M",
    ]

    for row in rows:
        start_dt, end_dt = booking_datetimes(row)
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:walk-{row['id']}@walks-with-jack",
                f"DTSTAMP:{stamp}",
                f"DTSTART;TZID={TIMEZONE_NAME}:{start_dt.strftime('%Y%m%dT%H%M%S')}",
                f"DTEND;TZID={TIMEZONE_NAME}:{end_dt.strftime('%Y%m%dT%H%M%S')}",
                f"SUMMARY:{escape_ics('Walk with ' + row['name'])}",
                "DESCRIPTION:Booked through Walks with Jack.",
                "END:VEVENT",
            ]
        )

    lines.append("END:VCALENDAR")
    body = "\r\n".join(lines) + "\r\n"
    return Response(
        body,
        mimetype="text/calendar",
        headers={
            "Content-Disposition": "inline; filename=jack-walks.ics",
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )


@app.get("/healthz")
def healthz():
    return {"ok": True, "app": APP_NAME}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
