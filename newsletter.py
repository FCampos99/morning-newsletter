#!/usr/bin/env python3
"""
Morning Newsletter – Póvoa de Varzim Edition
=============================================
Usage:
    python newsletter.py                 # generate HTML → newsletter.html
    python newsletter.py --email         # generate + send via Gmail SMTP
    python newsletter.py --install-cron  # install cron job at 7:30 AM

Env vars (for --email):
    SMTP_USER   your Gmail address
    SMTP_PASS   Gmail App Password (not your login password)
    TO_EMAIL    recipient address (defaults to SMTP_USER)
"""
from __future__ import annotations

import argparse
import html as html_lib
import logging
import os
import re
import smtplib
import ssl
import subprocess
import sys
from datetime import datetime
from difflib import SequenceMatcher
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from textwrap import shorten
from zoneinfo import ZoneInfo

import feedparser
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
POVOA_LAT = 41.3808   # Póvoa de Varzim
POVOA_LON = -8.7616
TIMEZONE  = "Europe/Lisbon"
MAX_ITEMS = 5      # articles per topic
TIMEOUT   = 12     # HTTP timeout in seconds

# Teams to track: last result only (TheSportsDB free API — eventsnext is unreliable on free tier)
TRACKED_TEAMS = [
    "Varzim",
    "FC Barcelona",
    "Boston Celtics",
]

TOPICS: dict[str, list[str]] = {
    # ── World news first ───────────────────────────────────────────────────────
    "🌍 World News": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
    ],
    # Portuguese outlets; keyword filter below ensures articles are *about* Portugal
    "🇵🇹 Portugal": [
        "https://noticias.rtp.pt/feed/",
        "https://eco.sapo.pt/feed/",
        "https://observador.pt/feed/",
    ],
    # ── Sports ─────────────────────────────────────────────────────────────────
    "🏀 NBA": [
        "https://www.espn.com/espn/rss/nba/news",
        "https://news.google.com/rss/search?q=NBA+basketball&hl=en-US&gl=US&ceid=US:en",
    ],
    "⚽ Football": [
        "https://feeds.bbci.co.uk/sport/football/rss.xml",
        "https://theathletic.com/rss.xml",
        "https://www.reddit.com/r/soccer.rss",
        "https://www.livescore.com/en/news/rss.xml",   # falls back gracefully if unavailable
    ],
    # Google News site: searches pull only from each outlet, filtered for Varzim/Liga 3
    # Twitter/X option (self-hosted rsshub needed):
    # add "https://rsshub.app/twitter/user/varzim_sc" to the list below
    "Varzim & Liga 3": [
        "https://news.google.com/rss/search?q=Varzim+OR+%22Liga+3%22+site:abola.pt&hl=pt-PT&gl=PT&ceid=PT:pt",
        "https://news.google.com/rss/search?q=Varzim+OR+%22Liga+3%22+site:ojogo.pt&hl=pt-PT&gl=PT&ceid=PT:pt",
        "https://news.google.com/rss/search?q=Varzim+OR+%22Liga+3%22+site:record.pt&hl=pt-PT&gl=PT&ceid=PT:pt",
        "https://news.google.com/rss/search?q=Varzim+site:zerozero.pt&hl=pt-PT&gl=PT&ceid=PT:pt",
    ],
    # ── Tech ───────────────────────────────────────────────────────────────────
    "🤖 Tech & AI": [
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
        "https://news.google.com/rss/search?q=artificial+intelligence+AI+LLM+tech+trends&hl=en-US&gl=US&ceid=US:en",
    ],
}

# Topics that need opinion/editorial articles filtered out (all of them)
_NO_OPINION_TOPICS = set(TOPICS.keys())

# Per-topic keyword filter: at least one keyword must appear in title+summary
TOPIC_KEYWORD_FILTER: dict[str, list[str]] = {
    "🇵🇹 Portugal": ["portugal", "portuguese", "português", "portuguesa", "lisboa", "lisbon"],
}

