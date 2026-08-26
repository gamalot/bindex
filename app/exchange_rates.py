"""
Currency conversion rates, used only to normalize unit prices to AUD for
sorting the parts list by price (mixed-currency prices aren't otherwise
comparable). The converted value is never displayed, only used as a sort key.

Source: the European Central Bank's daily reference rate feed — the
authoritative source that most third-party rate APIs republish anyway, free,
no API key, no rate limit, updated once per TARGET business day.
https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml

Rates are cached in the `exchange_rates` table and refreshed at most once a
day. If a refresh attempt fails (network issue, feed unavailable, etc.) the
previously cached rates are left untouched and used as-is.
"""
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from app.extensions import db
from app.models import ExchangeRate

ECB_DAILY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
FETCH_TIMEOUT = 10
STALE_AFTER = timedelta(hours=24)


def _utcnow():
    return datetime.now(timezone.utc)


def fetch_ecb_rates_to_aud():
    """Fetch the ECB daily feed and return {currency: rate_to_aud}, including AUD itself (1.0)."""
    with urllib.request.urlopen(ECB_DAILY_URL, timeout=FETCH_TIMEOUT) as resp:
        raw = resp.read()

    root = ET.fromstring(raw)
    eur_rates = {}
    for cube in root.findall(".//{*}Cube[@currency]"):
        currency = cube.get("currency")
        rate = cube.get("rate")
        if currency and rate:
            eur_rates[currency] = float(rate)

    if "AUD" not in eur_rates:
        raise ValueError("ECB feed did not include an AUD rate")

    aud_per_eur = eur_rates["AUD"]
    rates_to_aud = {"AUD": 1.0, "EUR": aud_per_eur}
    for currency, eur_rate in eur_rates.items():
        if currency != "AUD":
            rates_to_aud[currency] = aud_per_eur / eur_rate
    return rates_to_aud


def rates_last_updated():
    return db.session.query(db.func.max(ExchangeRate.updated_at)).scalar()


def rates_are_stale():
    latest = rates_last_updated()
    if latest is None:
        return True
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    return (_utcnow() - latest) > STALE_AFTER


def ensure_fresh_rates(force=False, logger=None):
    """Best-effort refresh. On any failure, silently keep whatever rates are already cached."""
    if not force and not rates_are_stale():
        return False

    try:
        rates = fetch_ecb_rates_to_aud()
    except Exception as exc:
        if logger:
            logger.warning("Exchange rate update failed, keeping previous rates: %s", exc)
        return False

    now = _utcnow()
    existing = {r.currency: r for r in ExchangeRate.query.all()}
    for currency, rate in rates.items():
        row = existing.get(currency)
        if row is None:
            row = ExchangeRate(currency=currency)
            db.session.add(row)
        row.rate_to_aud = rate
        row.updated_at = now
    db.session.commit()
    return True


def get_rate_map():
    """{currency: rate_to_aud} for whatever is currently cached."""
    return {r.currency: r.rate_to_aud for r in ExchangeRate.query.all()}
