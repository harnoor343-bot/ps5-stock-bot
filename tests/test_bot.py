"""Offline tests for the parsers and alert logic (no network needed): pytest -q"""
import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot  # noqa: E402

# ---------------------------------------------------------------- Amazon

AMZ_SOLD_BY_AMAZON = """
<div id="availability"><span>In Stock</span></div>
<div id="corePrice_feature_div"><span class="a-offscreen">$749.99</span></div>
<div id="merchantInfoFeature_feature_div">Sold by Amazon.ca</div>
<input type="hidden" name="merchantID" value="A3DWYIK6Y9EEQB">
<input id="add-to-cart-button" type="submit">
"""
AMZ_THIRD_PARTY = """
<div id="availability"><span>In Stock</span></div>
<div id="merchantInfoFeature_feature_div">Sold by GameDealz CA</div>
<input type="hidden" name="merchantID" value="A1B2C3D4E5F6G7">
<input id="add-to-cart-button" type="submit">
"""
AMZ_OOS = """<div id="availability"><span>Currently unavailable. We don't know when or if this item will be back in stock.</span></div>"""
AMZ_CAPTCHA = """<form action="/errors/validateCaptcha">Type the characters you see in this image</form>"""


def test_amazon_sold_by_amazon():
    r = bot.parse_amazon(AMZ_SOLD_BY_AMAZON)
    assert r.status == bot.IN_STOCK and r.price == "$749.99" and r.seller == "Amazon.ca"


def test_amazon_third_party_ignored():
    assert bot.parse_amazon(AMZ_THIRD_PARTY).status == bot.THIRD_PARTY


def test_amazon_oos_and_captcha():
    assert bot.parse_amazon(AMZ_OOS).status == bot.OUT_OF_STOCK
    assert bot.parse_amazon(AMZ_CAPTCHA).status == bot.BLOCKED
    assert bot.parse_amazon("", 503).status == bot.BLOCKED


# ---------------------------------------------------------------- Walmart

def _next(product):
    return ('<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps({"props": {"pageProps": {"initialData": {"data": {"product": product}}}}})
            + "</script>")


def test_walmart_first_party_in_stock():
    html = _next({"availabilityStatus": "IN_STOCK", "sellerName": "Walmart.ca",
                  "priceInfo": {"currentPrice": {"price": 1099.99, "priceString": "$1,099.99"}}})
    r = bot.parse_walmart(html)
    assert r.status == bot.IN_STOCK and r.price == "$1,099.99"


def test_walmart_marketplace_ignored():
    html = _next({"availabilityStatus": "IN_STOCK", "sellerDisplayName": "Daffy Store"})
    assert bot.parse_walmart(html).status == bot.THIRD_PARTY


def test_walmart_oos_and_block():
    assert bot.parse_walmart(_next({"availabilityStatus": "OUT_OF_STOCK", "sellerName": "Walmart"})).status == bot.OUT_OF_STOCK
    assert bot.parse_walmart("<h1>Robot or human?</h1>").status == bot.BLOCKED


def test_walmart_unknown_seller_does_not_alert():
    assert bot.parse_walmart(_next({"availabilityStatus": "IN_STOCK"})).status == bot.UNKNOWN


def test_walmart_ldjson_fallback():
    ld = {"@type": "Product", "offers": {"availability": "https://schema.org/InStock", "price": "819.99",
                                         "seller": {"name": "Walmart"}}}
    html = f'<script type="application/ld+json">{json.dumps(ld)}</script>'
    assert bot.parse_walmart(html).status == bot.IN_STOCK


# ---------------------------------------------------------------- Costco / EB Games

def test_ebgames_ldjson_oos():
    ld = {"name": "PS5 Digital", "offers": [{"priceCurrency": "CAD", "price": "749.99",
          "availability": "https://schema.org/OutOfStock", "seller": {"name": "www.ebgames.ca"}}]}
    html = f'<script type="application/ld+json">{json.dumps(ld)}</script>'
    assert bot.parse_generic(html, 200, ("ebgames",)).status == bot.OUT_OF_STOCK


def test_ebgames_ldjson_in_stock():
    ld = {"offers": [{"price": "749.99", "availability": "https://schema.org/InStock",
                      "seller": {"name": "www.ebgames.ca"}}]}
    html = f'<script type="application/ld+json">{json.dumps(ld)}</script>'
    r = bot.parse_generic(html, 200, ("ebgames",))
    assert r.status == bot.IN_STOCK and r.price == "$749.99"


