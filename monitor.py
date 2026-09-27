#!/usr/bin/env python3
"""
TCG Drop Alert - watches product pages and pings you (phone push via ntfy + email)
the moment something becomes purchasable or a queue opens.

Usage:
  python monitor.py                 # one check of every product
  python monitor.py --loop 340      # keep checking for 340 minutes
  python monitor.py --test          # print what it sees for each product, no alerts
  python monitor.py --test-notify   # send a test push + email
"""
import argparse
import json
import os
import random
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlparse

import yaml

try:  # curl_cffi mimics a real Chrome browser's connection, which many shops require
    from curl_cffi import requests as http
    IMPERSONATE = {"impersonate": "chrome"}
except ImportError:  # plain requests still works for friendlier sites
    import requests as http
    IMPERSONATE = {}

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "products.yaml"
STATE_FILE = ROOT / "state.json"

# ---- statuses ---------------------------------------------------------------
IN_STOCK, PREORDER, QUEUE, FOUND = "IN_STOCK", "PREORDER", "QUEUE", "FOUND"
OUT_OF_STOCK, NOT_LISTED, BLOCKED, ERROR, UNKNOWN = (
    "OUT_OF_STOCK", "NOT_LISTED", "BLOCKED", "ERROR", "UNKNOWN")
ALERT_STATUSES = {IN_STOCK, PREORDER, QUEUE, FOUND}
BLOCKED_WARN_AFTER = 10  # consecutive blocked checks before warning you

# ---- text patterns ----------------------------------------------------------
QUEUE_PATTERNS = [
    r"queue-it", r"virtual queue", r"you are (now )?in (the )?(line|queue)",
    r"waiting room", r"your estimated wait time", r"you('| a)re in line",
    r"place in (the )?(line|queue)",
]
BLOCK_PATTERNS = [
    r"_incapsula_resource", r"incapsula incident", r"access denied",
    r"pardon our interruption", r"cf-chl", r"just a moment\.\.\.",
    r"are you a robot", r"request unsuccessful", r"captcha",
]
OUT_PATTERNS = [
    r"out of stock", r"sold out", r"currently unavailable", r"coming soon",
    r"notify me when (it'?s )?(back|available)", r"email me when available",
    r"not available (for|to) (delivery|buy|purchase)", r"no longer available",
]
IN_PATTERNS = [
    r"add to (basket|cart|bag|trolley)", r"buy now", r"pre-?order now",
]
AVAILABILITY_MAP = {
    "instock": IN_STOCK, "limitedavailability": IN_STOCK, "onlineonly": IN_STOCK,
    "instoreonly": OUT_OF_STOCK, "preorder": PREORDER, "presale": PREORDER,
    "backorder": PREORDER, "outofstock": OUT_OF_STOCK, "soldout": OUT_OF_STOCK,
    "discontinued": OUT_OF_STOCK,
}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def any_match(patterns, text):
    return next((p for p in patterns if re.search(p, text, re.I)), None)


def visible_text(html):
    """Drop scripts/styles/tags so words hidden in JS bundles don't fool us."""
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html)


