#!/usr/bin/env python3
"""
PS5 stock bot for Canadian retailers.

Checks Best Buy, Amazon.ca, Walmart.ca, Costco.ca and EB Games for PS5 Pro,
PS5 Slim and PS5 Slim Digital, and emails you when one comes in stock
*sold by the retailer itself* (third-party/marketplace sellers are ignored).

Usage:
    python bot.py                      # one round of checks
    python bot.py --loop-minutes 4     # keep checking for 4 minutes (used by GitHub Actions)
    python bot.py --test-email         # send a test email and exit
    python bot.py --dry-run            # check and print, never email

Email settings come from environment variables (GitHub secrets):
    SMTP_USER   e.g. you@gmail.com
    SMTP_PASS   Gmail *app password* (not your normal password)
    EMAIL_TO    where alerts go (comma-separated ok). Defaults to SMTP_USER.
    SMTP_HOST   default smtp.gmail.com
    SMTP_PORT   default 465 (SSL)
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import smtplib
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path

import yaml
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as http  # browser-like TLS fingerprint, gets blocked far less
    IMPERSONATE = {"impersonate": "chrome"}
except ImportError:  # pragma: no cover
    import requests as http  # type: ignore
    IMPERSONATE = {}

ROOT = Path(__file__).resolve().parent
PRODUCTS_FILE = ROOT / "products.yaml"
STATE_FILE = Path(os.environ.get("STATE_FILE") or ROOT / "state" / "state.json")

IN_STOCK = "IN_STOCK"
OUT_OF_STOCK = "OUT_OF_STOCK"
THIRD_PARTY = "THIRD_PARTY"      # available, but only from a marketplace seller -> ignored
BLOCKED = "BLOCKED"              # retailer served a captcha / bot wall
ERROR = "ERROR"                  # network error, page changed, etc.
UNKNOWN = "UNKNOWN"              # page loaded but couldn't tell

AMAZON_CA_MERCHANT_ID = "A3DWYIK6Y9EEQB"   # Amazon.ca's own seller id
HEALTH_ALERT_AFTER = int(os.environ.get("HEALTH_ALERT_AFTER") or "60")  # consecutive failed rounds
REMIND_HOURS = float(os.environ.get("REMIND_HOURS") or "0")             # 0 = only alert on change

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9",
    "Cache-Control": "no-cache",
}


@dataclass
class Result:
    status: str
    detail: str = ""
    price: str | None = None
    seller: str | None = None


@dataclass
class Product:
    retailer: str
    model: str
    name: str
    url: str
    sku: str | None = None
    asin: str | None = None
    enabled: bool = True
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.retailer}:{self.sku or self.asin or self.url}"


# --------------------------------------------------------------------------- helpers

def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def fetch(url: str, headers: dict | None = None, timeout: int = 25):
    h = dict(HEADERS)
    if headers:
        h.update(headers)
    return http.get(url, headers=h, timeout=timeout, allow_redirects=True, **IMPERSONATE)


BLOCK_MARKERS = (
    "validatecaptcha", "type the characters you see", "robot or human", "px-captcha",
    "are you a robot", "access denied", "request unsuccessful. incapsula",
    "pardon our interruption", "verify you are human", "/_sec/cp_challenge",
)


def looks_blocked(status_code: int, text: str) -> bool:
    if status_code in (403, 429, 503):
        return True
    low = text[:200000].lower()
    return any(m in low for m in BLOCK_MARKERS)


def iter_dicts(obj):
    """Yield every dict nested anywhere inside obj."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from iter_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_dicts(v)


def ld_json_offers(soup: BeautifulSoup) -> list[dict]:
    """Return schema.org Offer dicts found in <script type=application/ld+json>."""
    offers = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for d in iter_dicts(data):
            o = d.get("offers")
            if isinstance(o, dict):
                offers.append(o)
            elif isinstance(o, list):
                offers.extend(x for x in o if isinstance(x, dict))
    return offers


def offer_availability(offer: dict) -> str | None:
    av = str(offer.get("availability", "")).lower()
    if not av:
        return None
    if any(k in av for k in ("instock", "limitedavailability", "onlineonly", "preorder", "presale")):
        return IN_STOCK
    return OUT_OF_STOCK


def offer_seller(offer: dict) -> str | None:
    s = offer.get("seller")
    if isinstance(s, dict):
        return s.get("name")
    if isinstance(s, str):
        return s
    return None


