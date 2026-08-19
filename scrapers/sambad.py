import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import img2pdf
import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
}

BASE = "https://sambadepaper.com/epaperimages"
MAX_PAGES = 100


def _is_jpeg(data: bytes) -> bool:
    return len(data) >= 4 and data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9"


def _download_page(session, url, path, page):
    response = session.get(
        url,
        headers=HEADERS,
        timeout=(10, 30),
        allow_redirects=True,
    )

    if response.status_code != 200:
        return False, f"HTTP {response.status_code}"

    content_type = response.headers.get("Content-Type", "").lower()

    # The Sambad endpoint should return an actual JPEG. Reject HTML/UI
    # responses rather than putting them into the PDF.
    if "image/" not in content_type and not _is_jpeg(response.content):
        return False, f"unexpected Content-Type: {content_type or 'unknown'}"

    if not _is_jpeg(response.content):
        return False, "response is not a JPEG"

    size = len(response.content)

    # Newspaper pages should be substantially larger than tiny UI assets.
    if size < 50_000:
        return False, f"image too small ({size / 1024:.1f} KB)"

    path.write_bytes(response.content)
    return True, f"{size / 1048576:.2f} MB"


def download_sambad() -> str | None:
    """Download today's Sambad Bhubaneswar e-paper pages and build a PDF."""

    today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d%m%Y")
    output_pdf = f"Sambad_{today}.pdf"

    downloaded = []

    session = requests.Session()

    try:
        print(f"📰 SAMBAD — {today}")
        print("🔎 Downloading page images...")

        for page in range(1, MAX_PAGES + 1):
            filename = f"sambad_{page:02d}.jpg"
            path = Path(filename)

            url = f"{BASE}/{today}/{today}-md-hr-{page}.jpg"

            ok, detail = _download_page(session, url, path, page)

            if not ok:
                if page == 1:
                    print(f"❌ Page 01 unavailable: {detail}")
                    return None

                print(f"⏹ Page {page:02d}: {detail}")
                print(f"✓ End of edition — {len(downloaded)} pages found")
                break

            downloaded.append(str(path))
            print(f"✓ Page {page:02d} — {detail}")

        if not downloaded:
            return None

        print(f"📦 Building PDF from {len(downloaded)} pages...")

        with open(output_pdf, "wb") as f:
            f.write(img2pdf.convert(downloaded))

        size_mb = os.path.getsize(output_pdf) / 1048576
        print(f"✓ Sambad PDF ready: {len(downloaded)} pages / {size_mb:.1f} MB")

        return output_pdf

    except Exception as exc:
        print(f"❌ Error downloading Sambad: {type(exc).__name__}: {exc}")
        return None

    finally:
        for filename in downloaded:
            try:
                os.remove(filename)
            except OSError:
                pass


if __name__ == "__main__":
    download_sambad()
