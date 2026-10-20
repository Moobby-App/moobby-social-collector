import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import shutil
from pathlib import Path
import argparse
from post_dates import extract_date

from playwright.sync_api import sync_playwright, TimeoutError

ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument("--account", default="commuterline")
parser.add_argument("--limit", type=int, default=20)
args = parser.parse_args()
if not 1 <= args.limit <= 100:
    parser.error("Limit harus antara 1 dan 100.")

ACCOUNT = args.account.lstrip("@")

if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", ACCOUNT):
    parser.error("Username X tidak valid.")

PROFILE_URL = f"https://x.com/{ACCOUNT}"
OUTPUT = ROOT / "data" / f"x_{ACCOUNT.lower()}_posts.json"

POST_PATH = re.compile(
    r"^/([A-Za-z0-9_]+)/status/(\d+)(?:/.*)?$"
)


def main():
    posts = {}
    session = ROOT / "playwright" / ".auth" / "x_reused_session.json"
    if not session.is_file():
        print("Session belum tersedia. Jalankan test_x_existing_session.py terlebih dahulu.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome",
            headless=False,
        )
        context = browser.new_context(storage_state=str(session))
        page = context.new_page()
        rate_limited = []
        def observe_response(response):
            from urllib.parse import urlsplit
            host = urlsplit(response.url).hostname or ""
            if response.status == 429 and (host == "x.com" or host.endswith(".x.com")):
                rate_limited.append(True)
        page.on("response", observe_response)

        try:
            page.goto(
                PROFILE_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )

            try:
                page.locator('[data-testid="SideNav_AccountSwitcher_Button"]').wait_for(
                    state="visible", timeout=30000,
                )
            except TimeoutError:
                print("Login belum terkonfirmasi. File lama tidak ditimpa.")
                print("Periksa apakah session kedaluwarsa atau X meminta verifikasi.")
                return


            cards = page.locator("article")

            try:
                cards.first.wait_for(
                    state="attached",
                    timeout=15000,
                )
            except TimeoutError:
                print("Tidak ada article. File lama tidak ditimpa.")
                return

            stop_reason = "max_scrolls_reached"
            stale_rounds = 0
            max_scrolls = 40
            restriction = re.compile(
                r"rate limit exceeded|too many requests|temporarily limited|"
                r"verify your identity|authenticate your account|"
                r"batas permintaan|verifikasi identitas", re.I,
            )
            for round_index in range(max_scrolls + 1):
                if rate_limited:
                    stop_reason = "rate_limited"
                    break
                if any(item.is_visible() for item in page.get_by_text(restriction).all()):
                    stop_reason = "verification_or_restriction"
                    break
                if "/i/flow/login" in page.url or "/onboarding/" in page.url or "/account/access" in page.url:
                    stop_reason = "login_or_verification_required"
                    break
                before = len(posts)
                for index, card in enumerate(cards.all(), start=1):
                    try:
                        # Utamakan permalink yang membungkus waktu postingan utama.
                        primary = card.locator('a:has(time)').first
                        primary_href = primary.get_attribute("href") if primary.count() else None
                        hrefs = card.locator(
                            'a[href*="/status/"]'
                        ).evaluate_all(
                            "(links) => links.map("
                            "el => el.getAttribute('href'))"
                        )

                        if primary_href:
                            hrefs.insert(0, primary_href)
                        match = next(
                            (
                                POST_PATH.fullmatch(href)
                                for href in hrefs
                                if href and POST_PATH.fullmatch(href)
                            ),
                            None,
                        )

                        if match is None:
                            print(f"Kartu {index}: URL tidak ditemukan.")
                            continue

                        username, post_id = match.groups()
                        if post_id in posts:
                            continue
                        source_url = (
                            f"https://x.com/{username}/status/{post_id}"
                        )

                        raw_text = card.inner_text().strip()

                        if not raw_text:
                            print(f"Kartu {index}: teks kosong.")
                            continue

                        collected_at = datetime.now(timezone.utc).isoformat()

                        date_info = extract_date(
                            raw_text,
                            username,
                            collected_at,
                        )

                        # Hanya gunakan timestamp dari permalink ID yang dipilih;
                        # jangan mengambil waktu dari quoted post.
                        for link in card.locator('a:has(time)').all():
                            link_match = POST_PATH.fullmatch(link.get_attribute("href") or "")
                            if not link_match or link_match.group(2) != post_id:
                                continue
                            value = link.locator("time").first.get_attribute("datetime")
                            if value:
                                try:
                                    published = datetime.fromisoformat(value.replace("Z", "+00:00"))
                                    if published.tzinfo is None:
                                        continue
                                    date_info = {
                                        "published_label": link.locator("time").first.inner_text(),
                                        "published_at": published.astimezone(timezone.utc).isoformat(),
                                        "published_date": published.astimezone(ZoneInfo("Asia/Jakarta")).date().isoformat(),
                                        "date_basis": "dom_datetime",
                                        "date_precision": "second",
                                    }
                                    break
                                except ValueError:
                                    pass

                        posts[post_id] = {
                            "id": post_id,
                            "source_account": username,
                            "source_url": source_url,
                            "raw_card_text": raw_text,
                            "collected_at": collected_at,
                            **date_info,
                            "verification_status": "unverified",
                            "curation_status": "not_reviewed",
                        }
                        if len(posts) >= args.limit:
                            break

                    except Exception as error:
                        print(
                            f"Kartu {index} dilewati: "
                            f"{type(error).__name__}"
                        )
                print(f"Tahap {round_index + 1}: {len(posts)}/{args.limit} postingan unik.")
                if len(posts) >= args.limit:
                    stop_reason = "target_reached"
                    break
                stale_rounds = stale_rounds + 1 if len(posts) == before else 0
                if stale_rounds >= 5:
                    stop_reason = "no_new_posts"
                    break
                if round_index == max_scrolls:
                    break
                # Scroll kurang dari satu layar agar kartu tidak terlewat.
                # Jeda untuk pemuatan konten, bukan penyamaran otomatisasi.
                page.evaluate("window.scrollBy(0, Math.floor(window.innerHeight * 0.75))")
                page.wait_for_timeout(3000)
            print(f"Alasan berhenti: {stop_reason}")

            if not posts:
                print("Tidak ada hasil. File lama tidak ditimpa.")
                return

            OUTPUT.parent.mkdir(parents=True, exist_ok=True)

            result = {
                "profile_url": PROFILE_URL,
                "coverage": "loaded_authenticated_posts_only",
                "complete_history": False,
                "target_count": args.limit,
                "target_reached": len(posts) >= args.limit,
                "stop_reason": stop_reason,
                "count": len(posts),
                "posts": list(posts.values()),
            }

            if OUTPUT.exists():
                backup = OUTPUT.with_name(OUTPUT.stem + ".backup_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
                shutil.copy2(OUTPUT, backup)
                print(f"Backup hasil sebelumnya: {backup.name}")

            temporary = OUTPUT.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(
                    result,
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            temporary.replace(OUTPUT)
            exact_count = sum(post.get("date_basis") == "dom_datetime" for post in posts.values())
            print(f"Timestamp lengkap: {exact_count}/{len(posts)} postingan.")
            print(f"Berhasil menyimpan {len(posts)} postingan.")
            print(f"File: {OUTPUT}")

        finally:
            browser.close()


if __name__ == "__main__":
    main()