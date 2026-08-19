import hashlib
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse
from zoneinfo import ZoneInfo
import img2pdf
import requests

import io
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

BASE = "https://www.prameyaepaper.com"
HEADERS = {"User-Agent": "Mozilla/5.0 Chrome/120 Safari/537.36"}

def _edition_links(html, page_url):
    out = []
    for raw in re.findall(r'href=["\']([^"\']*/edition/\d+/bhubaneswar[^"\']*)["\']', html, re.I):
        u = urljoin(page_url, raw.replace("\\/", "/"))
        if u not in out: out.append(u)
    return out

def _extract_page_routes(html, edition):
    found = {}
    soup = BeautifulSoup(html, "html.parser")

    def add(n, raw):
        try: n = int(n)
        except Exception: return
        if not 1 <= n <= 100 or not raw: return
        raw = raw.strip()
        if raw.lower().startswith("javascript:"):
            urls = re.findall(r'(?:https?:)?//[^"\'\s)]+|/[^"\'\s)]+', raw)
            for u in urls:
                candidate = urljoin(edition, u)
                if candidate.rstrip("/") != edition.rstrip("/"):
                    found.setdefault(n, candidate)
                    return
            return
        candidate = urljoin(edition, raw)
        if candidate.rstrip("/") != edition.rstrip("/"):
            found.setdefault(n, candidate)

    for tag in soup.find_all("a"):
        text = tag.get_text(" ", strip=True)
        nums = re.findall(r"\bPage\s*(?:No\.?\s*)?(\d{1,3})\b", text, re.I)
        for n in nums:
            for key in ("href", "data-url", "data-href", "data-page-url", "onclick"):
                if tag.get(key):
                    add(n, tag.get(key))

    for m in re.finditer(
        r'(?:page|pageno|page_no|pageNo)\s*["\']?\s*[:=]\s*["\']?(\d{1,3})[^{}\n]{0,250}?(?:url|href|link)\s*["\']?\s*[:=]\s*["\']([^"\']+)',
        html, re.I
    ):
        add(m.group(1), m.group(2))

    return sorted(found.items())

def _fallback_page_urls(edition, n):
    variants = [
        f"{edition.rstrip('/')}/page/{n}",
        f"{edition.rstrip('/')}/{n}",
        f"{edition.rstrip('/')}?page={n}",
        f"{edition.rstrip('/')}?pageno={n}",
        f"{edition.rstrip('/')}?page_no={n}",
        f"{edition.rstrip('/')}?pgnum={n}",
        f"{edition.rstrip('/')}?pageNo={n}",
    ]
    return variants

PRAMEYA_CDN_RE = re.compile(r"/FilesUpload/\d{4}/\d{1,2}/\d{1,2}/(\d+)_(\d+)_([a-z0-9]+)\.(?:webp|jpe?g|png)(?:[?#].*)?$", re.I)

def _page_image(session, html, page_url, page_no, seen, edition_id):
    candidates = _extract_image_candidates(html, page_url, page_no)

    exact = []
    for score, u, reason in candidates:
        m = PRAMEYA_CDN_RE.search(urlparse(u).path)
        if m and m.group(1) == str(edition_id) and int(m.group(2)) == page_no:
            exact.append((score + 10000, u, reason + " exact-cdn-page"))

    if exact:
        candidates = exact
        print(f"   🎯 exact Prameya page-image candidates: {len(candidates)}", flush=True)
    else:
        candidates = candidates[:20]
        print(f"   ⚠ No exact CDN page match found; fallback candidates: {len(candidates)}", flush=True)

    ranked = []
    limit = len(candidates)
    for idx, (score, u, reason) in enumerate(candidates, 1):
        print(f"   🔎 image candidate {idx}/{limit} — {u[:140]}", flush=True)
        try:
            result = _download_candidate(session, u, page_url)
        except requests.RequestException:
            continue
        if not result:
            continue
        data, width, height, fmt, final_url = result
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen:
            score -= 10000
        ranked.append((score, data, final_url, width, height, reason, digest))
        if exact and digest not in seen:
            return ranked[-1]
    return max(ranked, key=lambda x: x[0]) if ranked else None