# WMO weather interpretation codes → (description, emoji)
WMO: dict[int, tuple[str, str]] = {
    0:  ("Clear sky",            "☀️"),
    1:  ("Mainly clear",         "🌤️"),
    2:  ("Partly cloudy",        "⛅"),
    3:  ("Overcast",             "☁️"),
    45: ("Foggy",                "🌫️"),
    48: ("Depositing rime fog",  "🌫️"),
    51: ("Light drizzle",        "🌦️"),
    53: ("Moderate drizzle",     "🌦️"),
    55: ("Dense drizzle",        "🌧️"),
    61: ("Slight rain",          "🌧️"),
    63: ("Moderate rain",        "🌧️"),
    65: ("Heavy rain",           "🌧️"),
    71: ("Slight snow",          "🌨️"),
    73: ("Moderate snow",        "❄️"),
    75: ("Heavy snow",           "❄️"),
    80: ("Slight showers",       "🌦️"),
    81: ("Showers",              "🌧️"),
    82: ("Violent showers",      "⛈️"),
    95: ("Thunderstorm",         "⛈️"),
    96: ("Thunderstorm + hail",  "⛈️"),
    99: ("Severe thunderstorm",  "⛈️"),
}

# ── Weather ────────────────────────────────────────────────────────────────────

def fetch_weather() -> dict:
    """Fetch current conditions + daily summary for Póvoa de Varzim via Open-Meteo."""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={POVOA_LAT}&longitude={POVOA_LON}"
        "&current=temperature_2m,apparent_temperature,relative_humidity_2m,"
        "precipitation,weather_code,wind_speed_10m,uv_index"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,"
        "precipitation_probability_max,sunrise,sunset"
        f"&timezone={TIMEZONE}&forecast_days=1"
    )
    try:
        resp = requests.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        data  = resp.json()
        c     = data["current"]
        daily = data["daily"]
        code  = int(c.get("weather_code", 0))
        desc, emoji = WMO.get(code, ("Unknown", "🌡️"))
        uv = c.get("uv_index")
        return {
            "temp":        round(c["temperature_2m"]),
            "feels_like":  round(c["apparent_temperature"]),
            "humidity":    int(c["relative_humidity_2m"]),
            "wind":        round(c["wind_speed_10m"]),
            "uv":          round(uv, 1) if uv is not None else "–",
            "description": desc,
            "emoji":       emoji,
            "temp_max":    round(daily["temperature_2m_max"][0]),
            "temp_min":    round(daily["temperature_2m_min"][0]),
            "precip_prob": int(daily["precipitation_probability_max"][0]),
            "precip_sum":  daily["precipitation_sum"][0],
            "sunrise":     daily["sunrise"][0].split("T")[-1],
            "sunset":      daily["sunset"][0].split("T")[-1],
        }
    except Exception as exc:
        log.error("Weather fetch failed: %s", exc)
        return {}

# ── Sports Fixtures ────────────────────────────────────────────────────────────

_SPORTSDB = "https://www.thesportsdb.com/api/v1/json/3"


def _format_event(ev: dict | None) -> dict | None:
    if not ev:
        return None
    home_score = ev.get("intHomeScore")
    away_score = ev.get("intAwayScore")
    has_score  = home_score not in (None, "") and away_score not in (None, "")
    return {
        "home":        ev.get("strHomeTeam", ""),
        "away":        ev.get("strAwayTeam", ""),
        "home_score":  str(home_score) if has_score else "",
        "away_score":  str(away_score) if has_score else "",
        "date":        ev.get("dateEvent", ""),
        "time":        (ev.get("strTime") or "")[:5],
        "competition": ev.get("strLeague", ""),
        "venue":       ev.get("strVenue", ""),
    }


