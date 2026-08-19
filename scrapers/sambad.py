import os
from datetime import datetime
from pathlib import Path
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
MIN_IMAGE_SIZE = 50_000


def is_jpeg(data: bytes) -> bool:
    return (
        len(data) >= 4
        and data[:2] == b"\xff\xd8"
        and data[-2:] == b"\xff\xd9"
    )


def download_page(session: requests.Session, url: str, path: Path, page: int):
    response = session.get(
        url,
        headers=HEADERS,
        timeout=(10, 45),
        allow_redirects=True,
    )

    if response.status_code != 200:
        return False, f"HTTP {response.status_code}"

    content_type = response.headers.get("Content-Type", "").lower()

    if "image/" not in content_type and not is_jpeg(response.content):
        return False, f"not an image ({content_type or 'unknown content-type'})"

    if not is_jpeg(response.content):
        return False, "response is not JPEG"

    size = len(response.content)

    if size < MIN_IMAGE_SIZE:
        return False, f"image too small ({size / 1024:.1f} KB)"

    path.write_bytes(response.content)
    return True, f"{size / 1048576:.2f} MB"


def download_sambad() -> str | None:
    today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d%m%Y")
    output_pdf = Path(f"Sambad_bhubaneswar_{today}.pdf")
    downloaded = []

    print("=" * 60)
    print(f"📰 SAMBAD — BHUBANESWAR — {today}")
    print("=" * 60)
    print("🔎 Using direct Sambad e-paper image endpoint")

    session = requests.Session()

    try:
        for page in range(1, MAX_PAGES + 1):
            filename = f"sambad_{page:02d}.jpg"
            path = Path(filename)

            url = f"{BASE}/{today}/{today}-md-hr-{page}.jpg"

            print(f"📥 Page {page:02d}...", end=" ", flush=True)

            ok, detail = download_page(session, url, path, page)

            if not ok:
                print(f"⏹ {detail}")

                if page == 1:
                    raise RuntimeError(
                        f"Sambad page 1 unavailable: {detail}"
                    )

                break

            downloaded.append(str(path))
            print(f"✓ {detail}")

        if not downloaded:
            raise RuntimeError("No Sambad pages downloaded")

        print("-" * 60)
        print(f"📄 Building PDF from {len(downloaded)} pages...")

        with output_pdf.open("wb") as pdf:
            pdf.write(img2pdf.convert(downloaded))

        size_mb = output_pdf.stat().st_size / 1048576

        print(f"✅ Sambad PDF ready")
        print(f"   Pages : {len(downloaded)}")
        print(f"   Size  : {size_mb:.1f} MB")
        print(f"   File  : {output_pdf}")

        return str(output_pdf)

    except Exception as exc:
        print(f"❌ Sambad scraper failed: {type(exc).__name__}: {exc}")
        raise

    finally:
        for filename in downloaded:
            try:
                os.remove(filename)
            except OSError:
                pass


if __name__ == "__main__":
    download_sambad()
