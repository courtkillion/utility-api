"""Utility tariff API.

Endpoints:
    GET  /v1/tariff/plans   list available tariffs (free -- discovery aid)
    GET  /v1/tariff/rate    per-kWh rate at a moment in time
    POST /v1/tariff/bill    itemized bill from usage intervals

Payment is applied only when CDP credentials are present in the environment,
so the app runs unpaid locally and paid in production without a code change.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from tariff.calculate import (
    TariffDataError,
    UnsupportedTariffError,
    assert_billable,
    calculate_bill,
    classify_period,
    resolve_season,
)

import json

DATA_DIR = Path(__file__).resolve().parent / "data" / "tariffs"

# --------------------------------------------------------------------------
# registry -- load every tariff at startup, including unsupported ones so the
# API can explain *why* they are refused rather than 404ing on them
# --------------------------------------------------------------------------

def _load_registry():
    """tariff_id -> list of versions, oldest first.

    A tariff is a SERIES of filings, not one document. Each file covers a
    billing-cycle window; a rate case or a temporary adjustment adds a file
    rather than replacing one, so historical cycles stay answerable.
    """
    registry = {}
    for path in sorted(DATA_DIR.rglob("*.json")):
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        registry.setdefault(doc["tariff_id"], []).append(doc)
    for versions in registry.values():
        versions.sort(key=lambda d: d["effective"]["from_billing_cycle"])
    return registry


TARIFFS = _load_registry()


def _covers(doc, cycle: str) -> bool:
    eff = doc["effective"]
    through = eff.get("through_billing_cycle")
    return eff["from_billing_cycle"] <= cycle and (through is None or cycle <= through)


def _windows(versions):
    return [
        {
            "from": d["effective"]["from_billing_cycle"],
            "through": d["effective"].get("through_billing_cycle"),
        }
        for d in versions
    ]


def _select(tariff_id: str, year: int, month: int):
    """Pick the filing that governs a billing cycle, or refuse with the windows we do have."""
    versions = TARIFFS.get(tariff_id)
    if not versions:
        raise HTTPException(
            status_code=404,
            detail={"error": "unknown_tariff", "tariff_id": tariff_id, "available": sorted(TARIFFS)},
        )

    cycle = f"{year:04d}-{month:02d}"
    doc = next((d for d in versions if _covers(d, cycle)), None)
    if doc is None:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "outside_effective_window",
                "tariff_id": tariff_id,
                "requested_billing_cycle": cycle,
                "covered_windows": _windows(versions),
                "guidance": "No filing on record governs that billing cycle.",
            },
        )

    try:
        assert_billable(doc)
    except UnsupportedTariffError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unsupported_tariff",
                "tariff_id": tariff_id,
                "reason": str(exc),
                "guidance": "This plan contains terms the current data model cannot express. A bill computed for it would be wrong, so it is refused deliberately.",
            },
        )
    return doc


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------

class Interval(BaseModel):
    t: datetime = Field(description="Wall clock time in the tariff's filed timezone (SRP files in MST, no DST).")
    kwh: float = Field(ge=0)


class BillRequest(BaseModel):
    tariff_id: str = Field(examples=["srp:E-26"])
    intervals: List[Interval] = Field(min_length=1, max_length=9000)
    service_tier: str = Field(default="tier_2", examples=["tier_1", "tier_2", "tier_3"])
    billing_month: Optional[int] = Field(default=None, ge=1, le=12,
                                         description="Billing cycle month. Inferred from the first interval when omitted.")


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

NETWORK = os.getenv("X402_NETWORK", "eip155:84532")   # Base Sepolia by default
PAY_TO = os.getenv("X402_PAY_TO")

app = FastAPI(
    title="Utility Tariff API",
    description="Exact residential electric rates and bills for Arizona utilities, computed from filed tariffs with citations.",
    version="0.1.0",
)


@app.get("/")
async def root() -> dict:
    """Free. What this service is, and where to go next."""
    return {
        "service": "Utility Tariff API",
        "what_it_does": (
            "Returns exact residential electric rates and itemized bills for Arizona "
            "utilities, computed from filed tariff documents rather than estimates. "
            "Every response cites the filing it came from."
        ),
        "coverage": {
            "utilities": sorted({v["utility"]["name"] for vs in TARIFFS.values() for v in vs}),
            "plans": {tid: _windows(vs) for tid, vs in sorted(TARIFFS.items())},
            "customer_class": "residential",
            "note": "Each plan lists the billing-cycle windows on record. Cycles outside them return HTTP 422.",
        },
        "endpoints": {
            "GET /v1/tariff/plans": "List available plans and whether each can be billed. Free.",
            "GET /v1/tariff/rate": "Per-kWh rate at a moment in time, with component breakdown.",
            "POST /v1/tariff/bill": "Itemized bill from usage intervals.",
        },
        "docs": "/docs",
        "openapi": "/openapi.json",
        "payment": {
            "protocol": "x402",
            "network": NETWORK,
            "note": "Paid endpoints return HTTP 402 with terms in the payment-required header.",
        },
        "operator": "Killion Apps",
    }


@app.get("/v1/tariff/plans")
async def list_plans() -> dict:
    """Free. Lists what can be priced, so a caller knows before paying."""
    out = []
    for tid, versions in sorted(TARIFFS.items()):
        for doc in versions:
            try:
                assert_billable(doc)
                billable, reason = True, None
            except UnsupportedTariffError as exc:
                billable, reason = False, str(exc)
            out.append({
                "tariff_id": tid,
                "marketing_name": doc.get("marketing_name"),
                "utility": doc["utility"]["name"],
                "customer_class": doc["customer_class"],
                "status": doc["availability"]["status"],
                "effective_from_billing_cycle": doc["effective"]["from_billing_cycle"],
                "effective_through_billing_cycle": doc["effective"].get("through_billing_cycle"),
                "billable": billable,
                "not_billable_reason": reason,
            })
    return {"plans": out, "count": len(out)}


@app.get("/v1/tariff/rate")
async def get_rate(
    tariff_id: str = Query(examples=["srp:E-26"]),
    at: datetime = Query(description="Local wall clock time, e.g. 2026-07-15T15:00:00"),
    billing_month: Optional[int] = Query(default=None, ge=1, le=12,
                                         description="Defaults to the month of 'at'. Set explicitly when the billing cycle differs from the calendar month."),
) -> dict:
    """The per-kWh price in force at a single moment, with its components."""
    month = billing_month or at.month
    doc = _select(tariff_id, at.year, month)

    try:
        period = classify_period(at, doc)
        season = resolve_season(month, doc)
    except TariffDataError as exc:
        raise HTTPException(status_code=422, detail={"error": "tariff_data_error", "reason": str(exc)})

    charge = next(
        (c for c in doc["charges"]
         if c["type"] == "energy" and c.get("season") == season and c.get("period") == period),
        None,
    )
    if charge is None:
        raise HTTPException(
            status_code=422,
            detail={"error": "no_price", "season": season, "period": period},
        )

    return {
        "tariff_id": tariff_id,
        "at": at.isoformat(),
        "timezone": doc["time_basis"]["timezone"],
        "observes_dst": doc["time_basis"]["observes_dst"],
        "billing_month": month,
        "season": season,
        "period": period,
        "rate_per_kwh": charge["amount"],
        "currency": "USD",
        "components": charge.get("components", []),
        "source": doc["source"],
        "caveats": [p["summary"] for p in doc.get("unmodeled_provisions", [])],
    }


@app.post("/v1/tariff/bill")
async def post_bill(req: BillRequest) -> dict:
    """An itemized bill from usage intervals."""
    first = req.intervals[0].t
    doc = _select(req.tariff_id, first.year, req.billing_month or first.month)
    try:
        return calculate_bill(
            doc,
            [(i.t, i.kwh) for i in req.intervals],
            service_tier=req.service_tier,
            billing_month=req.billing_month,
        )
    except TariffDataError as exc:
        raise HTTPException(status_code=422, detail={"error": "tariff_data_error", "reason": str(exc)})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": "bad_request", "reason": str(exc)})


# --------------------------------------------------------------------------
# payment layer -- applied only when credentials exist
# --------------------------------------------------------------------------

if os.getenv("CDP_API_KEY_ID") and PAY_TO:
    from cdp.x402 import create_facilitator_config
    from x402.http import HTTPFacilitatorClient, PaymentOption
    from x402.http.middleware.fastapi import PaymentMiddlewareASGI
    from x402.http.types import RouteConfig
    from x402.mechanisms.evm.exact import ExactEvmServerScheme
    from x402.server import x402ResourceServer

    server = x402ResourceServer(HTTPFacilitatorClient(create_facilitator_config()))
    server.register(NETWORK, ExactEvmServerScheme())

    def _option(price):
        return PaymentOption(scheme="exact", pay_to=PAY_TO, price=price, network=NETWORK)

    routes = {
        "GET /v1/tariff/rate": RouteConfig(
            accepts=[_option("$0.02")],
            mime_type="application/json",
            description=(
                "Exact residential electric rate for Salt River Project (Phoenix metro, Arizona) "
                "at a specific date and time. Resolves time-of-use period from the filed peak-hour "
                "calendar including observed holidays and super-off-peak windows, resolves season "
                "from the billing cycle, and returns the per-kWh price with its full unbundled "
                "component breakdown and a citation to the filed ratebook. Inputs: tariff_id "
                "(e.g. srp:E-26), at (ISO 8601 local time). Call /v1/tariff/plans first, free, "
                "to list available plans."
            ),
        ),
        "POST /v1/tariff/bill": RouteConfig(
            accepts=[_option("$0.25")],
            mime_type="application/json",
            description=(
                "Itemized residential electric bill for Salt River Project (Phoenix metro, Arizona) "
                "from hourly usage intervals. Classifies every interval into its time-of-use period, "
                "applies seasonal rates, adds the monthly service charge for the dwelling tier, "
                "applies the minimum bill, and returns line items plus an aggregated component "
                "breakdown with a citation to the filed ratebook."
            ),
        ),
    }

    app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=server)
