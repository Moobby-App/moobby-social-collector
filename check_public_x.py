from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(
        channel="chrome",
        headless=False,
    )
    page = browser.new_page()

    try:
        page.goto(
            "https://x.com/commuterline",
            wait_until="domcontentloaded",
            timeout=60000,
        )

        input(
            "Periksa browser: apakah postingan terlihat, "
            "atau hanya permintaan login? "
            "Tekan Enter untuk menutup..."
        )
    finally:
        browser.close()