def offer_price(offer: dict) -> str | None:
    p = offer.get("price") or offer.get("lowPrice")
    return f"${p}" if p not in (None, "") else None


# --------------------------------------------------------------------------- retailers

def check_bestbuy(products: list[Product]) -> dict[str, Result]:
    """Best Buy has a JSON availability endpoint; one request covers every SKU."""
    skus = [p.sku for p in products if p.sku]
    out: dict[str, Result] = {}
    if not skus:
        return out
    url = (
        "https://www.bestbuy.ca/ecomm-api/availability/products"
        "?accept=application%2Fvnd.bestbuy.standardproduct.v1%2Bjson"
        "&accept-language=en-CA&skus=" + "%7C".join(skus)
    )
    try:
        r = fetch(url, headers={"Accept": "application/json", "Referer": "https://www.bestbuy.ca/"})
        text = r.content.decode("utf-8-sig", errors="replace")
        if looks_blocked(r.status_code, text) or not text.lstrip().startswith("{"):
            return {p.key: Result(BLOCKED, f"HTTP {r.status_code}") for p in products}
        data = json.loads(text)
    except Exception as e:  # noqa: BLE001
        return {p.key: Result(ERROR, f"{type(e).__name__}: {e}"[:200]) for p in products}

    by_sku = {str(a.get("sku")): a for a in data.get("availabilities", [])}
    for p in products:
        a = by_sku.get(p.sku or "")
        if not a:
            out[p.key] = Result(UNKNOWN, "SKU missing from response")
            continue
        ship = a.get("shipping") or {}
        pick = a.get("pickup") or {}
        ship_ok = bool(ship.get("purchasable"))
        pick_ok = bool(pick.get("purchasable"))
        detail = f"ship={ship.get('status')} pickup={pick.get('status')}"
        if ship_ok or pick_ok:
            how = "ship + pickup" if ship_ok and pick_ok else ("ships to home" if ship_ok else "store pickup only")
            qty = ship.get("quantityRemaining")
            if qty:
                how += f", ~{qty} left"
            out[p.key] = Result(IN_STOCK, how, seller="Best Buy")
        else:
            out[p.key] = Result(OUT_OF_STOCK, detail)
    return out


def parse_amazon(html: str, status_code: int = 200) -> Result:
    if looks_blocked(status_code, html):
        return Result(BLOCKED, f"captcha / HTTP {status_code}")
    soup = BeautifulSoup(html, "html.parser")

    # Who owns the buy box?
    merchant = None
    tag = soup.find("input", attrs={"name": "merchantID"}) or soup.find(id="merchantID")
    if tag and tag.get("value"):
        merchant = tag["value"].strip()
    if not merchant:
        m = re.search(r'"merchantID"\s*:\s*"([A-Z0-9]{8,})"', html)
        merchant = m.group(1) if m else None

    seller_text = ""
    for sel in ("#merchantInfoFeature_feature_div", "#tabular-buybox", "#merchant-info",
                "#sellerProfileTriggerId", "#offerDisplayFeatures_desktop"):
        el = soup.select_one(sel)
        if el:
            seller_text += " " + el.get_text(" ", strip=True)
    seller_text = re.sub(r"\s+", " ", seller_text).strip()

    has_cart = bool(soup.find(id="add-to-cart-button") or soup.find(id="buy-now-button"))
    avail_el = soup.select_one("#availability")
    avail_text = avail_el.get_text(" ", strip=True).lower() if avail_el else ""
    unavailable = "currently unavailable" in avail_text or "out of stock" in avail_text

    price_el = soup.select_one("#corePrice_feature_div .a-offscreen, .priceToPay .a-offscreen, #price_inside_buybox")
    price = price_el.get_text(strip=True) if price_el else None

    if not has_cart or unavailable:
        return Result(OUT_OF_STOCK, avail_text[:80] or "no buy box")

    is_amazon = merchant == AMAZON_CA_MERCHANT_ID or bool(
        re.search(r"sold by\s*amazon\.ca", seller_text, re.I)
        or re.search(r"ships from and sold by amazon\.ca", seller_text, re.I)
    )
    if is_amazon:
        return Result(IN_STOCK, avail_text[:80] or "in stock", price=price, seller="Amazon.ca")
    who = merchant or seller_text[:60] or "unknown seller"
    return Result(THIRD_PARTY, f"buy box held by {who}", price=price, seller=who)


