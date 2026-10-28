import base64
import html
import json
import os
import re
import urllib.parse
import urllib.request


def _format_phone_for_sms(value: str):
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if (value or "").startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits
    return None


def _post_json(url, payload, headers):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=10) as response:
        return 200 <= response.status < 300


def _post_form(url, payload, username, password):
    body = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    req.add_header("Authorization", f"Basic {token}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=10) as response:
        return 200 <= response.status < 300


def send_booking_confirmation(booking, *, date_label, time_label, manage_url, calendar_url):
    """Send an email or SMS after a booking is safely committed.

    Returns one of: sent, not_configured, failed.
    """
    contact_type = booking["contact_type"]
    contact = booking["contact"]
    name = booking["name"]

    if contact_type == "email":
        api_key = os.environ.get("RESEND_API_KEY", "").strip()
        sender = os.environ.get("EMAIL_FROM", "").strip()
        if not api_key or not sender:
            return "not_configured"

        subject = f"Walk with Jack confirmed for {date_label}"
        plain = (
            f"Hi {name},\n\n"
            f"Your free 5-minute walk with Jack is confirmed.\n\n"
            f"Date: {date_label}\n"
            f"Time: {time_label}\n\n"
            f"Manage or cancel your booking: {manage_url}\n"
            f"Add it to your calendar: {calendar_url}\n\n"
            "See you then!"
        )
        safe_name = html.escape(name)
        safe_date = html.escape(date_label)
        safe_time = html.escape(time_label)
        safe_manage = html.escape(manage_url, quote=True)
        safe_calendar = html.escape(calendar_url, quote=True)
        html_body = f"""
        <div style="font-family:Arial,sans-serif;max-width:560px;margin:auto;color:#173f35">
          <h2>Your walk with Jack is confirmed</h2>
          <p>Hi {safe_name},</p>
          <p>Your free 5-minute walk with Jack is booked.</p>
          <p><strong>Date:</strong> {safe_date}<br><strong>Time:</strong> {safe_time}</p>
          <p><a href="{safe_manage}">Manage or cancel your booking</a></p>
          <p><a href="{safe_calendar}">Add it to your calendar</a></p>
          <p>See you then!</p>
        </div>
        """
        try:
            ok = _post_json(
                "https://api.resend.com/emails",
                {
                    "from": sender,
                    "to": [contact],
                    "subject": subject,
                    "text": plain,
                    "html": html_body,
                },
                {"Authorization": f"Bearer {api_key}"},
            )
            return "sent" if ok else "failed"
        except Exception:
            return "failed"

    if contact_type == "phone":
        account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
        auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
        from_number = os.environ.get("TWILIO_FROM_NUMBER", "").strip()
        to_number = _format_phone_for_sms(contact)
        if not account_sid or not auth_token or not from_number or not to_number:
            return "not_configured"

        message = (
            f"Walk with Jack confirmed: {date_label} at {time_label}. "
            f"Manage/cancel: {manage_url}"
        )
        try:
            ok = _post_form(
                f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
                {"From": from_number, "To": to_number, "Body": message},
                account_sid,
                auth_token,
            )
            return "sent" if ok else "failed"
        except Exception:
            return "failed"

    return "failed"
