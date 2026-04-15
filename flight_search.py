#!/usr/bin/env python3
"""
Flight Search – OPO → Asia & Latin America (Sep–Oct 2026)
==========================================================
Uses fast-flights (Google Flights, no API key or sign-up needed).

Install once:
    pip install fast-flights playwright
    playwright install chromium

Usage:
    python flight_search.py                  # search + print ranked table
    python flight_search.py --email          # search + email if new flights found
    python flight_search.py --email --force  # email regardless of new flights
    python flight_search.py --install-cron   # twice-daily cron (8 AM + 8 PM)

Env vars (add to morning_newsletter/.env — only needed for --email):
    SMTP_USER   Gmail address  (shared with newsletter.py)
    SMTP_PASS   Gmail App Password
    TO_EMAIL    Recipient email (defaults to SMTP_USER)

Filters applied:
    Origin:       OPO (Porto)
    Trip:         Round-trip, minimum 15-day stay
    Departure:    01 Sep – 31 Oct 2026, sampled 5 date pairs
    Return:       Departure + 15 days
    Max price:    €800 round-trip
    Max stops:    1 (each direction)
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import logging
import os
import re
import smtplib
import ssl
import subprocess
import sys
import queue
import threading
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv
from fast_flights import FlightData, Passengers
from fast_flights.core import parse_response
from fast_flights.filter import TFSData

load_dotenv(Path(__file__).parent / ".env")

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
ORIGIN       = "OPO"
MAX_PRICE    = 800          # EUR, round-trip total
MAX_STOPS    = 1            # per direction
MIN_STAY     = 15           # days between outbound and return
MAX_WORKERS  = 6            # persistent Chromium browsers (one per thread)

# Every day in Sep + Oct 2026; return = departure + MIN_STAY days
def _all_trip_pairs() -> list[tuple[str, str]]:
    from datetime import date, timedelta
    pairs, d = [], date(2026, 9, 1)
    while d <= date(2026, 10, 31):
        pairs.append((d.strftime("%Y-%m-%d"), (d + timedelta(days=MIN_STAY)).strftime("%Y-%m-%d")))
        d += timedelta(days=1)
    return pairs

TRIP_PAIRS: list[tuple[str, str]] = _all_trip_pairs()   # 61 pairs
CACHE_FILE   = Path(__file__).parent / "flights_cache.json"

_ACCENT  = "#C2410C"
_ACCENT2 = "#7C2D12"

# ── Destination hub airports ───────────────────────────────────────────────────
# Curated set of the most reachable / cheapest-deal hubs from OPO.
# (IATA code, city, country)

ASIA_HUBS: list[tuple[str, str, str]] = [
    ("BKK", "Bangkok",           "Thailand"),
    ("SIN", "Singapore",         "Singapore"),
    ("KUL", "Kuala Lumpur",      "Malaysia"),
    ("HKG", "Hong Kong",         "Hong Kong"),
    ("NRT", "Tokyo",             "Japan"),
    ("ICN", "Seoul",             "South Korea"),
    ("TAS", "Tashkent",          "Uzbekistan"),
    ("MNL", "Manila",            "Philippines"),
    ("SGN", "Ho Chi Minh City",  "Vietnam"),
    ("CGK", "Jakarta",           "Indonesia"),
    ("DPS", "Bali",              "Indonesia"),
    ("PVG", "Shanghai",          "China"),
]

_HUB_INFO: dict[str, tuple[str, str]] = {
    code: (city, country)
    for code, city, country in ASIA_HUBS
}

# ── Parsing helpers ────────────────────────────────────────────────────────────

def _parse_price(price_str: str) -> float:
    """'€625' or '€1,250' → 625.0"""
    cleaned = re.sub(r"[^\d.]", "", price_str)
    return float(cleaned) if cleaned else 0.0


def _parse_duration(dur_str: str) -> float:
    """'15 hr 35 min' or '1 hr 5 min' → decimal hours."""
    h = re.search(r"(\d+)\s*hr", dur_str)
    m = re.search(r"(\d+)\s*min", dur_str)
    return (int(h.group(1)) if h else 0) + (int(m.group(1)) if m else 0) / 60


def _format_departure(dep_str: str, search_date: str) -> str:
    """
    '12:15 PM on Tue, Sep 15' + '2026-09-15' → '2026-09-15 12:15'
    Uses only the date portion from search_date; time from dep_str.
    """
    date_part = search_date[:10]  # YYYY-MM-DD
    # Extract HH:MM from the time portion, convert 12-hour to 24-hour
    m = re.match(r"(\d+):(\d+)\s*(AM|PM)", dep_str, re.IGNORECASE)
    if not m:
        return f"{date_part} ??"
    hour, minute, period = int(m.group(1)), int(m.group(2)), m.group(3).upper()
    if period == "PM" and hour != 12:
        hour += 12
    elif period == "AM" and hour == 12:
        hour = 0
    return f"{date_part} {hour:02d}:{minute:02d}"


# ── Flight search ──────────────────────────────────────────────────────────────

def _build_tfs(dest: str, dep_date: str, ret_date: str) -> str:
    """Encode a round-trip search as a base64 TFS string for Google Flights."""
    return TFSData.from_interface(
        flight_data=[
            FlightData(date=dep_date, from_airport=ORIGIN, to_airport=dest),
            FlightData(date=ret_date, from_airport=dest,   to_airport=ORIGIN),
        ],
        trip="round-trip",
        passengers=Passengers(adults=1),
        seat="economy",
        max_stops=MAX_STOPS,
    ).as_b64().decode()


def _parse_flights_from_html(html: str, dest: str, dep_date: str, ret_date: str) -> list[dict]:
    """Parse fast-flights result objects out of a Google Flights HTML snippet."""
    class _Resp:
        text = html
        text_markdown = html

    try:
        result = parse_response(_Resp())
    except RuntimeError:
        return []

    city, country = _HUB_INFO.get(dest, (dest, "Unknown"))
    dep_short = dep_date.replace("-", "")
    ret_short = ret_date.replace("-", "")
    flights: list[dict] = []

    for fl in result.flights:
        price = _parse_price(fl.price)
        if price <= 0 or price > MAX_PRICE:
            continue
        stops = fl.stops if isinstance(fl.stops, int) else MAX_STOPS + 1
        if stops > MAX_STOPS:
            continue
        flights.append({
            "price":      price,
            "city":       city,
            "country":    country,
            "airport":    dest,
            "departure":  _format_departure(fl.departure, dep_date),
            "return":     ret_date,
            "stay_days":  MIN_STAY,
            "duration_h": round(_parse_duration(fl.duration), 1),
            "stops":      stops,
            "airlines":   fl.name,
            "url":        f"https://www.kiwi.com/en/search/results/{ORIGIN}/{dest}/{dep_short}/{ret_short}",
        })
    return flights


def _browser_worker(
    task_q: queue.Queue,
    collector: list,
    lock: threading.Lock,
    done_ctr: list,
    total: int,
    worker_id: int,
) -> None:
    """
    One worker thread. Opens ONE Chromium instance, handles GDPR once,
    then processes all its tasks by navigating to successive Google Flights URLs.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    with sync_playwright() as pw:
        browser   = pw.chromium.launch(headless=True)
        ctx       = browser.new_context(locale="en-US")
        page      = ctx.new_page()
        gdpr_done = False

        while True:
            try:
                dest, dep_date, ret_date = task_q.get(block=True, timeout=10)
            except queue.Empty:
                break

            found: list[dict] = []
            try:
                tfs = _build_tfs(dest, dep_date, ret_date)
                page.goto(
                    f"https://www.google.com/travel/flights?tfs={tfs}&hl=en",
                    timeout=30000,
                    wait_until="domcontentloaded",
                )

                # Accept GDPR consent once — the cookie persists for the browser context
                if not gdpr_done and "consent.google.com" in page.url:
                    try:
                        page.click('text="Accept all"', timeout=5000)
                        page.wait_for_url("*google.com/travel*", timeout=10000)
                    except PWTimeout:
                        pass
                    gdpr_done = True

                # Wait for flight result cards to appear
                try:
                    page.wait_for_selector(".eQ35Ce", timeout=12000)
                except PWTimeout:
                    pass  # no flights for this route/date — normal
                else:
                    html  = page.inner_html('[role="main"]')
                    found = _parse_flights_from_html(html, dest, dep_date, ret_date)
                    if found:
                        with lock:
                            collector.extend(found)

            except Exception as exc:
                log.debug("W%d error %s %s: %s", worker_id, dest, dep_date, exc)
            finally:
                with lock:
                    done_ctr[0] += 1
                    n = done_ctr[0]
                if found:
                    log.info("  [%d/%d] %s %s→%s → %d deal(s)", n, total, dest, dep_date, ret_date, len(found))
                elif n % 100 == 0:
                    log.info("  [%d/%d] progress…", n, total)
                task_q.task_done()

        browser.close()