def check_amazon(p: Product) -> Result:
    url = f"https://www.amazon.ca/dp/{p.asin}?th=1&psc=1" if p.asin else p.url
    r = fetch(url)
    return parse_amazon(r.text, r.status_code)


def parse_walmart(html: str, status_code: int = 200) -> Result:
    if looks_blocked(status_code, html):
        return Result(BLOCKED, f"bot wall / HTTP {status_code}")
    soup = BeautifulSoup(html, "html.parser")

    status = seller = price = None
    nd = soup.find("script", id="__NEXT_DATA__")
    if nd and nd.string:
        try:
            data = json.loads(nd.string)
            for d in iter_dicts(data):
                if "availabilityStatus" in d and isinstance(d.get("availabilityStatus"), str):
                    status = d["availabilityStatus"]
                    seller = d.get("sellerDisplayName") or d.get("sellerName")
                    if not seller and isinstance(d.get("seller"), dict):
                        seller = d["seller"].get("name") or d["seller"].get("displayName")
                    pi = d.get("priceInfo") or {}
                    cp = pi.get("currentPrice") if isinstance(pi, dict) else None
                    if isinstance(cp, dict):
                        price = cp.get("priceString") or (f"${cp['price']}" if cp.get("price") else None)
                    break
        except json.JSONDecodeError:
            pass

    if status is None:  # fall back to schema.org data
        for o in ld_json_offers(soup):
            av = offer_availability(o)
            if av:
                status = "IN_STOCK" if av == IN_STOCK else "OUT_OF_STOCK"
                seller = seller or offer_seller(o)
                price = price or offer_price(o)
                break

    if status is None:
        return Result(UNKNOWN, "couldn't find availability on page")
    if status.upper() != "IN_STOCK":
        return Result(OUT_OF_STOCK, status)
    if seller and "walmart" in seller.lower():
        return Result(IN_STOCK, "sold by Walmart", price=price, seller="Walmart")
    if seller:
        return Result(THIRD_PARTY, f"sold by {seller}", price=price, seller=seller)
    return Result(UNKNOWN, "in stock but seller unknown (not alerting)", price=price)


def check_walmart(p: Product) -> Result:
    r = fetch(p.url)
    return parse_walmart(r.text, r.status_code)


def parse_generic(html: str, status_code: int, first_party_names: tuple[str, ...]) -> Result:
    """For first-party-only stores (Costco, EB Games): schema.org data, then page text."""
    if looks_blocked(status_code, html):
        return Result(BLOCKED, f"bot wall / HTTP {status_code}")
    if status_code == 404:
        return Result(ERROR, "page not found (404) - listing may have moved")
    soup = BeautifulSoup(html, "html.parser")

    for o in ld_json_offers(soup):
        av = offer_availability(o)
        if av:
            seller = offer_seller(o)
            if av == IN_STOCK and seller and not any(n in seller.lower() for n in first_party_names):
                return Result(THIRD_PARTY, f"sold by {seller}", seller=seller)
            return Result(av, "schema.org: " + str(o.get("availability", "")).rsplit("/", 1)[-1],
                          price=offer_price(o), seller=seller)

    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).lower()
    oos = any(k in text for k in ("out of stock", "sold out", "currently unavailable"))
    cart_els = soup.select("[id*=add-to-cart i], [class*=add-to-cart i], [class*=addtocart i], "
                           "[id*=addtocart i], button[name=add-to-cart]")
    cart = any(el.get("disabled") is None and "disabled" not in " ".join(el.get("class", [])).lower()
               for el in cart_els)
    if cart and not oos:
        return Result(IN_STOCK, "add-to-cart button present")
    if oos:
        return Result(OUT_OF_STOCK, "page says out of stock")
    return Result(UNKNOWN, "couldn't tell from page")


def check_costco(p: Product) -> Result:
    r = fetch(p.url, headers={"Referer": "https://www.costco.ca/"})
    return parse_generic(r.text, r.status_code, ("costco",))


def check_ebgames(p: Product) -> Result:
    r = fetch(p.url)
    return parse_generic(r.text, r.status_code, ("ebgames", "eb games", "gamestop"))


SINGLE_CHECKERS = {
    "amazon": check_amazon,
    "walmart": check_walmart,
    "costco": check_costco,
    "ebgames": check_ebgames,
}