def jsonld_availability(html):
    """Read schema.org 'availability' that most shops embed for Google Shopping."""
    found = []
    for block in re.findall(
            r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for k, v in node.items():
                    if k == "availability" and isinstance(v, str):
                        found.append(v.rsplit("/", 1)[-1].lower())
                    else:
                        stack.append(v)
            elif isinstance(node, list):
                stack.extend(node)
    statuses = [AVAILABILITY_MAP[a] for a in found if a in AVAILABILITY_MAP]
    for s in (IN_STOCK, PREORDER, OUT_OF_STOCK):  # best news wins
        if s in statuses:
            return s
    return None


def classify(product, status_code, final_url, html):
    """Return (status, reason)."""
    text = visible_text(html)
    lowered_host = urlparse(final_url).netloc.lower()

    if "queue" in lowered_host or any_match(QUEUE_PATTERNS, html):
        return QUEUE, f"queue page detected ({lowered_host})"
    if status_code in (404, 410):
        return NOT_LISTED, f"HTTP {status_code} - page not live yet"
    if status_code in (401, 403, 429) or status_code >= 500:
        return BLOCKED, f"HTTP {status_code}"
    if len(text) < 400 and any_match(BLOCK_PATTERNS, html):
        return BLOCKED, "bot-protection page"

    # Mode: watch a page (e.g. search results) for some text to appear
    if product.get("mode") == "text_appears":
        hit = next((t for t in product.get("watch_text", [])
                    if t.lower() in text.lower()), None)
        return (FOUND, f'"{hit}" appeared') if hit else (
            OUT_OF_STOCK, "watched text not on page yet")

    # Your own per-product phrases win over everything else
    if product.get("in_stock_text") and any(
            t.lower() in text.lower() for t in product["in_stock_text"]):
        return IN_STOCK, "matched your in_stock_text"
    if product.get("out_of_stock_text") and any(
            t.lower() in text.lower() for t in product["out_of_stock_text"]):
        return OUT_OF_STOCK, "matched your out_of_stock_text"

    ld = jsonld_availability(html)
    if ld:
        return ld, "structured data (schema.org availability)"

    out_hit = any_match(OUT_PATTERNS, text)
    if out_hit:
        return OUT_OF_STOCK, f'page says "{out_hit}"'
    in_hit = any_match(IN_PATTERNS, text)
    if in_hit:
        return IN_STOCK, f'buy button found ("{in_hit}")'
    if any_match(BLOCK_PATTERNS, html):
        return BLOCKED, "bot-protection page"
    return UNKNOWN, "couldn't tell - add in_stock_text/out_of_stock_text for this product"


def check(product):
    try:
        r = http.get(product["url"], headers=HEADERS, timeout=25,
                     allow_redirects=True, **IMPERSONATE)
        return classify(product, r.status_code, str(r.url), r.text)
    except Exception as e:  # network hiccup, DNS, timeout...
        return ERROR, f"{type(e).__name__}: {e}"[:200]


# ---- notifications ----------------------------------------------------------
def notify(title, message, url=None, urgent=True):
    sent = []
    topic = os.getenv("NTFY_TOPIC")
    if topic:
        server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        payload = {"topic": topic, "title": title, "message": message,
                   "priority": 5 if urgent else 3,
                   "tags": ["rotating_light"] if urgent else ["warning"]}
        if url:
            payload["click"] = url
            payload["actions"] = [{"action": "view", "label": "Open page", "url": url}]
        try:
            import requests
            requests.post(server, json=payload, timeout=15).raise_for_status()
            sent.append("push")
        except Exception as e:
            log(f"  ! push failed: {e}")

    sender, pw = os.getenv("GMAIL_ADDRESS"), os.getenv("GMAIL_APP_PASSWORD")
    if sender and pw:
        to = os.getenv("EMAIL_TO") or sender
        body = message + (f"\n\nGo: {url}" if url else "")
        mail = MIMEText(body, "plain", "utf-8")
        mail["Subject"], mail["From"], mail["To"] = title, sender, to
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
                s.login(sender, pw.replace(" ", ""))
                s.sendmail(sender, [a.strip() for a in to.split(",")], mail.as_string())
            sent.append("email")
        except Exception as e:
            log(f"  ! email failed: {e}")

    if not sent:
        log("  ! no notification channel configured (set NTFY_TOPIC and/or GMAIL_* )")
    return sent


# ---- state ------------------------------------------------------------------
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def run_pass(products, state, test=False):
    remind_every = 60 * float(os.getenv("REMIND_MINUTES", "20"))
    for p in products:
        name, url = p["name"], p["url"]
        status, reason = check(p)
        key = f"{name} | {url}"
        prev = state.get(key, {})
        prev_status = prev.get("status")
        log(f"{status:<12} {name}  ({reason})")
        if test:
            continue

        now = time.time()
        entry = {**prev, "name": name, "status": status, "reason": reason,
                 "checked": datetime.now(timezone.utc).isoformat(timespec="seconds")}

        if status in ALERT_STATUSES:
            first = prev_status not in ALERT_STATUSES
            reminder = (not first and now - prev.get("alerted_at", 0) >= remind_every
                        and prev.get("reminders", 0) < 3)
            if first or reminder:
                label = {QUEUE: "QUEUE IS OPEN", PREORDER: "PRE-ORDER LIVE",
                         FOUND: "SPOTTED", IN_STOCK: "IN STOCK"}[status]
                title = f"{label}: {name}" if first else f"Still live: {name}"
                notify(title, f"{name}\n{reason}\nChecked {entry['checked']} UTC", url)
                entry["alerted_at"] = now
                entry["reminders"] = 0 if first else prev.get("reminders", 0) + 1
        elif status in (OUT_OF_STOCK, NOT_LISTED):
            entry.pop("alerted_at", None)
            entry["reminders"] = 0

        entry["blocked_streak"] = prev.get("blocked_streak", 0) + 1 if status in (
            BLOCKED, ERROR) else 0
        if entry["blocked_streak"] == BLOCKED_WARN_AFTER:
            notify(f"Can't see {name}",
                   f"The site has blocked or failed the last {BLOCKED_WARN_AFTER} "
                   f"checks ({reason}). Alerts for this product may not work "
                   f"from here - see README 'If a site blocks the bot'.",
                   url, urgent=False)
        state[key] = entry
        time.sleep(random.uniform(1.5, 4))  # be polite between requests
    save_state(state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=float, default=0, help="keep running for N minutes")
    ap.add_argument("--test", action="store_true", help="print statuses, no alerts")
    ap.add_argument("--test-notify", action="store_true")
    args = ap.parse_args()

    if args.test_notify:
        sent = notify("Test: TCG drop alert works",
                      "If you can read this, alerts will reach you.",
                      "https://www.pokemoncenter.com/en-gb")
        sys.exit(0 if sent else 1)

    config = yaml.safe_load(CONFIG_FILE.read_text()) or {}
    products = [p for p in config.get("products", []) if p.get("enabled", True)]
    if not products:
        sys.exit("No enabled products in products.yaml")
    interval = float(os.getenv("CHECK_INTERVAL_SECONDS",
                               config.get("check_interval_seconds", 60)))

    state = load_state()
    end = time.time() + args.loop * 60
    while True:
        run_pass(products, state, test=args.test)
        if args.test or time.time() + interval > end:
            break
        time.sleep(interval * random.uniform(0.85, 1.15))


if __name__ == "__main__":
    main()
