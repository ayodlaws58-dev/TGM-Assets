from datetime import date, timedelta

import pytest

from components.themes import easter_sunday, theme_for_date


@pytest.mark.parametrize(("holiday", "name", "label"), [
    (date(2026, 4, 5), "easter", "Easter"),
    (date(2026, 10, 1), "independence", "Nigerian Independence Day"),
    (date(2026, 10, 31), "halloween", "Halloween"),
    (date(2026, 11, 26), "thanksgiving", "Thanksgiving"),
    (date(2026, 12, 25), "christmas", "Christmas"),
])
def test_theme_window(holiday, name, label):
    assert theme_for_date(holiday - timedelta(days=8)) is None
    for days_before in range(8):
        assert theme_for_date(holiday - timedelta(days=days_before)) == {
            "name": name, "label": label,
        }
    assert theme_for_date(holiday + timedelta(days=1)) is None


@pytest.mark.parametrize(("year", "expected"), [
    (2024, date(2024, 3, 31)),
    (2025, date(2025, 4, 20)),
    (2026, date(2026, 4, 5)),
    (2038, date(2038, 4, 25)),
])
def test_easter_sunday(year, expected):
    assert easter_sunday(year) == expected


@pytest.mark.parametrize("holiday", [
    date(2024, 11, 28), date(2025, 11, 27), date(2030, 11, 28),
])
def test_thanksgiving_moves_with_calendar(holiday):
    assert theme_for_date(holiday)["name"] == "thanksgiving"
    assert theme_for_date(holiday + timedelta(days=1)) is None


def test_no_theme_at_year_boundary():
    assert theme_for_date(date(2026, 12, 31)) is None
    assert theme_for_date(date(2027, 1, 1)) is None
