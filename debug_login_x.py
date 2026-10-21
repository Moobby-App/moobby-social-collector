from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright


def describe(request):
    host = urlsplit(request.url).hostname
    category = (
        "login/onboarding"
        if "onboarding" in request.url
        else request.resource_type
    )
    return f"{request.method} | {host} | {category}"


def on_response(response):
    request = response.request

    if (
        response.status >= 400
        or "onboarding" in request.url
    ):
        print(
            f"[RESPONSE {response.status}] "
            f"{describe(request)}",
            flush=True,
        )


def on_failed(request):
    print(
        f"[FAILED] {describe(request)} "
        f"| {request.failure}",
        flush=True,
    )


with sync_playwright() as p:
    browser = p.chromium.launch(
        channel="chrome",
        headless=False,
    )
    page = browser.new_page()

    page.on("response", on_response)
    page.on("requestfailed", on_failed)

    try:
        page.goto(
            "https://x.com/i/flow/login",
            wait_until="domcontentloaded",
            timeout=60000,
        )

        print(
            "\nCoba login manual dan tekan tombol yang bermasalah."
            "\nPemantauan berjalan selama 3 menit."
            "\nSession login tidak disimpan.\n",
            flush=True,
        )

        # Tetap memproses event saat Anda berinteraksi di browser.
        page.wait_for_timeout(180_000)

    except KeyboardInterrupt:
        print("\nPemantauan dihentikan.")

    finally:
        browser.close()