def fetch_team_fixtures(team_name: str) -> dict:
    """Return last result for a team via TheSportsDB free API."""
    try:
        r = requests.get(
            f"{_SPORTSDB}/searchteams.php",
            params={"t": team_name},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        teams = r.json().get("teams") or []
        if not teams:
            log.warning("Team not found in TheSportsDB: %s", team_name)
            return {"name": team_name, "badge": "", "last": None}

        team    = teams[0]
        team_id = team["idTeam"]
        badge   = team.get("strTeamBadge", "")
        sport   = team.get("strSport", "")

        last_r      = requests.get(f"{_SPORTSDB}/eventslast.php", params={"id": team_id}, timeout=TIMEOUT)
        last_events = last_r.json().get("results") or []

        return {
            "name":  team.get("strTeam", team_name),
            "badge": badge,
            "sport": sport,
            "last":  _format_event(last_events[-1] if last_events else None),
        }
    except Exception as exc:
        log.warning("Fixtures fetch failed for %s: %s", team_name, exc)
        return {"name": team_name, "badge": "", "sport": "", "last": None}


def fetch_all_fixtures() -> list[dict]:
    fixtures = []
    for name in TRACKED_TEAMS:
        log.info("  Fixtures: %s", name)
        fixtures.append(fetch_team_fixtures(name))
    return fixtures

# ── News ───────────────────────────────────────────────────────────────────────

_OPINION_RE = re.compile(
    r"^\s*(opinion|op[\.\-]?ed|column(ist)?|commentary|editorial|"
    r"letters?\s+to|perspective|viewpoint|essay|my\s+take|"
    r"your\s+view|readers?\s+respond)\s*[:\|–—]",
    re.IGNORECASE,
)


def _is_opinion(item: dict) -> bool:
    if _OPINION_RE.match(item["title"]):
        return True
    for tag in item.get("tags", []):
        if re.search(r"\b(opinion|editorial|op.?ed|commentary|column)\b", tag, re.IGNORECASE):
            return True
    return False


def _matches_keywords(item: dict, keywords: list[str]) -> bool:
    text = (item["title"] + " " + item["summary"]).lower()
    return any(kw in text for kw in keywords)


def _similar_title(a: str, b: str, threshold: float = 0.75) -> bool:
    a, b = a.lower().strip(), b.lower().strip()
    if a == b:
        return True
    return SequenceMatcher(None, a, b).ratio() >= threshold


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def fetch_feed(url: str) -> list[dict]:
    try:
        feed = feedparser.parse(url, agent="MorningNewsletter/1.0 (personal digest bot)")
        items: list[dict] = []
        for entry in feed.entries:
            tags = [t.get("term", "") for t in entry.get("tags", []) if t.get("term")]
            raw_summary = _strip_tags(entry.get("summary", ""))
            items.append({
                "title":   _strip_tags(entry.get("title", "Untitled")).strip(),
                "link":    entry.get("link", "#"),
                "summary": shorten(raw_summary.strip(), width=220, placeholder="…"),
                "source":  feed.feed.get("title", ""),
                "date":    entry.get("published", ""),
                "tags":    tags,
            })
        return items
    except Exception as exc:
        log.warning("Feed error [%s…]: %s", url[:55], exc)
        return []


def fetch_all_news() -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for topic, urls in TOPICS.items():
        filter_opinions = topic in _NO_OPINION_TOPICS
        kw_filter       = TOPIC_KEYWORD_FILTER.get(topic)
        articles: list[dict] = []

        for url in urls:
            for item in fetch_feed(url):
                if filter_opinions and _is_opinion(item):
                    continue
                if kw_filter and not _matches_keywords(item, kw_filter):
                    continue
                if any(_similar_title(item["title"], a["title"]) for a in articles):
                    continue
                articles.append(item)
                if len(articles) >= MAX_ITEMS:
                    break
            if len(articles) >= MAX_ITEMS:
                break

        result[topic] = articles
        log.info("  %-30s %d articles", topic, len(articles))
    return result

# ── HTML Rendering ─────────────────────────────────────────────────────────────

_ACCENT  = "#C2410C"
_ACCENT2 = "#7C2D12"
_LIGHT   = "#FFF7ED"
_BORDER  = "#FED7AA"


def _css() -> str:
    return f"""
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  background: #FDF8F2;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
  color: #1C1008;
  line-height: 1.6;
}}
.wrapper {{ max-width: 640px; margin: 0 auto; padding: 24px 14px; }}

/* ── Header ─────────────────────────────────────────────────────────────── */
.hdr {{
  background: linear-gradient(135deg, {_ACCENT2} 0%, {_ACCENT} 60%, #EA580C 100%);
  color: #fff;
  border-radius: 18px;
  padding: 34px 36px;
  text-align: center;
  margin-bottom: 22px;
  box-shadow: 0 6px 24px rgba(124,45,18,.28);
}}
.hdr h1    {{ font-size: 30px; font-weight: 800; margin-bottom: 8px; letter-spacing: -.4px; }}
.hdr .date {{ font-size: 14px; opacity: .88; }}
.hdr .city {{ font-size: 11px; opacity: .6; margin-top: 5px; letter-spacing: 1.2px; text-transform: uppercase; }}

/* ── Weather card ───────────────────────────────────────────────────────── */
.wx {{
  background: linear-gradient(135deg, #FFF7ED 0%, #FFEDD5 100%);
  border-radius: 16px;
  padding: 24px 28px;
  margin-bottom: 22px;
  border: 1px solid {_BORDER};
  box-shadow: 0 3px 14px rgba(194,65,12,.10);
}}
.wx-top  {{ display: flex; align-items: center; gap: 20px; flex-wrap: wrap; }}
.wx-icon {{ font-size: 64px; line-height: 1; }}
.wx-info {{ flex: 1; min-width: 140px; }}
.wx-temp {{ font-size: 50px; font-weight: 800; color: {_ACCENT}; line-height: 1; }}
.wx-desc {{ font-size: 15px; color: #78350F; margin-top: 6px; }}
.wx-loc  {{ font-size: 11px; color: #92400E; opacity: .7; margin-top: 4px; letter-spacing: .4px; }}
.wx-grid {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; }}
.wx-stat {{
  flex: 1; min-width: 100px;
  background: rgba(255,255,255,.75);
  border-radius: 10px;
  padding: 10px 12px;
  text-align: center;
  border: 1px solid {_BORDER};
}}
.wx-stat .lbl {{ font-size: 10px; color: #92400E; text-transform: uppercase; letter-spacing: .7px; font-weight: 600; }}
.wx-stat .val {{ font-size: 17px; font-weight: 700; color: {_ACCENT}; margin-top: 3px; }}

/* ── Fixtures ───────────────────────────────────────────────────────────── */
.fixtures-label {{
  font-size: 11px; font-weight: 700; color: {_ACCENT};
  text-transform: uppercase; letter-spacing: 1px;
  margin-bottom: 10px; padding-left: 2px;
}}
.fixtures-grid {{
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 14px;
  margin-bottom: 22px;
}}
.team-card {{
  background: #fff;
  border-radius: 14px;
  border: 1px solid #F3E4D4;
  box-shadow: 0 2px 10px rgba(194,65,12,.07);
  overflow: hidden;
}}
.team-card-hdr {{
  background: linear-gradient(90deg, {_ACCENT2} 0%, {_ACCENT} 100%);
  color: #fff;
  padding: 11px 14px;
  display: flex;
  align-items: center;
  gap: 9px;
  min-height: 52px;
}}
.team-badge {{
  width: 30px; height: 30px;
  object-fit: contain;
  background: rgba(255,255,255,.15);
  border-radius: 50%;
  padding: 3px;
  flex-shrink: 0;
}}
.team-name {{ font-size: 12.5px; font-weight: 700; line-height: 1.25; }}
.fix-block {{ padding: 10px 13px; border-bottom: 1px solid #FDF0E6; }}
.fix-block:last-child {{ border-bottom: none; }}
.fix-label {{
  font-size: 9.5px; font-weight: 800; color: {_ACCENT};
  text-transform: uppercase; letter-spacing: .6px; margin-bottom: 5px;
}}
.fix-teams {{ font-size: 12px; font-weight: 600; color: #1C1008; line-height: 1.35; }}
.fix-score {{ font-size: 20px; font-weight: 800; color: {_ACCENT}; margin: 3px 0 1px; line-height: 1; }}
.fix-sub   {{ font-size: 10.5px; color: #BBA898; margin-top: 3px; line-height: 1.3; }}
.fix-na    {{ font-size: 11px; color: #CCC; font-style: italic; }}

/* ── Section cards ──────────────────────────────────────────────────────── */
.section {{
  background: #FFFFFF;
  border-radius: 16px;
  margin-bottom: 22px;
  border: 1px solid #F3E4D4;
  box-shadow: 0 2px 14px rgba(194,65,12,.07);
  overflow: hidden;
}}
.sec-hdr {{
  background: linear-gradient(90deg, {_ACCENT2} 0%, {_ACCENT} 100%);
  color: #fff;
  padding: 14px 22px;
  font-size: 15px;
  font-weight: 700;
  letter-spacing: .3px;
}}
.article {{
  padding: 15px 22px 15px 25px;
  border-bottom: 1px solid #FDF0E6;
  border-left: 3px solid transparent;
}}
.article:last-child {{ border-bottom: none; }}
.article:hover {{ border-left-color: {_ACCENT}; background: #FFFBF7; }}
.art-num {{
  display: inline-block;
  font-size: 10px; font-weight: 700; color: #fff;
  background: {_ACCENT}; border-radius: 50%;
  width: 18px; height: 18px; line-height: 18px;
  text-align: center; margin-right: 8px; vertical-align: middle;
}}
.art-title a {{
  font-size: 14.5px; font-weight: 600; color: #1C1008;
  text-decoration: none; line-height: 1.45;
}}
.art-title a:hover {{ color: {_ACCENT}; }}
.art-meta    {{ font-size: 11px; color: #BBA898; margin-top: 5px; padding-left: 26px; }}
.art-summary {{ font-size: 13px; color: #7A6357; margin-top: 6px; padding-left: 26px; line-height: 1.55; }}

/* ── Footer ─────────────────────────────────────────────────────────────── */
.footer {{
  text-align: center; font-size: 12px;
  color: #C9A882; padding: 14px 0 28px;
  border-top: 1px solid #F0E0D0; margin-top: 6px;
}}
@media (max-width: 480px) {{
  .fixtures-grid {{ grid-template-columns: 1fr; }}
  .hdr {{ padding: 24px 20px; }}
  .hdr h1 {{ font-size: 24px; }}
}}
"""


def _render_weather(w: dict) -> str:
    if not w:
        return '<p style="text-align:center;color:#bbb;padding:24px">Weather data unavailable.</p>'
    return (
        '<div class="wx">'
        '<div class="wx-top">'
        f'<div class="wx-icon">{w["emoji"]}</div>'
        '<div class="wx-info">'
        f'<div class="wx-temp">{w["temp"]}°C</div>'
        f'<div class="wx-desc">{html_lib.escape(w["description"])} &bull; Feels like {w["feels_like"]}°C</div>'
        '<div class="wx-loc">P&oacute;voa de Varzim, Portugal</div>'
        "</div></div>"
        '<div class="wx-grid">'
        f'<div class="wx-stat"><div class="lbl">High / Low</div><div class="val">{w["temp_max"]}° / {w["temp_min"]}°</div></div>'
        f'<div class="wx-stat"><div class="lbl">Humidity</div><div class="val">{w["humidity"]}%</div></div>'
        f'<div class="wx-stat"><div class="lbl">Wind</div><div class="val">{w["wind"]} km/h</div></div>'
        f'<div class="wx-stat"><div class="lbl">UV Index</div><div class="val">{w["uv"]}</div></div>'
        f'<div class="wx-stat"><div class="lbl">Rain Chance</div><div class="val">{w["precip_prob"]}%</div></div>'
        f'<div class="wx-stat"><div class="lbl">Sunrise / Sunset</div><div class="val">{w["sunrise"]} / {w["sunset"]}</div></div>'
        "</div></div>"
    )


def _render_last_result(ev: dict | None) -> str:
    if not ev:
        return '<div class="fix-block"><div class="fix-na">No data</div></div>'
    home  = html_lib.escape(ev["home"])
    away  = html_lib.escape(ev["away"])
    comp  = html_lib.escape(ev["competition"])
    date  = ev["date"]
    sub   = " &bull; ".join(filter(None, [comp, date]))
    score = (
        f'<div class="fix-score">{ev["home_score"]} – {ev["away_score"]}</div>'
        if ev["home_score"] != "" else ""
    )
    return (
        '<div class="fix-block">'
        f'<div class="fix-label">Last result</div>'
        f'<div class="fix-teams">{home} vs {away}</div>'
        f'{score}'
        + (f'<div class="fix-sub">{sub}</div>' if sub else "")
        + '</div>'
    )


def _render_team_card(team: dict) -> str:
    badge_html = (
        f'<img class="team-badge" src="{team["badge"]}" alt="">'
        if team.get("badge") else ""
    )
    name = html_lib.escape(team["name"])
    return (
        '<div class="team-card">'
        f'<div class="team-card-hdr">{badge_html}<span class="team-name">{name}</span></div>'
        f'{_render_last_result(team.get("last"))}'
        '</div>'
    )


def _render_fixtures(fixtures: list[dict]) -> str:
    if not fixtures:
        return ""
    cards = "\n".join(_render_team_card(t) for t in fixtures)
    return (
        '<div class="fixtures-label">Sport Fixtures</div>'
        f'<div class="fixtures-grid">{cards}</div>'
    )


def _render_topic(topic: str, articles: list[dict]) -> str:
    if not articles:
        body = '<div class="article"><p style="color:#ccc;font-size:13px">No articles found today.</p></div>'
    else:
        rows: list[str] = []
        for i, a in enumerate(articles, 1):
            title   = html_lib.escape(a["title"])
            source  = html_lib.escape(a["source"])
            summary = html_lib.escape(a["summary"])
            date_txt = a["date"][:16] if a["date"] else ""
            meta = " &bull; ".join(filter(None, [source, date_txt]))
            rows.append(
                '<div class="article">'
                '<div class="art-title">'
                f'<span class="art-num">{i}</span>'
                f'<a href="{a["link"]}" target="_blank">{title}</a>'
                "</div>"
                + (f'<div class="art-meta">{meta}</div>' if meta else "")
                + (f'<div class="art-summary">{summary}</div>' if summary else "")
                + "</div>"
            )
        body = "\n".join(rows)
    return (
        '<div class="section">'
        f'<div class="sec-hdr">{topic}</div>'
        f"{body}"
        "</div>"
    )


def render_html(
    weather: dict,
    fixtures: list[dict],
    news: dict[str, list[dict]],
    now: datetime,
) -> str:
    date_str = now.strftime("%A, %B %d, %Y")
    time_str = now.strftime("%H:%M")
    hour     = now.hour
    greeting = (
        "Good morning" if hour < 12
        else "Good afternoon" if hour < 18
        else "Good evening"
    )

    head = (
        "<!DOCTYPE html>\n<html lang='en'>\n<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f"<title>Morning Newsletter – {date_str}</title>\n"
        f"<style>{_css()}</style>\n"
        "</head>\n<body>\n<div class='wrapper'>\n"
    )
    header = (
        "<div class='hdr'>"
        f"<h1>☀️ {greeting}!</h1>"
        f'<div class="date">{date_str}</div>'
        '<div class="city">P&oacute;voa de Varzim &bull; Your Daily Briefing</div>'
        "</div>\n"
    )
    weather_html  = _render_weather(weather)
    fixtures_html = _render_fixtures(fixtures)
    news_html     = "\n".join(_render_topic(t, a) for t, a in news.items())
    footer = (
        f"<div class='footer'>Generated at {time_str} &bull; "
        "Open-Meteo &amp; TheSportsDB &amp; RSS feeds</div>\n"
        "</div>\n</body>\n</html>"
    )
    return head + header + weather_html + "\n" + fixtures_html + "\n" + news_html + "\n" + footer

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
    log_file = script.parent / "newsletter.log"
    entry    = f"0 9 * * * {python} {script.resolve()} --email >> {log_file} 2>&1"

    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    existing = result.stdout if result.returncode == 0 else ""

    if str(script.resolve()) in existing:
        log.info("Cron job already installed.")
        return

    new_tab = existing.rstrip("\n") + "\n" + entry + "\n"
    proc = subprocess.run(["crontab", "-"], input=new_tab, text=True, capture_output=True)
    if proc.returncode == 0:
        print(f"✅  Cron job installed:\n    {entry}")
        print("\nTip: run  crontab -l  to verify.")
    else:
        log.error("Failed to install cron job:\n%s", proc.stderr)
        sys.exit(1)

# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a personalized morning newsletter.")
    parser.add_argument("--email",        action="store_true", help="Send via Gmail SMTP")
    parser.add_argument("--install-cron", action="store_true", help="Install cron job at 07:30")
    parser.add_argument("--out", default="newsletter.html", metavar="FILE",
                        help="Output HTML file (default: newsletter.html)")
    args = parser.parse_args()

    if args.install_cron:
        install_cron(Path(__file__))
        return

    now = datetime.now(ZoneInfo(TIMEZONE))
    log.info("=== Morning Newsletter  %s ===", now.strftime("%A %B %d, %Y  %H:%M"))

    log.info("Fetching weather for Póvoa de Varzim…")
    weather = fetch_weather()
    if weather:
        log.info(
            "  %s  %d°C (feels %d°C)  wind %d km/h  rain %d%%",
            weather["description"], weather["temp"], weather["feels_like"],
            weather["wind"], weather["precip_prob"],
        )

    log.info("Fetching sport fixtures…")
    fixtures = fetch_all_fixtures()

    log.info("Fetching news…")
    news = fetch_all_news()

    log.info("Rendering HTML…")
    html = render_html(weather, fixtures, news, now)

    out = Path(args.out)
    out.write_text(html, encoding="utf-8")
    log.info("Saved → %s", out.resolve())

    if args.email:
        subject = f"☀️ Morning Newsletter – {now.strftime('%A, %B %d')}"
        log.info("Sending email…")
        send_email(html, subject)


if __name__ == "__main__":
    main()