def search_all_flights() -> list[dict]:
    all_hubs = [code for code, _, _ in ASIA_HUBS]
    tasks    = [(code, dep, ret) for code in all_hubs for dep, ret in TRIP_PAIRS]
    total    = len(tasks)
    log.info(
        "Searching %d Asia hubs × %d dates = %d queries (workers: %d, persistent browsers)…",
        len(all_hubs), len(TRIP_PAIRS), total, MAX_WORKERS,
    )

    task_q   = queue.Queue()
    for t in tasks:
        task_q.put(t)

    collector: list       = []
    lock      = threading.Lock()
    done_ctr  = [0]

    threads = [
        threading.Thread(
            target=_browser_worker,
            args=(task_q, collector, lock, done_ctr, total, i),
            daemon=True,
        )
        for i in range(MAX_WORKERS)
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    # Deduplicate by (airport, departure, airlines, stops) and keep cheapest
    best: dict[str, dict] = {}
    for f in collector:
        key = _flight_key(f)
        if key not in best or f["price"] < best[key]["price"]:
            best[key] = f

    unique = sorted(best.values(), key=lambda x: x["price"])
    log.info("Total unique offers: %d", len(unique))
    return unique


# ── Cache ──────────────────────────────────────────────────────────────────────

def _flight_key(f: dict) -> str:
    return f"{f['airport']}|{f['departure'][:10]}|{f['return']}|{f['airlines']}|{f['stops']}"


def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(cache: dict) -> None:
    CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Text output ────────────────────────────────────────────────────────────────

def format_text_table(flights: list[dict]) -> str:
    if not flights:
        return "No flights found matching your criteria."
    header = (
        f"{'#':>3}  {'Price':>7}  {'Destination':<22}  {'Country':<18}  "
        f"{'Depart':<10}  {'Return':<10}  {'Stay':>4}  {'Dur':>5}  {'Stops':>5}  Airlines"
    )
    sep  = "-" * 125
    rows = []
    for i, f in enumerate(flights, 1):
        stop = "Direct" if f["stops"] == 0 else f"{f['stops']}×stop"
        dep  = f["departure"][:10]
        ret  = f["return"]
        rows.append(
            f"{i:>3}  €{f['price']:>6.0f}  {f['city']:<22}  {f['country']:<18}  "
            f"{dep:<10}  {ret:<10}  {f['stay_days']:>3}d  {f['duration_h']:>4.1f}h  {stop:>5}  {f['airlines']}"
        )
    return "\n".join([header, sep] + rows)


# ── HTML output ────────────────────────────────────────────────────────────────

def format_html(flights: list[dict], new_keys: set[str]) -> str:
    now_str   = datetime.now().strftime("%A, %B %d, %Y %H:%M")
    new_count = len(new_keys)

    rows: list[str] = []
    for i, f in enumerate(flights, 1):
        is_new   = _flight_key(f) in new_keys
        row_bg   = "#F0FFF4" if is_new else ("#FFFBF5" if i % 2 == 0 else "#FFFFFF")
        badge    = (
            ' <span style="background:#16A34A;color:#fff;font-size:10px;'
            'padding:2px 6px;border-radius:10px;font-weight:700">NEW</span>'
        ) if is_new else ""
        n_stops  = f["stops"]
        stop_str = "Direct" if n_stops == 0 else f"{n_stops} stop{'s' if n_stops > 1 else ''}"
        book_btn = (
            f'<a href="{html_lib.escape(f["url"])}" target="_blank" '
            f'style="background:{_ACCENT};color:#fff;padding:5px 10px;border-radius:6px;'
            f'text-decoration:none;font-size:12px;font-weight:600">Search ↗</a>'
        )
        rows.append(
            f'<tr style="background:{row_bg}">'
            f'<td style="padding:10px 12px;font-weight:700;color:{_ACCENT}">{i}</td>'
            f'<td style="padding:10px 12px;font-size:17px;font-weight:800;color:{_ACCENT}">€{f["price"]:.0f}</td>'
            f'<td style="padding:10px 12px"><strong>{html_lib.escape(f["city"])}</strong>{badge}<br>'
            f'<span style="font-size:11px;color:#888">{html_lib.escape(f["country"])} '
            f'({html_lib.escape(f["airport"])})</span></td>'
            f'<td style="padding:10px 12px;font-size:13px">{html_lib.escape(f["departure"][:10])}</td>'
            f'<td style="padding:10px 12px;font-size:13px">{html_lib.escape(f["return"])}</td>'
            f'<td style="padding:10px 12px;font-size:13px;text-align:center">{f["stay_days"]}d</td>'
            f'<td style="padding:10px 12px;font-size:13px;text-align:center">{f["duration_h"]}h</td>'
            f'<td style="padding:10px 12px;font-size:13px;text-align:center">{html_lib.escape(stop_str)}</td>'
            f'<td style="padding:10px 12px;font-size:12px;color:#555">{html_lib.escape(f["airlines"])}</td>'
            f'<td style="padding:10px 12px">{book_btn}</td>'
            "</tr>"
        )

    new_banner = (
        f'<p style="background:#DCFCE7;border:1px solid #86EFAC;border-radius:8px;'
        f'padding:10px 16px;color:#166534;font-weight:600;margin-bottom:16px">'
        f'🆕 {new_count} new flight option{"s" if new_count != 1 else ""} '
        f'detected since last check!</p>'
    ) if new_count else ""

    empty_row = (
        '<tr><td colspan="10" style="padding:24px;text-align:center;color:#bbb">'
        "No flights found matching your criteria.</td></tr>"
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Flight Deals – OPO → Asia &amp; LatAm</title>
</head>
<body style="background:#FDF8F2;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
             color:#1C1008;margin:0;padding:0">
<div style="max-width:980px;margin:0 auto;padding:24px 14px">

  <div style="background:linear-gradient(135deg,{_ACCENT2} 0%,{_ACCENT} 60%,#EA580C 100%);
              color:#fff;border-radius:18px;padding:34px 36px;text-align:center;margin-bottom:22px;
              box-shadow:0 6px 24px rgba(124,45,18,.28)">
    <h1 style="font-size:28px;font-weight:800;margin:0 0 8px">&#9992;&#65039; Flight Deals: OPO &#8594; Asia &amp; LatAm</h1>
    <p style="margin:0;opacity:.88;font-size:14px">
      Sep&ndash;Oct 2026 &bull; Round-trip &bull; Min {MIN_STAY}-day stay &bull;
      Max &euro;{MAX_PRICE} &bull; Max {MAX_STOPS} stop/direction &bull; {now_str}
    </p>
  </div>

  {new_banner}

  <p style="color:#555;font-size:14px;margin-bottom:16px">
    Found <strong>{len(flights)}</strong> round-trip deal{"s" if len(flights) != 1 else ""}
    sorted by price. Prices are round-trip per adult (OPO &#8644; destination),
    minimum {MIN_STAY}-day stay. "Search" links open Kiwi.com with the exact dates.
  </p>

  <div style="overflow-x:auto">
  <table style="width:100%;border-collapse:collapse;background:#fff;border-radius:12px;
                overflow:hidden;box-shadow:0 2px 14px rgba(194,65,12,.1)">
    <thead>
      <tr style="background:linear-gradient(90deg,{_ACCENT2} 0%,{_ACCENT} 100%);color:#fff">
        <th style="padding:12px 10px;font-size:12px;text-align:left">#</th>
        <th style="padding:12px 10px;font-size:12px;text-align:left">Price</th>
        <th style="padding:12px 10px;font-size:12px;text-align:left">Destination</th>
        <th style="padding:12px 10px;font-size:12px;text-align:left">Depart</th>
        <th style="padding:12px 10px;font-size:12px;text-align:left">Return</th>
        <th style="padding:12px 10px;font-size:12px;text-align:center">Stay</th>
        <th style="padding:12px 10px;font-size:12px;text-align:center">Dur</th>
        <th style="padding:12px 10px;font-size:12px;text-align:center">Stops</th>
        <th style="padding:12px 10px;font-size:12px;text-align:left">Airlines</th>
        <th style="padding:12px 10px;font-size:12px;text-align:left">Book</th>
      </tr>
    </thead>
    <tbody>
{"".join(rows) if rows else empty_row}
    </tbody>
  </table>
  </div>

  <p style="text-align:center;font-size:11px;color:#C9A882;padding:20px 0;
            border-top:1px solid #F0E0D0;margin-top:20px">
    Prices from Google Flights via fast-flights &bull;
    Booking links open Kiwi.com for that exact route &bull;
    Always verify before purchasing
  </p>
</div>
</body>
</html>"""


# ── Email ──────────────────────────────────────────────────────────────────────

def send_email(html: str, subject: str) -> None:
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    to_addr   = os.environ.get("TO_EMAIL", smtp_user)

    if not smtp_user or not smtp_pass:
        log.error(
            "Missing SMTP_USER / SMTP_PASS.\n"
            "  Set SMTP_USER=you@gmail.com and SMTP_PASS=<App Password> then re-run with --email."
        )
        sys.exit(1)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = smtp_user
    msg["To"]      = to_addr
    msg.attach(MIMEText(html, "html", "utf-8"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx) as server:
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, to_addr, msg.as_string())
    log.info("Email sent → %s", to_addr)


# ── Cron ───────────────────────────────────────────────────────────────────────

def install_cron(script: Path) -> None:
    python   = sys.executable
    log_file = script.parent / "flight_search.log"
    entries  = [
        f"0  8 * * * {python} {script.resolve()} --email >> {log_file} 2>&1",
        f"0 20 * * * {python} {script.resolve()} --email >> {log_file} 2>&1",
    ]
    result   = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    existing = result.stdout if result.returncode == 0 else ""

    if str(script.resolve()) in existing:
        log.info("Cron job already installed.")
        return

    new_tab = existing.rstrip("\n") + "\n" + "\n".join(entries) + "\n"
    proc = subprocess.run(["crontab", "-"], input=new_tab, text=True, capture_output=True)
    if proc.returncode == 0:
        print("✅  Cron jobs installed (8 AM + 8 PM daily):")
        for e in entries:
            print(f"    {e}")
        print("\nTip: run  crontab -l  to verify.")
    else:
        log.error("Failed to install cron job:\n%s", proc.stderr)
        sys.exit(1)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Search cheap flights OPO → Asia & Latin America (no sign-up needed).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--email",        action="store_true",
                        help="Send email alert when new flights are found")
    parser.add_argument("--force",        action="store_true",
                        help="Send email even if no new flights (use with --email)")
    parser.add_argument("--install-cron", action="store_true",
                        help="Install twice-daily cron job (8 AM + 8 PM)")
    parser.add_argument("--out", default="flights.html", metavar="FILE",
                        help="Output HTML file (default: flights.html)")
    args = parser.parse_args()

    if args.install_cron:
        install_cron(Path(__file__))
        return

    log.info("=== Flight Search: OPO → Asia & LatAm  |  Sep–Oct 2026 ===")

    flights = search_all_flights()
    log.info("Total: %d flights under €%d with ≤%d stop(s)", len(flights), MAX_PRICE, MAX_STOPS)

    print("\n" + format_text_table(flights) + "\n")

    cache        = load_cache()
    cached_keys  = set(cache.get("seen_keys", []))
    current_keys = {_flight_key(f) for f in flights}
    new_keys     = current_keys - cached_keys

    if new_keys:
        log.info("🆕 %d new option(s) detected since last run!", len(new_keys))
    else:
        log.info("No new options since last run (cache: %d keys).", len(cached_keys))

    html = format_html(flights, new_keys)
    out  = Path(args.out)
    out.write_text(html, encoding="utf-8")
    log.info("HTML saved → %s", out.resolve())

    if args.email:
        if new_keys or args.force:
            n = len(new_keys)
            subject = (
                f"✈️ {n} New Flight Deal{'s' if n != 1 else ''}! OPO → Asia/LatAm (Sep–Oct 2026)"
                if new_keys else
                f"✈️ Flight Update: {len(flights)} deals under €{MAX_PRICE} from OPO"
            )
            send_email(html, subject)
        else:
            log.info("No new flights – skipping email. Use --force to send anyway.")

    cache["seen_keys"]    = list(current_keys)
    cache["last_run"]     = datetime.now().isoformat()
    cache["flight_count"] = len(flights)
    save_cache(cache)
    log.info("Cache updated (%d keys).", len(current_keys))


if __name__ == "__main__":
    main()
