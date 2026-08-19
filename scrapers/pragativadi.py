import hashlib
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo
import img2pdf
import requests
from bs4 import BeautifulSoup

import io
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from PIL import Image

MIN_IMAGE_BYTES = 50_000
MIN_WIDTH = 700
MIN_HEIGHT = 900

_BAD_WORDS = {
    "logo", "icon", "arrow", "social", "menu", "favicon", "sprite",
    "banner", "button", "share", "close", "search", "calendar",
    "loader", "loading", "placeholder", "advert", "ads", "thumb",
    "thumbnail", "avatar"
}

def _normalize_url(value, page_url):
    if not value:
        return None
    value = str(value).strip().replace("\\/", "/").replace("&amp;", "&")
    if value.startswith(("data:", "javascript:")):
        return None
    return urljoin(page_url, value)

def _looks_like_image(data, content_type):
    return (
        content_type.startswith("image/")
        or data[:3] == b"\xff\xd8\xff"
        or data[:8] == b"\x89PNG\r\n\x1a\n"
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
    )

def _image_info(data):
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            return im.width, im.height, im.format
    except Exception:
        return None

def _extract_image_candidates(html, page_url, page_no):
    soup = BeautifulSoup(html, "html.parser")
    candidates = []

    def add(raw, score=0, reason=""):
        u = _normalize_url(raw, page_url)
        if not u:
            return
        low = u.lower()
        if low.startswith(("mailto:", "tel:")):
            return
        ext = re.search(r"\.(?:jpe?g|png|webp)(?:[?#]|$)", low)
        if not ext and not any(k in low for k in ("image", "img", "page", "epaper", "epaperimage")):
            return
        score2 = score
        score2 += 220 if re.search(rf"(?:page|pg|pageno|page_no|pageNo)[_-]?0*{page_no}(?:\D|$)", low, re.I) else 0
        score2 += 80 if re.search(rf"(?:^|[/_-])0*{page_no}(?:\.(?:jpg|jpeg|png|webp)|[/?_-])", low, re.I) else 0
        score2 += 40 if any(k in low for k in ("epaper", "epaperimage", "newspaper", "uploads")) else 0
        score2 -= sum(100 for w in _BAD_WORDS if w in low)
        candidates.append((score2, u, reason))

    for tag in soup.find_all(["img", "source"]):
        attrs = tag.attrs
        try:
            w, h = int(attrs.get("width") or 0), int(attrs.get("height") or 0)
        except (TypeError, ValueError):
            w = h = 0
        area_score = min((w*h)/10000, 600) if w and h else 0
        for key in ("src", "data-src", "data-original", "data-image", "data-img",
                    "data-url", "data-lazy-src", "data-filename"):
            if attrs.get(key):
                add(attrs[key], area_score + 120, f"{key} {w}x{h}")
        if attrs.get("srcset"):
            for item in str(attrs["srcset"]).split(","):
                add(item.strip().split()[0], area_score + 100, "srcset")

    for tag in soup.find_all("meta"):
        prop = (tag.get("property") or tag.get("name") or "").lower()
        if prop in {"og:image", "twitter:image"}:
            add(tag.get("content"), 50, prop)

    for script in soup.find_all("script"):
        text = script.string or script.get_text() or ""
        for m in re.finditer(
            r'(?:(?:https?:)?//|/)[^"\'\\\s<>]+?(?:\.(?:jpe?g|png|webp)(?:\?[^"\'\\\s<>]*)?|(?:image|img|page)[^"\'\\\s<>]{0,100})',
            text, re.I
        ):
            add(m.group(0), 90, "script")
        for m in re.finditer(r'["\']([^"\']{3,300})["\']', text):
            raw = m.group(1)
            if any(k in raw.lower() for k in ("image", "img", "epaper", "page")):
                add(raw, 30, "script-keyword")

    for tag in soup.find_all(True):
        for key, value in tag.attrs.items():
            if key.startswith("data-") or key.lower() in {"onclick", "href"}:
                if isinstance(value, list):
                    value = " ".join(value)
                if isinstance(value, str):
                    for raw in re.findall(r'(?:https?:)?//[^"\'\s)]+|/[^"\'\s)]+', value):
                        add(raw, 30, f"{key}")

    best = {}
    for score, u, reason in candidates:
        if u not in best or score > best[u][0]:
            best[u] = (score, u, reason)
    return sorted(best.values(), key=lambda x: x[0], reverse=True)

def _download_candidate(session, url, referer):
    r = session.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 Chrome/120 Safari/537.36",
                 "Referer": referer},
        timeout=(5, 10),
        allow_redirects=True,
    )
    if not r.ok:
        return None
    data = r.content
    ctype = r.headers.get("Content-Type", "").lower()
    if len(data) < MIN_IMAGE_BYTES or not _looks_like_image(data, ctype):
        return None
    info = _image_info(data)
    if not info:
        return None
    width, height, fmt = info
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        return None
    ratio = width / height
    if ratio > 1.35 or ratio < 0.40:
        return None
    return data, width, height, fmt, r.url

BASE = "https://epaper.pragativadi.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}