def run_checks(products: list[Product]) -> dict[str, Result]:
    results: dict[str, Result] = {}
    bb = [p for p in products if p.retailer == "bestbuy"]
    if bb:
        results.update(check_bestbuy(bb))
    for p in products:
        if p.retailer == "bestbuy":
            continue
        fn = SINGLE_CHECKERS.get(p.retailer)
        if not fn:
            results[p.key] = Result(ERROR, f"unknown retailer '{p.retailer}'")
            continue
        try:
            results[p.key] = fn(p)
        except Exception as e:  # noqa: BLE001
            results[p.key] = Result(ERROR, f"{type(e).__name__}: {e}"[:200])
        time.sleep(random.uniform(1.0, 2.5))  # be polite, look less like a bot
    return results


# --------------------------------------------------------------------------- state + email

def load_products() -> list[Product]:
    raw = yaml.safe_load(PRODUCTS_FILE.read_text()) or {}
    out = []
    for item in raw.get("products", []):
        known = {k: item.get(k) for k in ("retailer", "model", "name", "url", "sku", "asin")}
        p = Product(**{k: (str(v) if v is not None else None) for k, v in known.items()},
                    enabled=item.get("enabled", True))
        if p.enabled:
            out.append(p)
    return out


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"items": {}, "fail_streak": {}, "health_alerted": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def send_email(subject: str, text: str, html: str) -> None:
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    if not user or not pw:
        log("!! SMTP_USER / SMTP_PASS not set - can't send email. Message was:\n" + text)
        return
    to = [a.strip() for a in (os.environ.get("EMAIL_TO") or user).split(",") if a.strip()]
    host = (os.environ.get("SMTP_HOST") or "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT") or "465")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(to)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(user, pw)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, pw)
            s.send_message(msg)
    log(f"Email sent to {', '.join(to)}: {subject}")


RETAILER_NAMES = {"bestbuy": "Best Buy", "amazon": "Amazon.ca", "walmart": "Walmart",
                  "costco": "Costco", "ebgames": "EB Games"}


def build_stock_email(hits: list[tuple[Product, Result]]) -> tuple[str, str, str]:
    models = sorted({p.model for p, _ in hits})
    stores = sorted({RETAILER_NAMES.get(p.retailer, p.retailer) for p, _ in hits})
    subject = f"PS5 IN STOCK: {', '.join(models)} at {', '.join(stores)}"
    lines, rows = [], []
    for p, r in hits:
        store = RETAILER_NAMES.get(p.retailer, p.retailer)
        price = f" - {r.price}" if r.price else ""
        lines.append(f"* {p.model} @ {store}{price}\n  {p.name}\n  {r.detail}\n  {p.url}\n")
        rows.append(
            f"<tr><td><b>{escape(p.model)}</b><br><small>{escape(p.name)}</small></td>"
            f"<td>{escape(store)}</td><td>{escape(r.price or '')}</td><td>{escape(r.detail)}</td>"
            f"<td><a href=\"{escape(p.url)}\" style=\"background:#0070d1;color:#fff;padding:8px 14px;"
            f"border-radius:6px;text-decoration:none\">Buy now</a></td></tr>"
        )
    text = "Go go go! Sold by the retailer itself:\n\n" + "\n".join(lines)
    html = (
        "<h2 style='font-family:sans-serif'>PS5 in stock - go!</h2>"
        "<p style='font-family:sans-serif'>All of these are sold by the retailer itself (no third-party sellers).</p>"
        "<table cellpadding='8' style='font-family:sans-serif;border-collapse:collapse' border='1'>"
        "<tr><th>Console</th><th>Store</th><th>Price</th><th>Details</th><th></th></tr>"
        + "".join(rows) + "</table>"
    )
    return subject, text, html


