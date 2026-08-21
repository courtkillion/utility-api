# SRP Utility Tariff API

Exact residential electricity rates and itemized bills for **Salt River Project**
(Phoenix metro, Arizona), computed from SRP's filed ratebooks rather than
estimates. Every response cites the document and page it came from.

Live: **https://utility-api-jn9g.onrender.com** · Docs: **/docs**

```
GET https://utility-api-jn9g.onrender.com/v1/tariff/rate?tariff_id=srp:E-26&at=2026-07-15T15:00:00
```

```json
{
  "season": "summer_peak",
  "period": "on_peak",
  "rate_per_kwh": 0.2566,
  "components": [ ... ten unbundled line items ... ],
  "source": {
    "document_url": "https://www.srpnet.com/.../2025-Ratebook-with-Temporary-FPPAM.pdf",
    "page_refs": ["48-53"],
    "verified_by": "Courtney",
    "confidence": "verified"
  },
  "caveats": [ ... ]
}
```

---

## Why this exists

SRP's residential rates are public but genuinely hard to compute. Prices change
by hour, day of week, season, and observed holiday, across a dozen plans whose
definitions do not line up:

- **Seasons are defined by billing cycle. Peak hours are defined by calendar
  date.** On E-26, summer means the May, June, September, and October *billing
  cycles*, but the 2–8 p.m. on-peak window runs May 1 through October 31 by
  *calendar date*. A billing cycle straddling October 31 gets one season's
  prices and two different hour classifications. Collapsing these into a single
  "season" field silently mis-bills every shoulder month.
- **Tariffs are filed in Mountain Standard Time and Arizona does not observe
  DST.** Any generic time-of-use library that shifts for daylight saving is an
  hour wrong for half the year.
- **Rates are unbundled component stacks**, not single numbers. The fuel and
  purchased power adjustment sits inside the stack and moves on its own
  schedule, so it has to stay separable.
- **Holidays suspend some periods but not others.** On E-28, on-peak is
  suspended on Christmas while super-off-peak still applies.

This service models all of that from the filings and shows its work.

---

## Coverage

Call `GET /` or `GET /v1/tariff/plans` for the live answer. As of the latest
data load:

| Plan | Marketing name | Billing cycles | Billable |
|---|---|---|---|
| `srp:E-26` | Time-Of-Use | 2025-11 → 2026-04, 2026-05 → 2026-10 | yes |
| `srp:E-21` | EZ-3 (3–6 p.m.) | 2026-05 → 2026-10 | yes |
| `srp:E-28` | Conserve 6–9 p.m. and Save | 2025-11 → | **no** |

Residential only. Requests outside a covered billing cycle return **422** with
the windows that are covered.

---

## What it refuses, and why

This is the part worth reading before you rely on it.

**Unmodeled plans.** E-28 pays a per-exported-kWh credit that the current data
model cannot express. Rather than return a bill that is wrong for any customer
with rooftop solar, the service refuses E-28 outright and says so. Fixing this
is a schema change, tracked below.

**Uncovered dates.** Each file covers a specific billing-cycle window. Ask for a
cycle no filing on record governs and you get a 422 listing what is covered —
never an extrapolated rate.

**Provisions that cannot be data.** Every tariff carries an
`unmodeled_provisions` list surfaced as `caveats` in responses: the $300,000
annual energy-efficiency cap, SRP's discretion to move the fuel adjustment
between filings, and the governmental tax/fee pass-through. These are real terms
that can change the number, and they are stated rather than hidden.

**Taxes.** Not included. State, county, and municipal rates depend on the
service address.

---

## MCP server

The same calculator is available as an MCP server, so assistants can call it
directly. Three tools: `list_srp_plans`, `get_srp_rate`, `calculate_srp_bill`.
Free and local — no payment, no network call.

```json
{
  "mcpServers": {
    "srp-tariff": {
      "command": "C:\\path\\to\\utility-api\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\utility-api\\mcp_server.py"]
    }
  }
}
```

Windows notes, learned the hard way:

- Install dependencies with the venv's interpreter explicitly:
  `& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt`.
  A plain `pip install` may hit a different Python and the server will fail with
  `ModuleNotFoundError`.
- If Claude Desktop came from the Microsoft Store, it reads a sandboxed config
  path under
  `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\`, not
  `%APPDATA%\Claude\`. Use **Settings → Developer → Edit Config** to find the
  file the app actually reads.
- Developer mode must be enabled for local servers to appear.

---

## Machine payments (x402)

The HTTP endpoints are also payable per call over
[x402](https://x402.org) — an agent receives a 402, pays in USDC, and retries,
with no account or API key. Currently on **Base Sepolia (testnet)**; the network
in force is reported at `GET /`.

`/` and `/v1/tariff/plans` are free by design: a caller has to be able to learn
what is covered before deciding to pay.

---

## Running locally

```bash
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m pytest tests\ -v
python -m uvicorn server:app --reload
```

Payment activates only when `CDP_API_KEY_ID` and `X402_PAY_TO` are both set in
`.env`. Leave them unset to run unpaid.

---

## Data model

One JSON file per **filing**, not per plan:
`data/tariffs/srp/<slug>@<from-billing-cycle>.json`. A rate case or a temporary
adjustment adds a file; it never edits one. The registry keys `tariff_id` to a
list of versions and selects by the requested billing cycle, so historical
cycles stay answerable.

Files validate against `schemas/tariff-schema.v1.1.json`. The test suite checks
that every component stack sums to its printed total, that all twelve billing
months map to exactly one season, that every season/period combination the
calendar can produce has a price, and that a file claiming `confidence:
verified` also names who verified it and which pages.

---

## Known gaps

- **November 2026 onward is not modeled.** SRP publishes several ratebook
  variants (base, temporary-FPPAM, and TCA revisions) and which governs the
  November cycle needs confirming against the source before data is added.
- **Export credits** (E-28, E-13, E-14, E-16) need a credit charge type —
  schema v1.2.
- **Proration** for partial billing cycles (move-in, move-out, plan change) is
  not expressed.
- **Structured eligibility.** A customer with 2019 rooftop solar cannot be on
  E-26, but eligibility is currently prose, so nothing stops a caller pricing
  them there.
- **Rounding convention** is per-line-item then summed. Whether SRP sums first
  and rounds once is unverified against a real time-of-use bill.

---

## Accuracy and disclaimer

Rates are transcribed from SRP's filed ratebooks and each file records who
verified it against which pages. Component stacks are machine-checked to sum to
their printed totals. Even so, this is an unofficial reimplementation: it is not
affiliated with or endorsed by SRP, and it should not be the sole basis for a
financial decision. Verify against your own bill and SRP's published rates.

Filings change. Check `retrieved_at` in any response and confirm the cycle you
care about is covered.

---

Built by [Killion Apps](https://killionapps.com).
