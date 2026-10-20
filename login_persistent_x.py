from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
PROFILE = ROOT / "playwright" / ".auth" / "chrome-profile"

PROFILE.mkdir(parents=True, exist_ok=True)
PROFILE.chmod(0o700)

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE),
        channel="chrome",
        headless=False,
    )

    page = context.pages[0] if context.pages else context.new_page()

    try:
        page.goto(
            "https://x.com/i/flow/login",
            wait_until="domcontentloaded",
            timeout=60000,
        )

        print("Login manual melalui browser.")
        print("Browser akan tetap terbuka selama 5 menit.")
        print("Session disimpan dalam profil khusus collector.")

        page.wait_for_timeout(300_000)

    except KeyboardInterrupt:
        print("Dihentikan.")

    finally:
        context.close()