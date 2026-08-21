"""Ground-truth fixtures for the bill calculator.

No real bill was available for a time-of-use plan, so these fixtures stand in
for one. Every expected value below is written with its arithmetic spelled out
so it can be checked against the filed ratebook with a calculator and no code.

Verify by hand before trusting any of it. If a number here is wrong, the tests
will happily confirm a broken calculator forever.

E-26 rates used (May-Oct 2026 billing cycles, from the filed ratebook):
    summer_peak  on-peak  $0.2566   off-peak  $0.0888
    summer       on-peak  $0.2251   off-peak  $0.0865
    winter       on-peak  $0.1209   off-peak  $0.0891
    monthly service charge  tier_1  $20.00   tier_2  $30.00

Rounding convention: each line item is rounded to cents, then summed. If SRP
sums first and rounds once, totals can differ by a cent on large bills -- worth
confirming against a real bill when one becomes available.
"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tariff.calculate import (
    UnsupportedTariffError,
    calculate_bill,
    classify_period,
    load_tariff,
    srp_holidays,
)

DATA = Path(__file__).resolve().parent.parent / "data" / "tariffs" / "srp"


@pytest.fixture(scope="module")
def e26():
    return load_tariff(DATA / "e-26@2026-05.json")


def flat_day(d, kwh_per_hour=1.0):
    """24 hourly intervals of equal usage."""
    return [(d + timedelta(hours=h), kwh_per_hour) for h in range(24)]


# --------------------------------------------------------------------------
# period classification
# --------------------------------------------------------------------------

class TestClassification:
    """On-peak summer is 2 p.m.-8 p.m. weekdays; winter is 5-9 a.m. and 5-9 p.m."""

    @pytest.mark.parametrize("hour,expected", [
        (13, "off_peak"),   # 1 p.m. -- one hour before the window opens
        (14, "on_peak"),    # 2 p.m. -- first on-peak hour
        (19, "on_peak"),    # 7 p.m. -- last on-peak hour
        (20, "off_peak"),   # 8 p.m. -- window end is exclusive
    ])
    def test_summer_window_edges(self, e26, hour, expected):
        assert classify_period(datetime(2026, 7, 15, hour), e26) == expected

    @pytest.mark.parametrize("hour,expected", [
        (4, "off_peak"),
        (5, "on_peak"),     # morning peak opens
        (8, "on_peak"),
        (9, "off_peak"),    # morning peak closes
        (12, "off_peak"),   # midday gap between the two winter windows
        (17, "on_peak"),    # evening peak opens
        (20, "on_peak"),
        (21, "off_peak"),   # evening peak closes
    ])
    def test_winter_has_two_windows(self, e26, hour, expected):
        assert classify_period(datetime(2026, 1, 7, hour), e26) == expected

    def test_weekend_is_never_on_peak(self, e26):
        # 18 July 2026 is a Saturday
        assert classify_period(datetime(2026, 7, 18, 15), e26) == "off_peak"

    def test_holiday_suspends_on_peak(self, e26):
        # 25 December 2026 falls on a Friday, so it is observed that day
        assert classify_period(datetime(2026, 12, 25, 18), e26) == "off_peak"
        # the preceding Thursday is a normal weekday
        assert classify_period(datetime(2026, 12, 24, 18), e26) == "on_peak"

    def test_observed_holiday_shifts_off_weekend(self):
        # 4 July 2026 is a Saturday, so Independence Day is observed on Friday 3 July
        holidays = srp_holidays(2026)
        assert datetime(2026, 7, 3).date() in holidays
        assert datetime(2026, 7, 4).date() not in holidays


# --------------------------------------------------------------------------
# single-day bills, hand-checkable
# --------------------------------------------------------------------------

class TestDailyBills:

    def test_summer_peak_weekday(self, e26):
        """Wed 15 Jul 2026, 1 kWh/hr, tier_1.

        6 on-peak hours (14:00-19:59) x $0.2566 = $1.5396 -> $1.54
        18 off-peak hours              x $0.0888 = $1.5984 -> $1.60
        energy $3.14 + service $20.00              = $23.14
        """
        bill = calculate_bill(e26, flat_day(datetime(2026, 7, 15)),
                              service_tier="tier_1", billing_month=7)
        assert bill["season"] == "summer_peak"
        assert bill["kwh_by_period"] == {"on_peak": 6.0, "off_peak": 18.0}
        assert bill["energy_total"] == 3.14
        assert bill["total"] == 23.14

    def test_winter_weekday_two_peak_windows(self, e26):
        """Wed 7 Jan 2026, 1 kWh/hr, tier_1.

        8 on-peak hours (4 morning + 4 evening) x $0.1209 = $0.9672 -> $0.97
        16 off-peak hours                       x $0.0891 = $1.4256 -> $1.43
        energy $2.40 + service $20.00                      = $22.40
        """
        bill = calculate_bill(e26, flat_day(datetime(2026, 1, 7)),
                              service_tier="tier_1", billing_month=1)
        assert bill["season"] == "winter"
        assert bill["kwh_by_period"] == {"on_peak": 8.0, "off_peak": 16.0}
        assert bill["energy_total"] == 2.40
        assert bill["total"] == 22.40

    def test_observed_holiday_is_all_off_peak(self, e26):
        """Fri 3 Jul 2026 -- Independence Day observed.

        24 off-peak hours x $0.0888 = $2.1312 -> $2.13
        """
        bill = calculate_bill(e26, flat_day(datetime(2026, 7, 3)),
                              service_tier="tier_1", billing_month=7)
        assert bill["kwh_by_period"] == {"off_peak": 24.0}
        assert bill["energy_total"] == 2.13


# --------------------------------------------------------------------------
# the season / calendar split -- the subtlest thing in the schema
# --------------------------------------------------------------------------

class TestSeasonVsCalendar:
    """Peak HOURS come from the calendar date. PRICES come from the billing cycle.

    The same 15 October day classifies identically either way (2 p.m.-8 p.m. is
    in force through 31 October), but it is priced as summer in the October
    cycle and as winter in the November cycle.
    """

    def test_same_day_prices_differently_by_billing_cycle(self, e26):
        day = flat_day(datetime(2026, 10, 15))

        october = calculate_bill(e26, day, service_tier="tier_1", billing_month=10)
        november = calculate_bill(e26, day, service_tier="tier_1", billing_month=11)

        assert october["kwh_by_period"] == november["kwh_by_period"]

        assert october["season"] == "summer"
        # 6 x $0.2251 = $1.3506 -> $1.35 ; 18 x $0.0865 = $1.557 -> $1.56
        assert october["energy_total"] == 2.91

        assert november["season"] == "winter"
        # 6 x $0.1209 = $0.7254 -> $0.73 ; 18 x $0.0891 = $1.6038 -> $1.60
        assert november["energy_total"] == 2.33


# --------------------------------------------------------------------------
# service charge tiers and guards
# --------------------------------------------------------------------------

class TestChargesAndGuards:

    @pytest.mark.parametrize("tier,expected", [
        ("tier_1", 20.00),
        ("tier_2", 30.00),
        ("tier_3", 40.00),
    ])
    def test_service_charge_tiers(self, e26, tier, expected):
        bill = calculate_bill(e26, [(datetime(2026, 7, 15, 3), 0.0)],
                              service_tier=tier, billing_month=7)
        assert bill["service_charge"] == expected

    def test_unknown_tier_is_rejected(self, e26):
        with pytest.raises(Exception):
            calculate_bill(e26, flat_day(datetime(2026, 7, 15)),
                           service_tier="tier_9", billing_month=7)

    def test_export_credit_plan_is_refused(self):
        """E-28 has a per-exported-kWh credit the schema cannot express.

        Refusing is correct until v1.2 -- a wrong bill for a solar customer is
        worse than no bill.
        """
        with pytest.raises(UnsupportedTariffError):
            load_tariff(DATA / "e-28@2025-11.json")

    def test_result_carries_its_citation(self, e26):
        bill = calculate_bill(e26, flat_day(datetime(2026, 7, 15)),
                              service_tier="tier_1", billing_month=7)
        assert bill["source"]["document_url"].startswith("https://")
        assert bill["source"]["confidence"] in {"verified", "parsed_unreviewed"}

