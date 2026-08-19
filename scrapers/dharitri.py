import io
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import img2pdf
import requests
from bs4 import BeautifulSoup
from PIL import Image

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

BASE_URL = "https://dharitriepaper.in"
EDITION_URL = f"{BASE_URL}/category/4/bhubaneswar"
MIN_IMAGE_BYTES = 50_000
MIN_WIDTH = 700
MIN_HEIGHT = 900
MAX_PAGES = 120

BAD_WORDS = {
    "logo", "icon", "arrow", "download", "facebook", "twitter", "whatsapp",
    "telegram", "instagram", "youtube", "sprite", "banner", "button",
    "share", "close", "menu", "search", "calendar", "loader", "loading",
    "advert", "ads", "placeholder", "clip", "favicon"
}


def unique(items):
    seen = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def normalize_url(value, page_url):
    if not value:
        return None
    value = value.strip().replace("\\/", "/").replace("&amp;", "&")
    if value.startswith("data:"):
        return None
    return urljoin(page_url, value)


def find_today_edition(session):
    r = session.get(EDITION_URL, headers=HEADERS, timeout=(10, 30))
    r.raise_for_status()
    links = unique(
        normalize_url(x, r.url)
        for x in re.findall(r"(?:https?://[^\"' ]+)?/edition/\d+/bhubaneswar[^\"' <]*", r.text, re.I)
    )
    if not links:
        raise RuntimeError("Dharitri: no Bhubaneswar edition links found")
    return links[0]


def edition_id(edition_url):
    m = re.search(r"/edition/(\d+)/", edition_url)
    if not m:
        raise RuntimeError(f"Could not extract edition id from {edition_url}")
    return m.group(1)


def page_url(edition_url, page_no):
    return edition_url.rstrip("/") + f"/page/{page_no}"


def image_candidates(html, page_url_value, page_no):
    soup = BeautifulSoup(html, "html.parser")
    found = []

    def add(url, score=0, reason=""):
        u = normalize_url(url, page_url_value)
        if not u or not re.search(r"\.(?:jpe?g|png|webp)(?:\?|$)", u, re.I):
            return
        low = u.lower()
        penalty = sum(50 for word in BAD_WORDS if word in low)
        page_bonus = 0
        if re.search(rf"(?:page|pg)[_-]?0*{page_no}(?:\D|$)", low, re.I):
            page_bonus += 150
        if str(page_no) in low:
            page_bonus += 20
        if "/uploads/" in low or "/epaper/" in low:
            page_bonus += 40
        found.append((score + page_bonus - penalty, u, reason))

    # Main image-like elements. Attribute dimensions are a useful signal
    # because the viewer's newspaper page is much larger than UI images.
    for tag in soup.find_all(["img", "source"]):
        attrs = tag.attrs
        urls = []
        for key in ("src", "data-src", "data-original", "data-image", "data-img", "data-url"):
            if attrs.get(key):
                urls.append(attrs[key])
        if attrs.get("srcset"):
            for item in attrs["srcset"].split(","):
                urls.append(item.strip().split(" ")[0])

        try:
            w = int(attrs.get("width", 0) or 0)
            h = int(attrs.get("height", 0) or 0)
        except (TypeError, ValueError):
            w = h = 0
        area_score = min((w * h) / 10000, 500) if w and h else 0
        for u in urls:
            add(u, area_score, f"img {w}x{h}")

    # OpenGraph / Twitter image metadata.
    for meta in soup.find_all("meta"):
        prop = (meta.get("property") or meta.get("name") or "").lower()
        if prop in {"og:image", "twitter:image"}:
            add(meta.get("content"), 80, prop)

    # Some versions of the CMS put the viewer image in inline JS/JSON.
    for script in soup.find_all("script"):
        text = script.string or script.get_text() or ""
        for match in re.findall(r"(?:https?:)?//[^\"'\\s]+\.(?:jpe?g|png|webp)(?:\?[^\"'\\s]+)?", text, re.I):
            add(match, 60, "script")
        for match in re.findall(r"[\"']([^\"']+\.(?:jpe?g|png|webp)(?:\?[^\"']*)?)[\"']", text, re.I):
            add(match, 50, "script")

    return unique(sorted(found, key=lambda x: x[0], reverse=True))