def process_round(products: list[Product], state: dict, dry_run: bool) -> list[tuple[Product, Result]]:
    results = run_checks(products)
    now = datetime.now(timezone.utc)
    items = state.setdefault("items", {})
    hits: list[tuple[Product, Result]] = []

    for p in products:
        r = results.get(p.key, Result(ERROR, "no result"))
        prev = items.get(p.key, {})
        log(f"{RETAILER_NAMES.get(p.retailer, p.retailer):10} | {p.model:16} | {r.status:12} | "
            f"{(r.price or ''):>10} | {r.detail}")

        should_alert = False
        if r.status == IN_STOCK:
            if prev.get("status") != IN_STOCK:
                should_alert = True
            elif REMIND_HOURS > 0 and prev.get("last_alert"):
                last = datetime.fromisoformat(prev["last_alert"])
                should_alert = (now - last).total_seconds() > REMIND_HOURS * 3600
        # Ignore transient failures so a blip doesn't cause a duplicate alert later.
        new_status = r.status if r.status in (IN_STOCK, OUT_OF_STOCK, THIRD_PARTY) else prev.get("status", r.status)
        entry = {"status": new_status, "last_checked": now.isoformat(), "last_result": r.status,
                 "detail": r.detail, "last_alert": prev.get("last_alert")}
        if should_alert:
            hits.append((p, r))
            entry["last_alert"] = now.isoformat()
        items[p.key] = entry

    # retailer health: if *every* product at a retailer is blocked/erroring for a long time, tell the user once
    streak = state.setdefault("fail_streak", {})
    alerted = state.setdefault("health_alerted", {})
    health_msgs = []
    for retailer in sorted({p.retailer for p in products}):
        rs = [results.get(p.key) for p in products if p.retailer == retailer]
        all_bad = all(x and x.status in (BLOCKED, ERROR) for x in rs)
        streak[retailer] = streak.get(retailer, 0) + 1 if all_bad else 0
        if all_bad and streak[retailer] >= HEALTH_ALERT_AFTER and not alerted.get(retailer):
            alerted[retailer] = True
            health_msgs.append(f"{RETAILER_NAMES.get(retailer, retailer)}: {rs[0].status} - {rs[0].detail}")
        if not all_bad and alerted.get(retailer):
            alerted[retailer] = False

    if not dry_run:
        if hits:
            send_email(*build_stock_email(hits))
        if health_msgs:
            body = ("Heads up: the bot hasn't been able to check these stores for a while "
                    "(they're probably blocking GitHub's servers):\n\n" + "\n".join(health_msgs) +
                    "\n\nOther stores are still being checked. You'll get this once per outage.")
            send_email("PS5 bot: some stores are blocking checks", body,
                       "<pre style='font-family:sans-serif'>" + escape(body) + "</pre>")
    elif hits:
        log("DRY RUN - would email: " + build_stock_email(hits)[0])

    write_job_summary(products, results)
    return hits


def write_job_summary(products: list[Product], results: dict[str, Result]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    icon = {IN_STOCK: "🟢", OUT_OF_STOCK: "⚪", THIRD_PARTY: "🟡", BLOCKED: "🔴", ERROR: "🔴", UNKNOWN: "🟠"}
    lines = [f"### Check at {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC",
             "| Store | Console | Status | Price | Detail |", "|---|---|---|---|---|"]
    for p in products:
        r = results.get(p.key, Result(ERROR))
        lines.append(f"| {RETAILER_NAMES.get(p.retailer, p.retailer)} | [{p.model}]({p.url}) | "
                     f"{icon.get(r.status, '')} {r.status} | {r.price or ''} | {r.detail.replace('|', '/')} |")
    with open(path, "a") as f:
        f.write("\n".join(lines) + "\n\n")


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loop-minutes", type=float, default=0, help="keep checking for this many minutes")
    ap.add_argument("--interval", type=int, default=60, help="seconds between rounds when looping")
    ap.add_argument("--dry-run", action="store_true", help="never send email")
    ap.add_argument("--test-email", action="store_true", help="send a test email and exit")
    ap.add_argument("--only", help="only check this retailer (bestbuy, amazon, walmart, costco, ebgames)")
    args = ap.parse_args()

    if args.test_email:
        send_email("PS5 bot test email",
                   "If you're reading this, alerts will reach you. 🎮",
                   "<p style='font-family:sans-serif'>If you're reading this, alerts will reach you. 🎮</p>")
        return 0

    products = load_products()
    if args.only:
        products = [p for p in products if p.retailer == args.only]
    log(f"Watching {len(products)} listings")

    state = load_state()
    deadline = time.time() + args.loop_minutes * 60
    rounds = 0
    while True:
        rounds += 1
        log(f"--- round {rounds} ---")
        started = time.time()
        process_round(products, state, args.dry_run)
        save_state(state)
        round_secs = time.time() - started
        # only start another round if it can finish (roughly) before the deadline
        if time.time() + args.interval + round_secs > deadline + 30:
            break
        time.sleep(args.interval + random.uniform(-10, 10))
    return 0


if __name__ == "__main__":
    sys.exit(main())
