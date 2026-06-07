#!/usr/bin/env python3
"""Send the brief. Resend by default, Gmail SMTP as fallback.

All credentials come from environment variables. Never hardcode them.
"""
import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

DEFAULT_FROM = "DSN Brief <brief@yourdomain.com>"
DEFAULT_TO = "isaacs@dsn.com"


def _recipients():
    raw = os.environ.get("BRIEF_TO_EMAIL", DEFAULT_TO)
    return [addr.strip() for addr in raw.split(",") if addr.strip()]


def send_brief(html, subject):
    """Pick a transport based on which secrets are present."""
    if os.environ.get("RESEND_API_KEY"):
        return _send_resend(html, subject)
    if os.environ.get("GMAIL_USER") and os.environ.get("GMAIL_APP_PASSWORD"):
        return _send_gmail(html, subject)
    raise RuntimeError(
        "No email transport configured. Set RESEND_API_KEY, or "
        "GMAIL_USER + GMAIL_APP_PASSWORD."
    )


def _send_resend(html, subject):
    api_key = os.environ["RESEND_API_KEY"]
    payload = {
        "from": os.environ.get("BRIEF_FROM_EMAIL", DEFAULT_FROM),
        "to": _recipients(),
        "subject": subject,
        "html": html,
    }
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    if not resp.ok:
        raise requests.HTTPError(f"Resend {resp.status_code}: {resp.text[:500]}")
    msg_id = resp.json().get("id")
    print(f"sent via Resend (id={msg_id}) to {', '.join(_recipients())}")
    return msg_id


def _send_gmail(html, subject):
    user = os.environ["GMAIL_USER"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    to = _recipients()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = os.environ.get("BRIEF_FROM_EMAIL", user)
    msg["To"] = ", ".join(to)
    msg.attach(MIMEText("Your email client does not support HTML.", "plain"))
    msg.attach(MIMEText(html, "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(user, password)
        server.sendmail(user, to, msg.as_string())
    print(f"sent via Gmail SMTP to {', '.join(to)}")
    return None
