import os
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
AUTH_DIR = ROOT / "playwright" / ".auth"

PROFILE = AUTH_DIR / "firefox-profile"
APP_HOME = AUTH_DIR / "firefox-app-home"

for folder in (AUTH_DIR, PROFILE, APP_HOME):
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)

# Hanya berlaku untuk proses Firefox yang dijalankan script ini.
firefox_env = {
    **os.environ,
    "CFFIXED_USER_HOME": str(APP_HOME),
}

with sync_playwright() as p:
    context = p.firefox.launch_persistent_context(
        user_data_dir=str(PROFILE),
        headless=False,
        env=firefox_env,
    )

    try:
        page = (
            context.pages[0]
            if context.pages
            else context.new_page()
        )

        page.goto(
            "https://x.com/i/flow/login",
            wait_until="domcontentloaded",
            timeout=60000,
        )

        print("Firefox berhasil terbuka.")
        print("Silakan login manual. Waktu tersedia 5 menit.")

        page.wait_for_timeout(300_000)

    finally:
        context.close()