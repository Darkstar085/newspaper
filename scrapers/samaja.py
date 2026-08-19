import hashlib
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import img2pdf
import requests

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
        timeout=(10, 90),
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

BASE = "https://m.samajaepaper.in"
EDCODE = 73
SUBCODE = 73
HEADERS = {"User-Agent": "Mozilla/5.0 Chrome/120 Safari/537.36"}

def _page_url_variants(ds, n):
    # pgnum is the current viewer parameter; keep a few legacy aliases because
    # Samaja has changed the viewer implementation more than once.
    keys = ("pgnum", "pageno", "page", "page_no", "pgno")
    urls = []
    for key in keys:
        urls.append(
            f"{BASE}/indexnext.php?pagedate={ds}&edcode={EDCODE}"
            f"&subcode={SUBCODE}&mod=1&{key}={n}&type=a"
        )
    return urls

def _find_page_image(session, html, page_url, page_no, seen):
    candidates = _extract_image_candidates(html, page_url, page_no)
    ranked = []
    for score, u, reason in candidates[:80]:
        try:
            result = _download_candidate(session, u, page_url)
        except requests.RequestException:
            continue
        if not result:
            continue
        data, width, height, fmt, final_url = result
        digest = hashlib.sha256(data).hexdigest()
        duplicate_penalty = 1000 if digest in seen else 0
        ranked.append((score - duplicate_penalty, data, final_url, width, height, reason, digest))
    if not ranked:
        return None
    ranked.sort(key=lambda x: x[0], reverse=True)
    return ranked[0]

def download_samaja():
    d = datetime.now(ZoneInfo("Asia/Kolkata"))
    ds = d.strftime("%Y-%m-%d")
    out = Path(f"Samaja_{d:%Y%m%d}.pdf")
    files, seen = [], set()
    session = requests.Session()
    print("=" * 60)
    print(f"📰 SAMAJA — BHUBANESWAR — {ds}")
    print("=" * 60)
    try:
        first_url = _page_url_variants(ds, 1)[0]
        first = session.get(first_url, headers=HEADERS, timeout=40)
        if not first.ok:
            raise RuntimeError(f"Samaja: viewer HTTP {first.status_code}")

        nums = {
            int(x) for x in re.findall(r"Page\s*(?:No\.?)?\s*(\d+)", first.text, re.I)
            if 1 <= int(x) <= 100
        }
        nums.update(
            int(x) for x in re.findall(r"(?:pgnum|pageno|page|page_no)=(\d+)", first.text, re.I)
            if 1 <= int(x) <= 100
        )
        counts = re.findall(
            r'(?:totalPages|pageCount|total_page|totalPagesCount)\s*[:=]\s*["\']?(\d+)',
            first.text, re.I
        )
        if counts:
            nums.update(range(1, max(map(int, counts)) + 1))
        if not nums:
            raise RuntimeError("Samaja: no page numbers found")

        total = max(nums)
        print(f"🔎 Found {total} pages")

        for n in range(1, total + 1):
            selected = None
            errors = []
            for url in _page_url_variants(ds, n):
                try:
                    page = session.get(url, headers=HEADERS, timeout=40)
                except requests.RequestException as exc:
                    errors.append(str(exc))
                    continue
                if not page.ok:
                    errors.append(f"{page.status_code} {url}")
                    continue
                result = _find_page_image(session, page.text, page.url, n, seen)
                if result:
                    # Prefer a page-specific image. If the first endpoint returns
                    # page 1 for every request, a duplicate will score lower and
                    # the next parameter variant gets a chance.
                    selected = result
                    if result[-1] not in seen:
                        break
            if not selected:
                raise RuntimeError(f"Samaja: no valid image for page {n}")
            score, data, image_url, width, height, reason, digest = selected
            if digest in seen:
                raise RuntimeError(
                    f"Samaja: page {n} still resolves to a duplicate image; "
                    f"last image={image_url}"
                )
            seen.add(digest)
            fn = Path(f"samaja_page_{n:02d}.jpg")
            fn.write_bytes(data)
            files.append(str(fn))
            print(f"✓ Page {n:02d} — {len(data)/1048576:.2f} MB — {width}x{height}")

        with out.open("wb") as f:
            f.write(img2pdf.convert(files))
        print(f"✅ Samaja PDF ready: {len(files)} pages / {out.stat().st_size/1048576:.2f} MB")
        return str(out)
    finally:
        for f in files:
            try: os.remove(f)
            except OSError: pass