def download_and_validate_image(session, url):
    r = session.get(url, headers=HEADERS, timeout=(15, 90))
    r.raise_for_status()
    data = r.content
    if len(data) < MIN_IMAGE_BYTES:
        raise RuntimeError(f"too small ({len(data) / 1024:.1f} KB)")

    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            width, height = im.size
            fmt = im.format
    except Exception as exc:
        raise RuntimeError(f"not a readable image: {exc}") from exc

    if width < MIN_WIDTH or height < MIN_HEIGHT:
        raise RuntimeError(f"too small dimensions ({width}x{height})")

    # Newspaper pages are portrait-oriented. This rejects logos and most UI
    # graphics without assuming an exact pixel size.
    ratio = width / height
    if ratio > 1.15 or ratio < 0.45:
        raise RuntimeError(f"unlikely newspaper page ratio ({width}x{height})")

    return data, width, height, fmt


def find_page_images(session, edition_url):
    pages = []
    print("🔎 Discovering Dharitri page list...")

    for page_no in range(1, MAX_PAGES + 1):
        url = page_url(edition_url, page_no)
        try:
            r = session.get(url, headers=HEADERS, timeout=(10, 30))
            if r.status_code == 404:
                break
            r.raise_for_status()
        except requests.RequestException as exc:
            if page_no == 1:
                raise RuntimeError(f"could not open page 1: {exc}") from exc
            break

        candidates = image_candidates(r.text, r.url, page_no)
        accepted = None
        last_error = None
        for _, candidate, reason in candidates[:20]:
            try:
                data, width, height, fmt = download_and_validate_image(session, candidate)
                accepted = (candidate, data, width, height, fmt)
                break
            except Exception as exc:
                last_error = exc

        if accepted is None:
            # Once a page URL exists but contains no usable newspaper image,
            # treat the edition as incomplete rather than silently shortening it.
            print(f"   ✗ Page {page_no}: no valid page image ({last_error})")
            raise RuntimeError(f"Dharitri page {page_no} has no valid newspaper image")

        candidate, data, width, height, fmt = accepted
        pages.append((page_no, candidate, data, width, height, fmt))
        print(f"   ✓ Page {page_no:02d}  {width}x{height}  {len(data)/1048576:.2f} MB")

    if not pages:
        raise RuntimeError("No Dharitri newspaper pages found")

    return pages


def make_pdf(pages, output_pdf):
    ordered = [data for _, _, data, *_ in sorted(pages, key=lambda x: x[0])]
    with output_pdf.open("wb") as f:
        f.write(img2pdf.convert(ordered))


def download_dharitri() -> str | None:
    today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
    output_pdf = Path(f"Dharitri_bhubaneswar_{today}.pdf")
    session = requests.Session()

    print("=" * 60)
    print(f"📰 DHARITRI — BHUBANESWAR — {today}")
    print("=" * 60)

    try:
        print("🔎 Finding today's edition...")
        edition = find_today_edition(session)
        print(f"✓ Edition: {edition}")

        pages = find_page_images(session, edition)
        page_numbers = [p[0] for p in pages]
        expected = list(range(1, max(page_numbers) + 1))
        if page_numbers != expected:
            raise RuntimeError(
                f"incomplete page sequence: got {page_numbers}, expected 1..{max(page_numbers)}"
            )

        print(f"📚 Found complete sequence: {len(pages)} pages")
        print("🧩 Building PDF...")
        make_pdf(pages, output_pdf)

        size = output_pdf.stat().st_size
        if size < 100_000:
            raise RuntimeError(f"assembled PDF is suspiciously small: {size/1024:.1f} KB")
        with output_pdf.open("rb") as f:
            if f.read(5) != b"%PDF-":
                raise RuntimeError("assembled file is not a PDF")

        print("-" * 60)
        print("✅ Dharitri PDF ready")
        print(f"   Pages: {len(pages)}")
        print(f"   Size : {size / 1048576:.2f} MB")
        print(f"   File : {output_pdf}")
        print("-" * 60)
        return str(output_pdf)

    except Exception as exc:
        output_pdf.unlink(missing_ok=True)
        print(f"❌ Dharitri scraper failed: {type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    download_dharitri()
