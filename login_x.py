from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
AUTH_DIR = ROOT / "playwright" / ".auth"
AUTH_FILE = AUTH_DIR / "x.json"

def main():
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    AUTH_DIR.chmod(0o700)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome",
            headless=False,
        )
        context = browser.new_context()
        page = context.new_page()

        try:
            page.goto(
                "https://x.com/i/flow/login",
                wait_until="domcontentloaded",
                timeout=60000,
            )

            print("Login X secara manual di browser yang terbuka.")
            print("Masukkan password dan verifikasi langsung di browser.")

            input(
                "\nSetelah berhasil masuk ke Home X, "
                "tekan Enter di Terminal..."
            )

            page.goto(
                "https://x.com/home",
                wait_until="domcontentloaded",
                timeout=60000,
            )

            confirmation = input(
                "\nApakah Home sudah menampilkan akun Anda? "
                "Ketik ya untuk menyimpan session: "
            )

            if confirmation.strip().lower() != "ya":
                print("Session tidak disimpan. Jalankan ulang setelah siap.")
                return

            context.storage_state(path=str(AUTH_FILE))
            AUTH_FILE.chmod(0o600)

            print(f"\nSession tersimpan di: {AUTH_FILE}")

        finally:
            browser.close()


if __name__ == "__main__":
    main()