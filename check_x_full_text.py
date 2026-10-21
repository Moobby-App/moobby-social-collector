"""Periksa satu postingan tanpa mengubah CSV atau session tersimpan."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout


POST_ID = "2112490082422976818"
POST_URL = "https://x.com/CommuterLine/status/" + POST_ID
ROOT = Path(__file__).resolve().parent


def find_texts(value):
    """Ambil field teks milik ID target saja; abaikan quoted post/balasan."""
    found = []
    if isinstance(value, dict):
        legacy = value.get("legacy")
        legacy = legacy if isinstance(legacy, dict) else {}
        if str(value.get("rest_id", "")) == POST_ID or str(legacy.get("id_str", "")) == POST_ID:
            note = value.get("note_tweet") or {}
            note_result = (note.get("note_tweet_results") or {}).get("result") or {}
            preview = legacy.get("full_text")
            extended = note_result.get("text")
            if isinstance(preview, str) or isinstance(extended, str):
                found.append({
                    "legacy_full_text": preview if isinstance(preview, str) else None,
                    "note_tweet_text": extended if isinstance(extended, str) else None,
                    "legacy_truncated": legacy.get("truncated"),
                })
        for child in value.values():
            found.extend(find_texts(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(find_texts(child))
    return found


def main():
    session = ROOT / "playwright" / ".auth" / "x_reused_session.json"
    if not session.is_file():
        print("Session belum tersedia. Jalankan test_x_existing_session.py dahulu.")
        return
    captured = []
    restricted = []

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=False)
        try:
            context = browser.new_context(storage_state=str(session))
            page = context.new_page()

            def observe(response):
                # Jangan simpan header, cookies, token, atau respons lengkap.
                if "/graphql/" not in response.url:
                    return
                if response.status == 429:
                    restricted.append(True)
                    return
                if not response.ok:
                    return
                try:
                    for item in find_texts(response.json()):
                        if item not in captured:
                            captured.append(item)
                except (ValueError, PlaywrightTimeout):
                    pass

            page.on("response", observe)
            page.goto(POST_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            print("Di browser, periksa postingan KA 1344 yang dimaksud.")
            print('Klik "Show more" / "Tampilkan lebih banyak" bila tersedia.')
            print("Jika ada pembatasan/verifikasi, jangan mencoba melewatinya.")
            input("Setelah teks lengkap terlihat, tekan Enter di Terminal... ")
            page.wait_for_timeout(1000)
            if restricted or "/login" in page.url or "/onboarding/" in page.url or "/account/access" in page.url:
                print("Login/verifikasi atau pembatasan terdeteksi. Pemeriksaan dihentikan.")
                return

            dom_text = None
            show_more_visible = None
            for card in page.locator("article").all():
                links = card.locator("a:has(time)")
                primary = links.first.get_attribute("href") if links.count() else ""
                if not re.search(r"/status/" + POST_ID + r"(?:/|$)", primary or ""):
                    continue
                body = card.locator('[data-testid="tweetText"]').first
                if body.count():
                    dom_text = body.inner_text().strip()
                more = card.get_by_text(re.compile(r"^(show more|tampilkan lebih banyak|lihat selengkapnya)$", re.I))
                show_more_visible = any(item.is_visible() for item in more.all())
                break

            output = {
                "post_id": POST_ID,
                "source_url": POST_URL,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "response_text_candidates": captured,
                "expanded_page_text": dom_text,
                "show_more_visible": show_more_visible,
                "text_completeness": "unknown",
                "review_note": "Bandingkan teks dengan postingan asli; status belum otomatis full.",
            }
            folder = ROOT / "data" / "full_text_checks"
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            target = folder / (POST_ID + "_" + stamp + ".json")
            with target.open("x", encoding="utf-8") as handle:
                json.dump(output, handle, ensure_ascii=False, indent=2)
            notes = [item["note_tweet_text"] for item in captured if item["note_tweet_text"]]
            print("Teks panjang dari respons:", "tersedia" if notes else "belum ditemukan")
            print("Teks dari halaman:", "tersedia" if dom_text else "belum ditemukan")
            if notes:
                print("\n--- Teks panjang dari respons ---\n" + max(notes, key=len))
            if dom_text:
                print("\n--- Teks pada halaman ---\n" + dom_text)
            print("\nHasil pemeriksaan:", target)
            print("CSV dan session sebelumnya tidak diubah.")
        finally:
            browser.close()


if __name__ == "__main__":
    try:
        main()
    except PlaywrightTimeout:
        print("Halaman terlalu lama dimuat. Hasil belum dapat dipastikan.")
    except KeyboardInterrupt:
        print("\nPemeriksaan dibatalkan.")
