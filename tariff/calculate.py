"""Bill calculation from a tariff document.

Pure functions over a tariff dict loaded from data/tariffs/. No I/O, no network.

Design notes worth knowing before you edit this:

  * Timestamps are treated as WALL CLOCK TIME IN THE TARIFF'S FILED TIMEZONE.
    SRP files in Mountain Standard Time and Arizona does not observe DST, so a
    naive datetime is unambiguous here. If you ever add a utility in a
    DST-observing state, this assumption breaks and needs revisiting.

  * SEASON comes from the billing cycle (a named month). PERIOD comes from the
    calendar date of each interval. These do not align in shoulder months --
    that is the whole reason the schema keeps them in separate fields.

  * A tariff carrying a SCHEMA GAP provision is refused outright. Producing a
    confidently wrong bill is worse than producing none.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

DAY_NAMES = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


class UnsupportedTariffError(Exception):
    """The tariff cannot be billed correctly with the current schema version."""


class TariffDataError(Exception):
    """The tariff document is internally inconsistent."""


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

def load_tariff(path):
    with open(path, encoding="utf-8") as fh:
        tariff = json.load(fh)
    assert_billable(tariff)
    return tariff


def assert_billable(tariff):
    """Refuse tariffs whose filing contains terms the schema cannot express."""
    gaps = [
        p["summary"]
        for p in tariff.get("unmodeled_provisions", [])
        if p["summary"].startswith("SCHEMA GAP")
    ]
    if gaps:
        raise UnsupportedTariffError(
            f"{tariff['tariff_id']} cannot be billed: {gaps[0]}"
        )


# --------------------------------------------------------------------------
# holidays
# --------------------------------------------------------------------------

def _nth_weekday(year, month, weekday, n):
    """n-th weekday of a month (weekday: Monday=0). n=-1 means last."""
    if n > 0:
        d = date(year, month, 1)
        offset = (weekday - d.weekday()) % 7
        return d + timedelta(days=offset + 7 * (n - 1))
    d = date(year, month, 28)
    while (d + timedelta(days=7)).month == month:
        d += timedelta(days=7)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d):
    """Federal observance: Saturday -> preceding Friday, Sunday -> following Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def srp_holidays(year):
    """The six holidays named in every SRP residential price plan."""
    return {
        _observed(date(year, 1, 1)),                  # New Year's Day
        _nth_weekday(year, 5, 0, -1),                 # Memorial Day
        _observed(date(year, 7, 4)),                  # Independence Day
        _nth_weekday(year, 9, 0, 1),                  # Labor Day
        _nth_weekday(year, 11, 3, 4),                 # Thanksgiving
        _observed(date(year, 12, 25)),                # Christmas
    }


# --------------------------------------------------------------------------
# period classification
# --------------------------------------------------------------------------

def _in_date_range(d, rng):
    """Month/day range, inclusive, wrapping the year boundary if needed."""
    if rng is None:
        return True
    start = tuple(int(x) for x in rng["from"].split("-"))
    end = tuple(int(x) for x in rng["to"].split("-"))
    cur = (d.month, d.day)
    if start <= end:
        return start <= cur <= end
    return cur >= start or cur <= end


def _in_time_window(ts, window):
    """Start inclusive, end exclusive. A 2 p.m.-8 p.m. window covers 14:00-19:59."""
    hhmm = f"{ts.hour:02d}:{ts.minute:02d}"
    return window["start"] <= hhmm < window["end"]


def classify_period(ts, tariff, holidays=None):
    """Return the pricing period for a single timestamp.

    Evaluation order: windows in array order, each checked against date range,
    day of week, clock time, and holiday applicability. First match wins.
    Falls back to default_period.
    """
    cal = tariff["period_calendar"]
    rule = cal.get("holiday_rule", {})
    suspended = set(rule.get("suspends_periods", []))

    if holidays is None:
        holidays = srp_holidays(ts.year)
    is_holiday = ts.date() in holidays

    for window in cal["windows"]:
        if not _in_date_range(ts.date(), window.get("date_range")):
            continue
        if DAY_NAMES[ts.weekday()] not in window["days"]:
            continue
        if not _in_time_window(ts, window):
            continue
        if is_holiday and window["period"] in suspended:
            if not window.get("applies_on_holidays", False):
                continue
        return window["period"]

    if is_holiday and rule.get("fallback_period"):
        return rule["fallback_period"]
    return cal["default_period"]


# --------------------------------------------------------------------------
# season resolution
# --------------------------------------------------------------------------

