from datetime import date, timedelta


def easter_sunday(year):
    """Calculate Gregorian Easter Sunday."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def theme_for_date(today):
    november_first = date(today.year, 11, 1)
    thanksgiving = november_first + timedelta(days=(3 - november_first.weekday()) % 7 + 21)
    holidays = (
        ("easter", "Easter", easter_sunday(today.year)),
        ("independence", "Nigerian Independence Day", date(today.year, 10, 1)),
        ("halloween", "Halloween", date(today.year, 10, 31)),
        ("thanksgiving", "Thanksgiving", thanksgiving),
        ("christmas", "Christmas", date(today.year, 12, 25)),
    )
    for name, label, holiday in holidays:
        if holiday - timedelta(days=15) <= today <= holiday:
            return {"name": name, "label": label}
    return None
