#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import smtplib
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


URL = os.getenv(
    "TICKET_URL",
    "https://www.wigmore-hall.org.uk/whats-on/202606031930",
)

STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))

# Wigmore Hall's own FAQ says “Sold Out” changes to “Book Now” when returns appear.
AVAILABLE_CLICK_PATTERNS = [
    r"\bbook\s*now\b",
    r"\bbuy\s*tickets?\b",
    r"\bselect\s*(tickets?|seats?)\b",
    r"\bchoose\s*seats?\b",
    r"\badd\s*to\s*basket\b",
]

UNAVAILABLE_PATTERNS = [
    r"\bsold\s*out\b",
    r"\bno\s*tickets?\s*available\b",
    r"\bcurrently\s*unavailable\b",
    r"\bnot\s*currently\s*available\b",
    r"\bnot\s*on\s*sale\b",
    r"\bbooking\s*has\s*closed\b",
    r"\bfully\s*booked\b",
]


@dataclass(frozen=True)
class CheckResult:
    status: str  # "available", "unavailable", or "unknown"
    reason: str
    visible_clickables: list[str]
    relevant_text: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalise(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def any_pattern(patterns: Iterable[str], text: str) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"status": "unknown", "url": URL}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"status": "unknown", "url": URL}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(STATE_FILE)


def send_email(result: CheckResult, previous_status: str) -> None:
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_username = os.getenv("SMTP_USERNAME", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    email_from = os.getenv("EMAIL_FROM", smtp_username).strip()
    email_to = os.getenv("EMAIL_TO", "").strip()

    missing = [
        name
        for name, value in {
            "SMTP_HOST": smtp_host,
            "SMTP_USERNAME": smtp_username,
            "SMTP_PASSWORD": smtp_password,
            "EMAIL_FROM or SMTP_USERNAME": email_from,
            "EMAIL_TO": email_to,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing required email settings: {', '.join(missing)}")

    msg = EmailMessage()
    msg["Subject"] = "Wigmore Hall ticket may be available"
    msg["From"] = email_from
    msg["To"] = email_to
    msg.set_content(
        f"""A ticket may now be available for the Wigmore Hall event.

Event page:
{URL}

Detected status: {result.status}
Previous stored status: {previous_status}
Reason: {result.reason}
Checked at: {utc_now()}

Visible booking-related controls seen:
{chr(10).join("- " + x for x in result.visible_clickables[:30]) or "- none"}

Open the page quickly and book manually if tickets are still available.
"""
    )

    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as server:
            server.login(smtp_username, smtp_password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.starttls()
            server.login(smtp_username, smtp_password)
            server.send_message(msg)


def get_page_state() -> CheckResult:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-GB",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
        )

        page = context.new_page()

        # Keep the check light: block media/images/fonts, but keep JS/CSS/XHR/fetch.
        def route_request(route):
            if route.request.resource_type in {"image", "media", "font"}:
                route.abort()
            else:
                route.continue_()

        page.route("**/*", route_request)

        page.goto(URL, wait_until="domcontentloaded", timeout=60_000)

        # Cookie banner wording may change; harmless if none of these exist.
        for label in ["Accept all", "Accept", "I accept", "Allow all"]:
            try:
                button = page.get_by_role("button", name=re.compile(label, re.I)).first
                if button.is_visible(timeout=1_000):
                    button.click(timeout=2_000)
                    break
            except Exception:
                pass

        # Let the availability widget finish rendering. Do not fail merely because
        # the page keeps a long-polling or analytics request open.
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except PlaywrightTimeoutError:
            pass

        page.wait_for_timeout(3_000)

        body_text = page.locator("body").inner_text(timeout=10_000)

        clickables = page.locator("a, button, [role=button]").evaluate_all(
            """
            els => els
              .filter(el => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== 'hidden'
                  && style.display !== 'none'
                  && rect.width > 0
                  && rect.height > 0;
              })
              .map(el => (el.innerText || el.getAttribute('aria-label') || '').trim())
              .filter(Boolean)
            """
        )

        # Save debugging aids in the GitHub Actions artifact when status is unknown.
        Path("debug_page_text.txt").write_text(body_text[:12000], encoding="utf-8")
        try:
            page.screenshot(path="debug_screenshot.png", full_page=True)
        except Exception:
            pass

        browser.close()

    clickable_text = "\n".join(clickables)
    clickable_norm = normalise(clickable_text)
    body_norm = normalise(body_text)

    # Prefer clickables: Wigmore says the booking label changes to “Book Now”.
    if any_pattern(AVAILABLE_CLICK_PATTERNS, clickable_norm):
        return CheckResult(
            status="available",
            reason="Found a visible booking control such as 'Book Now'.",
            visible_clickables=clickables,
            relevant_text=body_text[:2000],
        )

    if any_pattern(UNAVAILABLE_PATTERNS, clickable_norm) or any_pattern(UNAVAILABLE_PATTERNS, body_norm):
        return CheckResult(
            status="unavailable",
            reason="Found unavailable wording such as 'Sold Out'.",
            visible_clickables=clickables,
            relevant_text=body_text[:2000],
        )

    return CheckResult(
        status="unknown",
        reason="Could not confidently find either 'Book Now' or 'Sold Out'.",
        visible_clickables=clickables,
        relevant_text=body_text[:2000],
    )


def main() -> int:
    state = load_state()
    previous_status = state.get("status", "unknown")

    print(f"Previous stored status: {previous_status}")
    result = get_page_state()
    print(f"Detected status: {result.status}")
    print(f"Reason: {result.reason}")
    print("Visible clickables:")
    for item in result.visible_clickables[:50]:
        print(f"  - {item}")

    if result.status == "unknown":
        print("Status unknown; leaving stored state unchanged to avoid duplicate or false alerts.")
        return 0

    if result.status == previous_status:
        print("No status transition; no email sent.")
        return 0

    if result.status == "available":
        print("Transition to available detected; sending email.")
        send_email(result, previous_status)

    new_state = {
        "url": URL,
        "status": result.status,
        "previous_status": previous_status,
        "last_transition_at": utc_now(),
        "last_reason": result.reason,
    }
    save_state(new_state)
    print(f"State updated to {result.status}.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise