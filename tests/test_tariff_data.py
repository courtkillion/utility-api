"""Validation suite for tariff data files.

Run from the repo root:
    python -m pytest tests/ -v

Catches the three failure classes seen in review so far:
  1. component stacks that don't sum to their stated total
  2. season/period coverage gaps that would silently misprice an hour
  3. data drifting out of schema after a hand edit
"""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

REPO = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO / "schemas" / "tariff-schema.v1.1.json"
DATA_DIR = REPO / "data" / "tariffs"

TARIFF_FILES = sorted(DATA_DIR.rglob("*.json"))
CENT = 1e-9


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


SCHEMA = load(SCHEMA_PATH)


def ids(paths):
    return [p.relative_to(DATA_DIR).as_posix() for p in paths]


@pytest.fixture(scope="module")
def validator():
    return Draft202012Validator(SCHEMA)


def test_data_dir_not_empty():
    assert TARIFF_FILES, f"no tariff files found under {DATA_DIR}"


@pytest.mark.parametrize("path", TARIFF_FILES, ids=ids(TARIFF_FILES))
def test_validates_against_schema(path, validator):
    errors = sorted(validator.iter_errors(load(path)), key=lambda e: e.path)
    assert not errors, "\n".join(
        f"  {'/'.join(str(p) for p in e.path)}: {e.message}" for e in errors
    )


@pytest.mark.parametrize("path", TARIFF_FILES, ids=ids(TARIFF_FILES))
def test_component_stacks_sum_to_total(path):
    """A component stack that doesn't reconcile means the source was misread."""
    doc = load(path)
    for i, charge in enumerate(doc["charges"]):
        if charge.get("components") and "amount" in charge:
            total = round(sum(c["amount"] for c in charge["components"]), 6)
            assert abs(total - charge["amount"]) < CENT, (
                f"charges[{i}] {charge.get('season')}/{charge.get('period')}: "
                f"stated {charge['amount']} but components sum to {total}"
            )
        for j, tier in enumerate(charge.get("categorical_tiers", [])):
            if tier.get("components"):
                total = round(sum(c["amount"] for c in tier["components"]), 6)
                assert abs(total - tier["amount"]) < CENT, (
                    f"charges[{i}].categorical_tiers[{j}] ({tier['key']}): "
                    f"stated {tier['amount']} but components sum to {total}"
                )


@pytest.mark.parametrize("path", TARIFF_FILES, ids=ids(TARIFF_FILES))
def test_seasons_cover_all_twelve_billing_cycles(path):
    """Every billing month must map to exactly one season."""
    doc = load(path)
    seen = {}
    for season in doc["seasons"]:
        for month in season.get("billing_cycle_months", []):
            assert month not in seen, (
                f"month {month} claimed by both '{seen[month]}' and '{season['name']}'"
            )
            seen[month] = season["name"]
    if seen:
        assert set(seen) == set(range(1, 13)), (
            f"billing months not covered: {sorted(set(range(1, 13)) - set(seen))}"
        )


@pytest.mark.parametrize("path", TARIFF_FILES, ids=ids(TARIFF_FILES))
def test_every_season_period_combination_is_priced(path):
    """If the calendar can produce a period, a price must exist for it in every season."""
    doc = load(path)
    seasons = [s["name"] for s in doc["seasons"]]
    periods = {w["period"] for w in doc["period_calendar"]["windows"]}
    periods.add(doc["period_calendar"]["default_period"])

    priced = {
        (c.get("season"), c.get("period"))
        for c in doc["charges"]
        if c["type"] == "energy"
    }
    missing = [(s, p) for s in seasons for p in periods if (s, p) not in priced]
    assert not missing, f"no energy price for: {missing}"


@pytest.mark.parametrize("path", TARIFF_FILES, ids=ids(TARIFF_FILES))
def test_no_duplicate_season_period_pricing(path):
    doc = load(path)
    keys = [
        (c.get("season"), c.get("period"))
        for c in doc["charges"]
        if c["type"] == "energy"
    ]
    dupes = {k for k in keys if keys.count(k) > 1}
    assert not dupes, f"duplicate energy charges for: {dupes}"


@pytest.mark.parametrize("path", TARIFF_FILES, ids=ids(TARIFF_FILES))
def test_window_times_are_ordered(path):
    doc = load(path)
    for i, w in enumerate(doc["period_calendar"]["windows"]):
        assert w["start"] < w["end"], (
            f"window[{i}] {w['start']}-{w['end']} does not cross midnight correctly; "
            "split it into two windows"
        )


@pytest.mark.parametrize("path", TARIFF_FILES, ids=ids(TARIFF_FILES))
def test_source_provenance_present(path):
    """The citation is part of what the endpoint sells."""
    src = load(path)["source"]
    assert src["document_url"].startswith("https://")
    assert src["retrieved_at"]
    assert src.get("confidence") in {"verified", "parsed_unreviewed"}


@pytest.mark.parametrize("path", TARIFF_FILES, ids=ids(TARIFF_FILES))
def test_filename_matches_tariff_id(path):
    """Guards against the E-21 / E-22 confusion — both are marketed as EZ-3."""
    doc = load(path)
    slug = doc["tariff_id"].split(":", 1)[1].lower()
    assert path.stem.lower().split("@")[0] == slug, (
        f"{path.name} holds tariff_id {doc['tariff_id']}"
    )

