"""Runtime wrapper for the existing Flask app.

Gunicorn imports this package as `app`; we load the original root app.py,
keep its routes, and add runtime features that can migrate existing bookings
without losing data.
"""

import functools
import importlib.util
import re
import sqlite3
import sys
from pathlib import Path

from notifications import send_booking_confirmation

_ROOT_APP = Path(__file__).resolve().parent.parent / "app.py"
_spec = importlib.util.spec_from_file_location("walks_original_app", _ROOT_APP)
_original = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _original
_spec.loader.exec_module(_original)

app = _original.app


# Add note fields without deleting or recreating the existing database.
with app.app_context():
    db = _original.get_db()
    columns = {row["name"] for row in db.execute("PRAGMA table_info(bookings)").fetchall()}
    if "walker_notes" not in columns:
        db.execute("ALTER TABLE bookings ADD COLUMN walker_notes TEXT NOT NULL DEFAULT ''")
    if "jack_notes" not in columns:
        db.execute("ALTER TABLE bookings ADD COLUMN jack_notes TEXT NOT NULL DEFAULT ''")
    db.commit()


_original_book = app.view_functions["book"]


def _clean_note(value):
    return (value or "").strip()[:1000]


@functools.wraps(_original_book)
def _book_with_confirmation(*args, **kwargs):
    result = _original_book(*args, **kwargs)
    response = app.make_response(result)

    location = response.headers.get("Location", "")
    match = re.search(r"/booking/(\d+)", location)
    if not match:
        return response

    booking_id = int(match.group(1))
    db = _original.get_db()

    # Save the optional note only after the booking itself successfully wins
    # the unique date/time slot.
    walker_notes = _clean_note(_original.request.form.get("notes", ""))
    if walker_notes:
        db.execute(
            "UPDATE bookings SET walker_notes = ? WHERE id = ?",
            (walker_notes, booking_id),
        )
        db.commit()

    booking = db.execute(
        "SELECT * FROM bookings WHERE id = ? AND status = 'booked'",
        (booking_id,),
    ).fetchone()
    if booking is None:
        return response

    manage_url = _original.url_for(
        "walker_dashboard", token=booking["manage_token"], _external=True
    )
    calendar_url = _original.url_for(
        "booking_calendar_download",
        token=booking["manage_token"],
        booking_id=booking["id"],
        _external=True,
    )
    status = send_booking_confirmation(
        booking,
        date_label=_original.format_date(booking["date"]),
        time_label=(
            f"{_original.format_time(booking['start_time'])}–"
            f"{_original.format_time(booking['end_time'])}"
        ),
        manage_url=manage_url,
        calendar_url=calendar_url,
    )

    if status == "sent":
        if booking["contact_type"] == "email":
            _original.flash("Confirmation email sent.", "success")
        else:
            _original.flash("Confirmation text sent.", "success")
    elif status == "failed":
        _original.flash(
            "Your walk is booked, but the confirmation could not be delivered.",
            "error",
        )

    return response


app.view_functions["book"] = _book_with_confirmation


@app.post("/my/<token>/bookings/<int:booking_id>/notes")
def walker_update_notes(token, booking_id):
    db = _original.get_db()
    booking = db.execute(
        "SELECT * FROM bookings WHERE id = ? AND manage_token = ?",
        (booking_id, token),
    ).fetchone()
    if booking is None:
        _original.flash("Booking not found.", "error")
    elif booking["status"] != "booked":
        _original.flash("Notes can only be changed for an active booking.", "error")
    else:
        db.execute(
            "UPDATE bookings SET walker_notes = ? WHERE id = ?",
            (_clean_note(_original.request.form.get("walker_notes", "")), booking_id),
        )
        db.commit()
        _original.flash("Your note was saved for Jack.", "success")
    return _original.redirect(_original.url_for("walker_dashboard", token=token))


@app.post("/admin/bookings/<int:booking_id>/move")
@_original.admin_required
def admin_move_booking(booking_id):
    day = _original.parse_date(_original.request.form.get("date"))
    start = _original.parse_hhmm(_original.request.form.get("start_time"))

    if day is None or not _original.within_booking_window(day):
        _original.flash("Choose a valid booking date.", "error")
        return _original.redirect(_original.url_for("admin_dashboard"))
    if start is None or not _original.valid_slot_time(start):
        _original.flash("Choose a valid 5-minute walk time.", "error")
        return _original.redirect(_original.url_for("admin_dashboard"))

    start_dt = _original.datetime.combine(day, start, tzinfo=_original.TZ)
    if start_dt <= _original.now_local():
        _original.flash("Bookings can only be moved to a future time.", "error")
        return _original.redirect(_original.url_for("admin_dashboard"))

    end_dt = start_dt + _original.timedelta(minutes=5)
    db = _original.get_db()
    booking = db.execute(
        "SELECT * FROM bookings WHERE id = ?", (booking_id,)
    ).fetchone()
    if booking is None:
        _original.flash("Booking not found.", "error")
        return _original.redirect(_original.url_for("admin_dashboard"))

    try:
        db.execute("BEGIN IMMEDIATE")
        taken = db.execute(
            """
            SELECT 1 FROM bookings
            WHERE date = ? AND start_time = ? AND status = 'booked' AND id != ?
            """,
            (day.isoformat(), start.strftime("%H:%M"), booking_id),
        ).fetchone()
        if taken:
            db.rollback()
            _original.flash("That time already belongs to another booking.", "error")
            return _original.redirect(_original.url_for("admin_dashboard"))

        db.execute(
            """
            UPDATE bookings
            SET date = ?, start_time = ?, end_time = ?
            WHERE id = ?
            """,
            (
                day.isoformat(),
                start.strftime("%H:%M"),
                end_dt.strftime("%H:%M"),
                booking_id,
            ),
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        _original.flash("That time already belongs to another booking.", "error")
        return _original.redirect(_original.url_for("admin_dashboard"))

    _original.flash("Booking time updated.", "success")
    return _original.redirect(_original.url_for("admin_dashboard"))


@app.post("/admin/bookings/<int:booking_id>/notes")
@_original.admin_required
def admin_save_booking_notes(booking_id):
    db = _original.get_db()
    booking = db.execute("SELECT 1 FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    if booking is None:
        _original.flash("Booking not found.", "error")
    else:
        db.execute(
            "UPDATE bookings SET jack_notes = ? WHERE id = ?",
            (_clean_note(_original.request.form.get("jack_notes", "")), booking_id),
        )
        db.commit()
        _original.flash("Jack's note was saved.", "success")
    return _original.redirect(_original.url_for("admin_dashboard"))


@app.post("/admin/bookings/<int:booking_id>/delete")
@_original.admin_required
def admin_delete_booking(booking_id):
    db = _original.get_db()
    booking = db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    if booking is None:
        _original.flash("Booking not found.", "error")
    else:
        db.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
        db.commit()
        _original.flash("Booking permanently deleted. The time slot is open again.", "success")
    return _original.redirect(_original.url_for("admin_dashboard"))