def test_costco_text_fallback():
    assert bot.parse_generic('<button id="add-to-cart-btn">Add to Cart</button>', 200, ("costco",)).status == bot.IN_STOCK
    assert bot.parse_generic('<button id="add-to-cart-btn" disabled>Add</button><p>Out of Stock</p>', 200, ("costco",)).status == bot.OUT_OF_STOCK
    assert bot.parse_generic('<button id="add-to-cart-btn" disabled>Add</button>', 200, ("costco",)).status == bot.UNKNOWN
    assert bot.parse_generic("Access Denied", 403, ("costco",)).status == bot.BLOCKED


# ---------------------------------------------------------------- Best Buy

class FakeResp:
    def __init__(self, body, code=200):
        self.content = body.encode("utf-8-sig")
        self.text = body
        self.status_code = code


def test_bestbuy_api():
    body = json.dumps({"availabilities": [
        {"sku": "19492009", "shipping": {"status": "InStock", "purchasable": True, "quantityRemaining": 12},
         "pickup": {"status": "NotAvailable", "purchasable": False}},
        {"sku": "19446111", "shipping": {"status": "SoldOutOnline", "purchasable": False},
         "pickup": {"status": "NotAvailable", "purchasable": False}},
    ]})
    ps = [bot.Product("bestbuy", "PS5 Pro", "Pro", "u1", sku="19492009"),
          bot.Product("bestbuy", "PS5 Slim (disc)", "Slim", "u2", sku="19446111")]
    with mock.patch.object(bot, "fetch", return_value=FakeResp(body)):
        res = bot.check_bestbuy(ps)
    assert res[ps[0].key].status == bot.IN_STOCK and "12 left" in res[ps[0].key].detail
    assert res[ps[1].key].status == bot.OUT_OF_STOCK


def test_bestbuy_blocked():
    ps = [bot.Product("bestbuy", "PS5 Pro", "Pro", "u1", sku="1")]
    with mock.patch.object(bot, "fetch", return_value=FakeResp("<html>Access Denied</html>", 403)):
        assert bot.check_bestbuy(ps)[ps[0].key].status == bot.BLOCKED


# ---------------------------------------------------------------- alert logic

def test_alerts_only_on_change(tmp_path, monkeypatch):
    p = bot.Product("amazon", "PS5 Pro", "Pro", "https://x", asin="B0X")
    state = {"items": {}, "fail_streak": {}, "health_alerted": {}}
    sent = []
    monkeypatch.setattr(bot, "send_email", lambda *a: sent.append(a[0]))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    seq = [bot.Result(bot.OUT_OF_STOCK), bot.Result(bot.IN_STOCK, "x", "$1"), bot.Result(bot.IN_STOCK),
           bot.Result(bot.BLOCKED), bot.Result(bot.IN_STOCK), bot.Result(bot.OUT_OF_STOCK), bot.Result(bot.IN_STOCK)]
    for r in seq:
        monkeypatch.setattr(bot, "run_checks", lambda ps, r=r: {p.key: r})
        bot.process_round([p], state, dry_run=False)
    # alerts: first IN_STOCK, (blocked blip doesn't re-trigger), then after going OOS -> IN_STOCK again
    assert len(sent) == 2 and all(s.startswith("PS5 IN STOCK") for s in sent)


def test_health_alert_once(monkeypatch):
    p = bot.Product("walmart", "PS5 Pro", "Pro", "https://x")
    state = {}
    sent = []
    monkeypatch.setattr(bot, "send_email", lambda *a: sent.append(a[0]))
    monkeypatch.setattr(bot, "HEALTH_ALERT_AFTER", 3)
    monkeypatch.setattr(bot, "run_checks", lambda ps: {p.key: bot.Result(bot.BLOCKED, "captcha")})
    for _ in range(6):
        bot.process_round([p], state, dry_run=False)
    assert sent == ["PS5 bot: some stores are blocking checks"]


def test_products_file_loads():
    ps = bot.load_products()
    assert len(ps) >= 15
    assert {p.retailer for p in ps} == {"bestbuy", "amazon", "walmart", "costco", "ebgames"}
    assert all(p.sku for p in ps if p.retailer == "bestbuy")
    assert all(p.asin for p in ps if p.retailer == "amazon")
    assert len({p.key for p in ps}) == len(ps)
