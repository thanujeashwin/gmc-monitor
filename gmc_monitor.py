#!/usr/bin/env python3
"""
GMC Registration Monitor — runs inside GitHub Actions.
Fetches https://www.gmc-uk.org/registrants/7959006, compares to
state.json, and sends an email via Gmail SMTP on any change.
"""

import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
GMC_URL      = "https://www.gmc-uk.org/registrants/7959006"
NOTIFY_EMAIL = "ashwin@wso2.com"
STATE_FILE   = Path("state.json")

# Injected via GitHub Actions secrets
SMTP_USER = os.environ.get("SMTP_USER", "")   # your Gmail address
SMTP_PASS = os.environ.get("SMTP_PASS", "")   # Gmail app password

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Cache-Control": "max-age=0",
}


# ── Extraction ────────────────────────────────────────────────────────────────

def fetch_page(retries: int = 3, delay: int = 10) -> str:
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(GMC_URL, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                # Handle gzip/deflate transparently
                encoding = resp.headers.get("Content-Encoding", "")
                if encoding == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                elif encoding == "br":
                    import brotli  # type: ignore
                    raw = brotli.decompress(raw)
                return raw.decode("utf-8", errors="replace")
        except Exception as e:
            last_err = e
            print(f"  Attempt {attempt} failed: {e}")
            if attempt < retries:
                time.sleep(delay)
    raise last_err


def extract_snapshot(html: str) -> dict:
    snap = {}

    # Registration status — order matters (most specific first)
    statuses = [
        "Erasure",
        "Suspension",
        "Voluntary removal",
        "Conditional registration",
        "Registered without a licence to practise",
        "Registered with a licence to practise",
        "Not registered",
    ]
    snap["status"] = next(
        (s for s in statuses if s.lower() in html.lower()), "Unknown"
    )

    # GP / Specialist register
    snap["gp_register"]         = "This doctor is on the GP Register" in html
    snap["specialist_register"] = "This doctor is on the Specialist Register" in html

    # APS scheme flag
    snap["aps_scheme"] = (
        "approved practice setting" in html.lower()
        and "subject to the requirements" in html.lower()
    )

    # Designated body (grab the NHS trust line)
    for line in html.splitlines():
        s = line.strip()
        if any(kw in s for kw in ("NHS Trust", "NHS Foundation", "University Hospitals")) and len(s) < 120:
            snap["designated_body"] = s
            break

    # Annual retention fee date
    lines = html.splitlines()
    for i, line in enumerate(lines):
        if "Annual retention fee due date" in line:
            # value is often inline after a colon, or on the next non-empty line
            combined = " ".join(lines[i:i+3])
            m = re.search(r"(\d{2} \w+ \d{4})", combined)
            if m:
                snap["retention_fee_date"] = m.group(1)
            break

    snap["checked_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    return snap


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(snap: dict):
    STATE_FILE.write_text(json.dumps(snap, indent=2))


def diff_fields(old: dict, new: dict) -> list[str]:
    ignore = {"checked_at"}
    diffs = []
    for k in (set(old) | set(new)) - ignore:
        if old.get(k) != new.get(k):
            diffs.append(f"  {k}:\n    was: {old.get(k)!r}\n    now: {new.get(k)!r}")
    return diffs


# ── Notification ──────────────────────────────────────────────────────────────

def send_email(subject: str, body: str):
    if not SMTP_USER or not SMTP_PASS:
        print("  WARNING: SMTP credentials not set — skipping email.")
        return

    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = NOTIFY_EMAIL

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, NOTIFY_EMAIL, msg.as_string())
    print(f"  Email sent to {NOTIFY_EMAIL}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"[{datetime.utcnow():%Y-%m-%d %H:%M UTC}] Checking GMC registration…")

    try:
        html = fetch_page()
    except Exception as e:
        # Log but exit cleanly — transient network errors shouldn't
        # mark the workflow run as failed (red X) in GitHub Actions.
        print(f"  ERROR fetching page after retries: {e}", file=sys.stderr)
        print("  Skipping this check. Will retry in 5 minutes.")
        sys.exit(0)

    new_snap = extract_snapshot(html)
    old_snap = load_state()

    print(f"  Status : {new_snap.get('status')}")
    print(f"  GP Reg : {new_snap.get('gp_register')}")
    print(f"  SP Reg : {new_snap.get('specialist_register')}")
    print(f"  APS    : {new_snap.get('aps_scheme')}")
    print(f"  Body   : {new_snap.get('designated_body', 'n/a')}")
    print(f"  Fee due: {new_snap.get('retention_fee_date', 'n/a')}")

    if not old_snap:
        save_state(new_snap)
        print("  First run — baseline saved.")
        send_email(
            "GMC Monitor: baseline saved",
            f"Monitoring started for GMC 7959006 (Thavindra Delpagoda Gamage).\n\n"
            f"Current status: {new_snap.get('status')}\n"
            f"Checked at: {new_snap['checked_at']}\n\n"
            f"You'll be notified at this address whenever anything changes.\n\n"
            f"View record: {GMC_URL}"
        )
        return

    diffs = diff_fields(old_snap, new_snap)
    if diffs:
        diff_text = "\n".join(diffs)
        print(f"  CHANGE DETECTED:\n{diff_text}")

        send_email(
            "⚠️ GMC Registration Change Detected",
            f"A change was detected on the GMC registration record for:\n"
            f"Thavindra Delpagoda Gamage  |  GMC ref: 7959006\n\n"
            f"{'='*50}\n"
            f"CHANGES:\n{diff_text}\n"
            f"{'='*50}\n\n"
            f"Current status: {new_snap.get('status')}\n"
            f"Detected at: {new_snap['checked_at']}\n\n"
            f"View full record: {GMC_URL}"
        )
        save_state(new_snap)
    else:
        print("  No changes detected.")
        # State file unchanged — git will not create a commit


if __name__ == "__main__":
    main()
