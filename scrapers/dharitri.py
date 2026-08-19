import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

BASE_URL = "https://dharitriepaper.in"
EDITION_URL = f"{BASE_URL}/category/4/bhubaneswar"

MIN_PDF_SIZE = 100_000


def find_pdf_url(html: str, page_url: str) -> str | None:
    """Find a real PDF URL exposed by the Dharitri page."""

    # Normal href/src references.
    patterns = [
        r'''href=["']([^"']+\.pdf(?:\?[^"']*)?)["']''',
        r'''src=["']([^"']+\.pdf(?:\?[^"']*)?)["']''',
        r'''["']([^"']+\.pdf(?:\?[^"']*)?)["']''',
    ]

    candidates = []

    for pattern in patterns:
        candidates.extend(re.findall(pattern, html, re.I))

    # Prefer candidates that look like the full edition.
    preferred = []
    for url in candidates:
        lower = url.lower()

        if any(x in lower for x in ("full", "edition", "epaper", "epaperpdf")):
            preferred.append(url)

    candidates = preferred + candidates

    for url in candidates:
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = BASE_URL + url
        elif not url.startswith(("http://", "https://")):
            url = requests.compat.urljoin(page_url, url)

        if url.lower().endswith(".pdf") or ".pdf?" in url.lower():
            return url

    return None


def download_dharitri() -> str | None:
    today = datetime.now(
        ZoneInfo("Asia/Kolkata")
    ).strftime("%Y-%m-%d")

    output_pdf = Path(
        f"Dharitri_bhubaneswar_{today}.pdf"
    )

    session = requests.Session()

    print("=" * 60)
    print(f"📰 DHARITRI — BHUBANESWAR — {today}")
    print("=" * 60)

    try:
        print("🔎 Finding today's edition...")

        response = session.get(
            EDITION_URL,
            headers=HEADERS,
            timeout=(10, 30),
        )
        response.raise_for_status()

        print(f"Edition page: {response.url}")

        pdf_url = find_pdf_url(
            response.text,
            response.url,
        )

        if not pdf_url:
            raise RuntimeError(
                "Could not find the actual Dharitri Full PDF URL"
            )

        print(f"📄 PDF candidate: {pdf_url}")
        print("📥 Downloading Dharitri PDF...")

        with session.get(
            pdf_url,
            headers=HEADERS,
            timeout=(15, 120),
            stream=True,
        ) as pdf_response:

            pdf_response.raise_for_status()

            content_type = (
                pdf_response.headers
                .get("Content-Type", "")
                .lower()
            )

            total = 0

            with output_pdf.open("wb") as f:
                for chunk in pdf_response.iter_content(
                    chunk_size=1024 * 1024
                ):
                    if not chunk:
                        continue

                    f.write(chunk)
                    total += len(chunk)

                    print(
                        f"\r📥 {total / 1048576:.1f} MB",
                        end="",
                        flush=True,
                    )

        print()

        # Validate the downloaded file.
        if not output_pdf.exists():
            raise RuntimeError("PDF file was not created")

        size = output_pdf.stat().st_size

        if size < MIN_PDF_SIZE:
            raise RuntimeError(
                f"PDF is suspiciously small: "
                f"{size / 1024:.1f} KB"
            )

        with output_pdf.open("rb") as f:
            header = f.read(5)

        if header != b"%PDF-":
            raise RuntimeError(
                "Downloaded file is not a valid PDF"
            )

        print("-" * 60)
        print("✅ Dharitri PDF downloaded")
        print(f"   Size : {size / 1048576:.1f} MB")
        print(f"   File : {output_pdf}")
        print("-" * 60)

        return str(output_pdf)

    except Exception as exc:
        print(
            f"❌ Dharitri scraper failed: "
            f"{type(exc).__name__}: {exc}"
        )

        if output_pdf.exists():
            output_pdf.unlink()

        raise


if __name__ == "__main__":
    download_dharitri()