"""Uji session X milik sendiri; masukkan cookies lokal, bukan lewat chat."""
from getpass import getpass
from pathlib import Path
import os

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


def main():
    auth_token = getpass("Tempel Value cookie auth_token (tidak ditampilkan): ").strip()
    ct0 = getpass("Tempel Value cookie ct0 (tidak ditampilkan): ").strip()
    if not auth_token or not ct0:
        print("Cookie kosong. Pengujian dibatalkan.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=False)
        try:
            context = browser.new_context()
            context.add_cookies([
                {"name": name, "value": value, "domain": ".x.com", "path": "/", "secure": True, "httpOnly": name == "auth_token", "sameSite": "Lax"}
                for name, value in (("auth_token", auth_token), ("ct0", ct0))
            ])
            page = context.new_page()
            page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60000)
            try:
                page.locator('[data-testid="SideNav_AccountSwitcher_Button"]').wait_for(state="visible", timeout=45000)
            except PlaywrightTimeoutError:
                print("Status login belum terkonfirmasi. Session tidak disimpan.")
                print("Periksa browser: apakah beranda, form login, atau pesan pembatasan?")
            else:
                folder = Path(__file__).resolve().parent / "playwright" / ".auth"
                folder.mkdir(parents=True, exist_ok=True)
                folder.chmod(0o700)
                target = folder / "x_reused_session.json"
                # Buat file privat sebelum Playwright menulis session.
                fd = os.open(str(target), os.O_CREAT | os.O_WRONLY, 0o600)
                os.close(fd)
                target.chmod(0o600)
                context.storage_state(path=str(target))
                print("Login terkonfirmasi. Session tersimpan lokal di playwright/.auth/x_reused_session.json")
            print("Browser tetap terbuka selama 60 detik untuk pemeriksaan. Tidak ada postingan dikirim.")
            page.wait_for_timeout(60000)
        finally:
            browser.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nPengujian dihentikan.")
