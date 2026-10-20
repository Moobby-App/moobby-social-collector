import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

MONTHS = {
    name: index
    for index, name in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        start=1,
    )
}


def extract_date(raw_text, account, collected_at):
    result = {
        "published_label": None,
        "published_date": None,
        "published_at": None,
        "date_basis": "unknown",
        "date_precision": "unknown",
    }

    lines = [
        line.strip()
        for line in raw_text.splitlines()
        if line.strip()
    ]

    # Baca baris tepat setelah username, bukan tanggal di isi berita.
    marker = f"@{account}".casefold()

    for index, line in enumerate(lines[:-1]):
        if line.casefold() == marker:
            label = lines[index + 1]
            break
    else:
        return result

    reference = datetime.fromisoformat(collected_at)
    reference = reference.astimezone(ZoneInfo("Asia/Jakarta"))

    # Label relatif: hanya perkiraan, bukan timestamp pasti.
    relative = re.fullmatch(r"(\d+)(s|m|h|d)", label)

    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        seconds = amount * {
            "s": 1,
            "m": 60,
            "h": 3600,
            "d": 86400,
        }[unit]

        estimated = reference - timedelta(seconds=seconds)

        result.update(
            published_label=label,
            date_basis="relative_label_estimate",
            date_precision="approximate",
            published_date_estimate=estimated.date().isoformat(),
        )
        return result

    absolute = re.fullmatch(
        r"([A-Z][a-z]{2}) (\d{1,2})(?:, (\d{4}))?",
        label,
    )

    if not absolute or absolute.group(1) not in MONTHS:
        return result

    month = MONTHS[absolute.group(1)]
    day = int(absolute.group(2))
    explicit_year = absolute.group(3)
    year = int(explicit_year) if explicit_year else reference.year

    try:
        date = datetime(
            year, month, day,
            tzinfo=ZoneInfo("Asia/Jakarta"),
        ).date()

        if not explicit_year and date > reference.date():
            date = date.replace(year=year - 1)
    except ValueError:
        return result

    result["published_label"] = label

    if explicit_year:
        result.update(
            published_date=date.isoformat(),
            date_basis="displayed_date",
            date_precision="day",
        )
    else:
        result.update(
            published_date_estimate=date.isoformat(),
            date_basis="year_inferred_from_collection",
            date_precision="approximate",
        )

    return result