def _fetch_page_image(session, edition, n, routes, seen, edition_id):
    urls = []
    if n in routes:
        urls.append(routes[n])
    urls.extend(_fallback_page_urls(edition, n))

    attempted = set()
    for url in urls:
        if url in attempted:
            continue
        attempted.add(url)
        try:
            r = session.get(url, headers=HEADERS, timeout=(5, 25), allow_redirects=True)
        except requests.RequestException:
            continue
        if not r.ok:
            continue
        result = _page_image(session, r.text, r.url, n, seen, edition_id)
        if result:
            return result
    return None

def download_prameya():
    d = datetime.now(ZoneInfo("Asia/Kolkata"))
    out = Path(f"Prameya_{d:%Y%m%d}.pdf")
    files, seen = [], set()
    session = requests.Session()
    targets = [d.strftime("%d %b, %Y").lower(), d.strftime("%d %B, %Y").lower()]
    print("=" * 60)
    print(f"📰 PRAMEYA — BHUBANESWAR — {d:%Y-%m-%d}")
    print("=" * 60)
    try:
        candidates = []
        for source in (BASE + "/", BASE + "/edition", BASE + "/editions"):
            try:
                r = session.get(source, headers=HEADERS, timeout=40)
                if r.ok: candidates.extend(_edition_links(r.text, r.url))
            except requests.RequestException:
                pass

        edition = edition_html = None
        for u in dict.fromkeys(candidates):
            try:
                r = session.get(u, headers=HEADERS, timeout=40)
            except requests.RequestException:
                continue
            if r.ok and any(t in r.text.lower() for t in targets):
                edition, edition_html = r.url, r.text
                break
        if not edition:
            raise RuntimeError("Prameya: today's Bhubaneswar edition not found")
        print(f"✓ Edition: {edition}")
        m_edition = re.search(r"/edition/(\d+)/", edition)
        edition_id = m_edition.group(1) if m_edition else None
        if not edition_id:
            raise RuntimeError("Prameya: could not determine edition ID")
        print(f"🔑 Prameya edition ID: {edition_id}")

        soup = BeautifulSoup(edition_html, "html.parser")
        page_numbers = set()
        for text in soup.stripped_strings:
            for m in re.finditer(r"\bPage\s*(?:No\.?\s*)?(\d{1,3})\b", text, re.I):
                page_numbers.add(int(m.group(1)))
        if not page_numbers:
            raise RuntimeError("Prameya: no page numbers found")
        total = max(page_numbers)
        print(f"🔎 Found {total} pages")

        routes = dict(_extract_page_routes(edition_html, edition))
        print(f"🔗 Resolved explicit page routes: {len(routes)}/{total}")

        for n in range(1, total + 1):
            print(f"📄 Prameya page {n}/{total} — resolving viewer", flush=True)
            result = _fetch_page_image(session, edition, n, routes, seen, edition_id)
            if not result:
                raise RuntimeError(f"Prameya: no usable page image found for page {n}")
            score, data, image_url, width, height, reason, digest = result
            if digest in seen:
                raise RuntimeError(
                    f"Prameya: page {n} resolves to a duplicate image ({image_url})"
                )
            seen.add(digest)
            fn = Path(f"prameya_page_{n:02d}.jpg")
            with Image.open(io.BytesIO(data)) as im:
                im = im.convert("RGB")
                im.save(
                    fn,
                    "JPEG",
                    quality=75,
                    optimize=True,
                    progressive=True,
                )
            files.append(str(fn))
            print(
                f"✓ Page {n:02d} — source {len(data)/1048576:.2f} MB — "
                f"{width}x{height} — JPEG {fn.stat().st_size/1048576:.2f} MB"
            )

        with out.open("wb") as f:
            f.write(img2pdf.convert(files))
        pdf_mb = out.stat().st_size / 1048576
        print(f"✅ Prameya PDF ready: {len(files)} pages / {pdf_mb:.2f} MB")
        if pdf_mb >= 50:
            print("⚠ Prameya PDF is >= 50 MB; Telegram Bot API upload may fail.")
        return str(out)
    finally:
        for f in files:
            try: os.remove(f)
            except OSError: pass

if __name__ == "__main__":
    download_prameya()
