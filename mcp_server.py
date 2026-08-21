"""MCP server for the SRP tariff engine.

Exposes the same calculator the HTTP API uses, as tools an assistant can call
directly. No payment layer here on purpose: this channel exists for reach and
credibility, and a paywall inside a chat session would kill the only signal
worth collecting.

Built against MCP Python SDK v2 (mcp.server.mcpserver.MCPServer).

Run locally:
    mcp dev mcp_server.py

Install into a client by pointing it at:
    <repo>/.venv/Scripts/python.exe  <repo>/mcp_server.py
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from tariff.calculate import (
    TariffDataError,
    UnsupportedTariffError,
    assert_billable,
    calculate_bill,
    classify_period,
    resolve_season,
)

DATA_DIR = Path(__file__).resolve().parent / "data" / "tariffs"

mcp = MCPServer(
    name="srp-tariff",
    title="SRP Utility Tariff",
    version="0.1.0",
    instructions=(
        "Exact residential electricity rates and bills for Salt River Project "
        "(Phoenix metro, Arizona), computed from SRP's filed ratebooks rather than "
        "estimates. Call list_srp_plans first to see which plans and billing cycles "
        "are covered. Never estimate a rate this server refuses to give — a refusal "
        "means the governing filing has not been modeled, and a guess would be wrong."
    ),
)


# --------------------------------------------------------------------------
# registry (mirrors server.py — one file per filing, selected by billing cycle)
# --------------------------------------------------------------------------

def _load_registry() -> dict[str, list[dict]]:
    registry: dict[str, list[dict]] = {}
    for path in sorted(DATA_DIR.rglob("*.json")):
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        registry.setdefault(doc["tariff_id"], []).append(doc)
    for versions in registry.values():
        versions.sort(key=lambda d: d["effective"]["from_billing_cycle"])
    return registry


TARIFFS = _load_registry()


def _covers(doc: dict, cycle: str) -> bool:
    eff = doc["effective"]
    through = eff.get("through_billing_cycle")
    return eff["from_billing_cycle"] <= cycle and (through is None or cycle <= through)


def _windows(versions: list[dict]) -> list[dict]:
    return [
        {
            "from": d["effective"]["from_billing_cycle"],
            "through": d["effective"].get("through_billing_cycle"),
        }
        for d in versions
    ]


def _select(tariff_id: str, year: int, month: int) -> dict:
    """Return the filing governing a billing cycle, or raise with a usable explanation."""
    versions = TARIFFS.get(tariff_id)
    if not versions:
        raise ValueError(
            f"Unknown tariff '{tariff_id}'. Available: {', '.join(sorted(TARIFFS))}"
        )

    cycle = f"{year:04d}-{month:02d}"
    doc = next((d for d in versions if _covers(d, cycle)), None)
    if doc is None:
        covered = "; ".join(
            f"{w['from']} to {w['through'] or 'open-ended'}" for w in _windows(versions)
        )
        raise ValueError(
            f"No filing on record governs billing cycle {cycle} for {tariff_id}. "
            f"Covered windows: {covered}. Do not estimate a rate for this cycle — "
            f"the governing filing has not been modeled."
        )

    assert_billable(doc)
    return doc


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------

@mcp.tool()
def list_srp_plans() -> dict[str, Any]:
    """List every Salt River Project residential price plan this server can price,
    with the billing-cycle windows on record for each.

    Call this first when you don't know which plan a customer is on, or to check
    whether a date is covered before asking for a rate. SRP serves the Phoenix
    metropolitan area in Arizona.

    Plans marked not billable are refused deliberately: their filings contain
    terms the data model cannot express, so any number would be wrong.
    """
    plans = []
    for tariff_id, versions in sorted(TARIFFS.items()):
        newest = versions[-1]
        try:
            assert_billable(newest)
            billable, reason = True, None
        except UnsupportedTariffError as exc:
            billable, reason = False, str(exc)
        plans.append({
            "tariff_id": tariff_id,
            "marketing_name": newest.get("marketing_name"),
            "status": newest["availability"]["status"],
            "eligibility": newest["availability"].get("eligibility_notes"),
            "covered_billing_cycles": _windows(versions),
            "billable": billable,
            "not_billable_reason": reason,
        })
    return {
        "utility": "Salt River Project (SRP), Phoenix metro, Arizona",
        "customer_class": "residential",
        "plans": plans,
        "note": (
            "Rates come from SRP's filed ratebooks, not estimates. Every result "
            "carries a citation to the source document."
        ),
    }


@mcp.tool()
def get_srp_rate(tariff_id: str, at: str, billing_month: int | None = None) -> dict[str, Any]:
    """Get the exact per-kWh electricity price in force at a specific moment on an
    SRP residential plan, with its full unbundled component breakdown.

    Use this for questions like "what does power cost at 3pm on a July weekday",
    for EV charge scheduling, battery dispatch, or solar payback analysis. It
    resolves the time-of-use period from the filed peak-hour calendar — including
    seasonal window changes, observed holidays, and super-off-peak windows — and
    resolves the season from the billing cycle, which is a different boundary
    than the calendar date.

    Args:
        tariff_id: Plan identifier, e.g. "srp:E-26". Call list_srp_plans if unsure.
        at: Local wall-clock time, ISO 8601, e.g. "2026-07-15T15:00:00".
            SRP files in Mountain Standard Time and Arizona does not observe
            daylight saving, so no offset is needed or applied.
        billing_month: 1-12. Defaults to the month of `at`. Set explicitly only
            when the billing cycle differs from the calendar month.
    """
    try:
        ts = datetime.fromisoformat(at)
    except ValueError as exc:
        raise ValueError(f"Could not parse '{at}' as an ISO 8601 datetime: {exc}")

    month = billing_month or ts.month
    doc = _select(tariff_id, ts.year, month)

    period = classify_period(ts, doc)
    season = resolve_season(month, doc)

    charge = next(
        (c for c in doc["charges"]
         if c["type"] == "energy" and c.get("season") == season and c.get("period") == period),
        None,
    )
    if charge is None:
        raise TariffDataError(f"{tariff_id}: no price on file for {season}/{period}")

    return {
        "tariff_id": tariff_id,
        "at": ts.isoformat(),
        "timezone": doc["time_basis"]["timezone"],
        "observes_daylight_saving": doc["time_basis"]["observes_dst"],
        "billing_month": month,
        "season": season,
        "period": period,
        "rate_per_kwh_usd": charge["amount"],
        "components": charge.get("components", []),
        "source": doc["source"],
        "caveats": [p["summary"] for p in doc.get("unmodeled_provisions", [])],
    }


@mcp.tool()
def calculate_srp_bill(
    tariff_id: str,
    intervals: list[dict],
    service_tier: str = "tier_2",
    billing_month: int | None = None,
) -> dict[str, Any]:
    """Compute an itemized SRP electricity bill from metered usage intervals.

    Classifies every interval into its time-of-use period, applies the seasonal
    rate for the billing cycle, adds the monthly service charge for the dwelling
    tier, applies the minimum bill, and returns line items plus an aggregated
    breakdown of every rate component. Taxes are NOT included — those depend on
    the service address jurisdiction.

    Args:
        tariff_id: Plan identifier, e.g. "srp:E-26".
        intervals: List of {"t": ISO 8601 local time, "kwh": number}. Hourly is
            typical; any granularity works. Up to about a month of hourly data.
        service_tier: Dwelling tier for the monthly service charge.
            "tier_1" = apartment, condo, townhouse, or patio home, 0-225 amps ($20)
            "tier_2" = any other dwelling type, 0-225 amps ($30)
            "tier_3" = any residence over 225 amps ($40)
        billing_month: 1-12. Defaults to the month of the first interval.
    """
    if not intervals:
        raise ValueError("No usage intervals supplied.")

    parsed = []
    for i, item in enumerate(intervals):
        try:
            parsed.append((datetime.fromisoformat(item["t"]), float(item["kwh"])))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"intervals[{i}] must be an object with 't' (ISO 8601) and 'kwh' (number): {exc}"
            )

    month = billing_month or parsed[0][0].month
    doc = _select(tariff_id, parsed[0][0].year, month)

    bill = calculate_bill(doc, parsed, service_tier=service_tier, billing_month=month)
    bill["taxes_note"] = (
        "Taxes are not included. State, county, and municipal rates depend on the "
        "service address and must be applied separately."
    )
    return bill


if __name__ == "__main__":
    mcp.run()