def resolve_season(billing_month, tariff):
    """billing_month is an int 1-12 naming the billing cycle, not a date."""
    for season in tariff["seasons"]:
        if billing_month in season.get("billing_cycle_months", []):
            return season["name"]
    raise TariffDataError(
        f"{tariff['tariff_id']}: no season covers billing month {billing_month}"
    )


# --------------------------------------------------------------------------
# charge lookup
# --------------------------------------------------------------------------

def _energy_charge(tariff, season, period):
    for c in tariff["charges"]:
        if c["type"] == "energy" and c.get("season") == season and c.get("period") == period:
            return c
    raise TariffDataError(
        f"{tariff['tariff_id']}: no energy price for {season}/{period}"
    )


def _service_charge(tariff, tier_key):
    for c in tariff["charges"]:
        if c["type"] != "monthly_service":
            continue
        if "amount" in c:
            return c["amount"]
        for tier in c.get("categorical_tiers", []):
            if tier["key"] == tier_key:
                return tier["amount"]
        raise TariffDataError(
            f"{tariff['tariff_id']}: no service charge tier '{tier_key}'. "
            f"Available: {[t['key'] for t in c.get('categorical_tiers', [])]}"
        )
    raise TariffDataError(f"{tariff['tariff_id']}: no monthly service charge")


def _has_minimum_bill(tariff):
    return any(c["type"] == "minimum_bill" for c in tariff["charges"])


# --------------------------------------------------------------------------
# the calculation
# --------------------------------------------------------------------------

def calculate_bill(tariff, intervals, service_tier="tier_2", billing_month=None):
    """Compute an itemized bill.

    tariff        -- dict from load_tariff()
    intervals     -- iterable of (datetime, kwh); datetimes are wall clock in
                     the tariff's filed timezone
    service_tier  -- key from the monthly service charge's categorical_tiers
    billing_month -- 1-12; inferred from the first interval when omitted

    Returns a dict with period totals, line items, an aggregated component
    breakdown, and the caveats a caller needs to see.
    """
    assert_billable(tariff)

    intervals = list(intervals)
    if not intervals:
        raise ValueError("no usage intervals supplied")

    if billing_month is None:
        billing_month = intervals[0][0].month
    season = resolve_season(billing_month, tariff)

    holiday_cache = {}
    kwh_by_period = {}
    for ts, kwh in intervals:
        if ts.year not in holiday_cache:
            holiday_cache[ts.year] = srp_holidays(ts.year)
        period = classify_period(ts, tariff, holiday_cache[ts.year])
        kwh_by_period[period] = kwh_by_period.get(period, 0.0) + kwh

    line_items = []
    components = {}
    energy_total = 0.0

    for period in sorted(kwh_by_period):
        kwh = kwh_by_period[period]
        charge = _energy_charge(tariff, season, period)
        amount = round(kwh * charge["amount"], 2)
        energy_total += amount
        line_items.append({
            "description": f"Energy - {season} {period}",
            "kwh": round(kwh, 3),
            "rate": charge["amount"],
            "amount": amount,
        })
        for comp in charge.get("components", []):
            components[comp["name"]] = round(
                components.get(comp["name"], 0.0) + kwh * comp["amount"], 4
            )

    service = _service_charge(tariff, service_tier)
    line_items.append({
        "description": f"Monthly Service Charge ({service_tier})",
        "kwh": None,
        "rate": None,
        "amount": service,
    })

    subtotal = round(energy_total + service, 2)

    minimum_applied = False
    total = subtotal
    if _has_minimum_bill(tariff) and subtotal < service:
        total = service
        minimum_applied = True

    caveats = [
        p["summary"]
        for p in tariff.get("unmodeled_provisions", [])
        if p.get("affects") in {"bill total", None}
    ]

    return {
        "tariff_id": tariff["tariff_id"],
        "billing_month": billing_month,
        "season": season,
        "kwh_by_period": {k: round(v, 3) for k, v in kwh_by_period.items()},
        "total_kwh": round(sum(kwh_by_period.values()), 3),
        "line_items": line_items,
        "component_breakdown": components,
        "energy_total": round(energy_total, 2),
        "service_charge": service,
        "subtotal": subtotal,
        "minimum_bill_applied": minimum_applied,
        "total": round(total, 2),
        "taxes_included": tariff.get("taxes", {}).get("included_in_rates", False),
        "source": tariff["source"],
        "caveats": caveats,
    }