def _image_from_page(session, page_url, page_no, seen):
    response = session.get(page_url, headers=HEADERS, timeout=(5, 25))
    response.raise_for_status()
    candidates = _extract_image_candidates(response.text, response.url, page_no)

    # Viewer routes are HTML pages, not newspaper images. Remove them before
    # downloading candidates so /page/N cannot win as a false image source.
    filtered = []
    for score, u, reason in candidates:
        low = u.lower()
        if re.search(r"/edition/\d+/[^/]+/page(?:/|$)", urlparse(u).path, re.I):
            continue
        if re.search(r"/edition/\d+/[^/]+/page(?:/|$)", urlparse(u).path, re.I):
            continue
        if "default.jpg" in low or "imageprocessor" in low and "default.jpg" in low:
            continue
        # Reject non-image viewer/navigation URLs even when they contain /page/.
        if re.search(r"/(?:page|edition)/[^.?#]*$", urlparse(u).path, re.I):
            continue
        filtered.append((score, u, reason))
    candidates = filtered

    # Direct e-paper uploads are substantially more trustworthy than generic
    # page assets. Keep the requested page number as the primary signal.
    for i, (score, u, reason) in enumerate(candidates):
        low = u.lower()
        if "/uploads/epaper/" in low:
            candidates[i] = (score + 250, u, reason + " direct-epaper")
        if re.search(rf"(?:page|pg|pageno|page_no|pageNo)[_-]?0*{page_no}(?:\\D|$)", low, re.I):
            candidates[i] = (candidates[i][0] + 500, u, reason + " exact-page")

    ranked = []
    limit = min(len(candidates), 12)
    for idx, (score, u, reason) in enumerate(candidates[:limit], 1):
        print(f"   🔎 image candidate {idx}/{limit} — {u[:140]}", flush=True)
        try:
            result = _download_candidate(session, u, response.url)
        except requests.RequestException:
            continue
        if not result:
            continue
        data, width, height, fmt, final_url = result
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen:
            score -= 1000
        ranked.append((score, data, final_url, width, height, reason, digest))
    if not ranked:
        return None
    return max(ranked, key=lambda x: x[0])

def download_pragativadi():
    d = datetime.now(ZoneInfo("Asia/Kolkata"))
    date = d.strftime("%d-%m-%Y")
    out = Path(f"Pragativadi_{d:%Y%m%d}.pdf")
    files, seen = [], set()
    session = requests.Session()
    print("=" * 60)
    print(f"📰 PRAGATIVADI — TWIN CITY — {date}")
    print("=" * 60)
    try:
        category = session.get(f"{BASE}/category/7/bhubaneswar", headers=HEADERS, timeout=40)
        category.raise_for_status()
        edition = None
        for a in BeautifulSoup(category.content, "html.parser").find_all("a", href=True):
            href = a["href"]
            if "twin-city" in href.lower() and date in href and "/edition/" in href:
                edition = urljoin(category.url, href)
                break
        if not edition:
            for raw in re.findall(r'href=["\']([^"\']*/edition/\d+/[^"\']*twin-city[^"\']*)["\']',
                                  category.text, re.I):
                url = urljoin(category.url, raw)
                if date in url:
                    edition = url
                    break
        if not edition:
            raise RuntimeError(f"Pragativadi: today's TWIN CITY edition not found for {date}")
        print(f"✓ Edition: {edition}")

        er = session.get(edition, headers=HEADERS, timeout=40)
        er.raise_for_status()
        soup = BeautifulSoup(er.content, "html.parser")
        page_numbers = set()
        for text in soup.stripped_strings:
            m = re.fullmatch(r"Page No\s+(\d{1,3})", text)
            if m: page_numbers.add(int(m.group(1)))
        if not page_numbers:
            for text in soup.stripped_strings:
                for m in re.finditer(r"TWIN CITY.*?-(\d{1,3})$", text, re.I):
                    page_numbers.add(int(m.group(1)))
        if not page_numbers:
            raise RuntimeError("Pragativadi: no page numbers found")
        total = max(page_numbers)
        if sorted(page_numbers) != list(range(1, total+1)):
            raise RuntimeError(f"Pragativadi: incomplete page sequence {sorted(page_numbers)}")
        print(f"🔎 Found {total} pages")

        for n in range(1, total+1):
            print(f"📄 Pragativadi page {n}/{total} — opening viewer", flush=True)
            page_url = f"{edition.rstrip('/')}/page/{n}"
            result = _image_from_page(session, page_url, n, seen)
            if not result:
                raise RuntimeError(f"Pragativadi: no usable newspaper image found for page {n}")
            score, data, image_url, width, height, reason, digest = result
            if digest in seen:
                raise RuntimeError(f"Pragativadi: duplicate page image detected on page {n} ({image_url})")
            seen.add(digest)
            fn = Path(f"pragativadi_page_{n:02d}.jpg")
            fn.write_bytes(data); files.append(str(fn))
            print(f"✓ Page {n:02d} — {len(data)/1048576:.2f} MB — {width}x{height}")

        with out.open("wb") as f: f.write(img2pdf.convert(files))
        print(f"✅ Pragativadi PDF ready: {len(files)} pages / {out.stat().st_size/1048576:.2f} MB")
        return str(out)
    finally:
        for fn in files:
            try: os.remove(fn)
            except OSError: pass

if __name__ == "__main__":
    download_pragativadi()
