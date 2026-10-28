"""Runtime wrapper for the existing Flask app.

Gunicorn imports this package as `app`; we then load the original root app.py,
keep all of its routes, and wrap only the successful booking endpoint so a
confirmation can be delivered after the database commit succeeds.
"""

import functools
import importlib.util
import re
from pathlib import Path

from notifications import send_booking_confirmation

_ROOT_APP = Path(__file__).resolve().parent.parent / "app.py"
_spec = importlib.util.spec_from_file_location("walks_original_app", _ROOT_APP)
_original = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_original)

app = _original.app
_original_book = app.view_functions["book"]


@functools.wraps(_original_book)
def _book_with_confirmation(*args, **kwargs):
    result = _original_book(*args, **kwargs)
    response = app.make_response(result)

    location = response.headers.get("Location", "")
    match = re.search(r"/booking/(\d+)", location)
    if not match:
        return response

    booking_id = int(match.group(1))
    booking = _original.get_db().execute(
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
