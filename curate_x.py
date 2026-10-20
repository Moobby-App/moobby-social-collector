import json
import re
from pathlib import Path
import argparse
from datetime import date, datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument("--account", default="commuterline")
args = parser.parse_args()

account = args.account.lstrip("@").lower()

if not re.fullmatch(r"[a-z0-9_]{1,15}", account):
    parser.error("Username X tidak valid.")

INPUT = ROOT / "data" / f"x_{account}_posts.json"
OUTPUT = ROOT / "data" / f"x_{account}_curated.json"

LOCATIONS = [
    "tanah abang", "palmerah", "kebayoran",
    "pondok ranji", "jurangmangu", "sudimara",
    "rawa buntu", "serpong", "cisauk", "cicayur",
    "parung panjang", "parungpanjang", "cilejit",
    "daru", "tenjo", "tigaraksa", "cikoya",
    "maja", "citeras", "rangkasbitung", "green line",
]

EVENTS = [
    "gangguan", "keterlambatan", "terlambat",
    "tertunda", "dibatalkan", "terhenti",
    "rekayasa pola operasi", "penutupan", "ditutup",
    "anjlok", "banjir", "genangan", "pohon tumbang",
    "kecelakaan", "kebakaran", "unjuk rasa",
]

STATION_INCIDENTS = [
    "mengemis",
    "pencopetan",
    "copet",
    "pelecehan",
    "perkelahian",
    "terjebak",
]


def find_terms(text, terms):
    return [
        term for term in terms
        if re.search(
            r"(?<!\w)" + re.escape(term) + r"(?!\w)",
            text,
        )
    ]

def check_age(post):
    value = (
        post.get("published_date")
        or post.get("published_date_estimate")
    )

    if not value:
        return "unknown", None

    try:
        published = date.fromisoformat(value)
    except ValueError:
        return "unknown", None

    today = datetime.now(
        ZoneInfo("Asia/Jakarta")
    ).date()

    age = (today - published).days

    if age < 0:
        return "future_date", age

    if age > 7:
        return "historical", age

    if post.get("date_precision") == "approximate":
        return "recent_estimate", age

    return "recent", age

def main():
    data = json.loads(INPUT.read_text(encoding="utf-8"))
    overrides_file = ROOT / "data" / "review_overrides.json"

    overrides = (
        json.loads(overrides_file.read_text(encoding="utf-8"))
        if overrides_file.exists()
        else {}
    )
    results = []

    for post in data["posts"]:
        post = {
            **post,
            **overrides.get(post["id"], {}),
        }
        
        text = re.sub(
            r"\s+", " ", post["raw_card_text"].casefold()
        ).strip()

        locations = find_terms(text, LOCATIONS)
        events = find_terms(text, EVENTS)
        station_incidents = find_terms(text, STATION_INCIDENTS)

        if locations and events:
            category = "possible_travel_disruption"
            status = "needs_review"
            reason = "Ada penyebutan lokasi dan kata kejadian perjalanan."

        elif locations and station_incidents:
            category = "station_incident"
            status = "needs_review"
            reason = (
                "Ada indikasi kejadian di sekitar stasiun. "
                "Dampak terhadap perjalanan belum diketahui."
            )

        elif events:
            category = "possible_travel_disruption"
            status = "needs_location_review"
            reason = "Ada kata kejadian, lokasi Green Line belum jelas."

        else:
            category = "general_information"
            status = "not_selected"
            reason = "Tidak ditemukan indikasi kejadian yang dicakup filter."

        temporal_status, age_days = check_age(post)

        # Simpan hasil filter relevansi sebelum menerapkan filter tanggal.
        relevance_status = status

        if temporal_status == "historical":
            status = "archived"
            reason += " Tanggal publikasi di luar batas 7 hari."

        elif temporal_status in ("unknown", "future_date"):
            status = (
                "needs_date_review"
                if relevance_status != "not_selected"
                else "not_selected"
            )

        elif (
            temporal_status == "recent_estimate"
            and relevance_status != "not_selected"
        ):
            status = "needs_date_review"
            reason += " Tanggal masih perkiraan; perlu dikonfirmasi."

        results.append({
            **post,
            "curation_status": status,
            "matched_locations": locations,
            "matched_events": events,
            "curation_reason": reason,
            "text_may_be_truncated": "show more" in text,
            "operational_impact": "unknown",
            "relevance_status": relevance_status,
            "temporal_status": temporal_status,
            "publication_age_days": age_days,
            "category": category,
            "matched_station_incidents": station_incidents,
        })

    OUTPUT.write_text(
        json.dumps(
            {"count": len(results), "posts": results},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    for post in results:
        print(f'{post["id"]}: {post["curation_status"]}')

    print(f"\nHasil tersimpan: {OUTPUT}")


if __name__ == "__main__":
    main()