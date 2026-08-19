import hashlib
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
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

# Compression is a separate pass after all pages are downloaded.
PDF_JPEG_QUALITY = 62

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

def _fetch_page_response(session, page_url, page_no):
    # Pragativadi occasionally returns a transient 5xx for an otherwise valid
    # page route. Retry with backoff and a cache-busting query before giving up.
    # This is intentionally page-generic; page 10 is not special.
    retry_delays = (0.0, 1.5, 3.0, 6.0, 10.0)
    last_error = None
    variants = (
        page_url,
        f"{page_url}/",
        f"{page_url}?_epaper_retry=1",
        f"{page_url}?_epaper_retry=2",
    )
    attempt = 0
    for delay in retry_delays:
        if delay:
            time.sleep(delay)
        url = variants[min(attempt, len(variants) - 1)]
        attempt += 1
        try:
            response = session.get(
                url,
                headers={**HEADERS, "Cache-Control": "no-cache", "Pragma": "no-cache"},
                timeout=(5, 30),
                allow_redirects=True,
            )
            if response.ok:
                return response
            last_error = requests.HTTPError(
                f"HTTP {response.status_code} for {url}", response=response
            )
            if response.status_code not in {429, 500, 502, 503, 504}:
                break
            print(
                f"   ⚠ Page {page_no}: viewer returned HTTP {response.status_code}; "
                f"retrying ({attempt}/{len(retry_delays)})",
                flush=True,
            )
        except requests.RequestException as exc:
            last_error = exc
            print(
                f"   ⚠ Page {page_no}: viewer request failed ({exc}); "
                f"retrying ({attempt}/{len(retry_delays)})",
                flush=True,
            )

    if last_error:
        raise last_error
    raise requests.RequestException(f"Pragativadi: unable to fetch page {page_no}")


def _page_variants(edition, page_no):
    """Only real viewer endpoints; /page/<n> is HTML, never an image."""
    base = edition.rstrip("/")
    return [f"{base}/page/{page_no}", f"{base}/page/{page_no}/"]


def _extract_image_candidates(html, page_url, page_no):
    """
    Extract actual raster/download URLs exposed by the Pragativadi viewer.
    Navigation URLs such as /page/1 are deliberately rejected.
    """
    soup = BeautifulSoup(html, "html.parser")
    candidates = []

    def add(raw, score=0, reason=""):
        if not raw:
            return
        raw = str(raw).strip().replace("\\/", "/").replace("&amp;", "&")
        if raw.startswith(("data:", "javascript:", "mailto:", "tel:")):
            return

        raw = raw.replace("\\u0026", "&").replace("\\x26", "&")
        u = _normalize_url(raw, page_url)
        if not u:
            return

        low = u.lower()
        path = low.split("?", 1)[0]

        # Critical: never consider viewer/navigation pages to be images.
        if re.search(r"/page(?:/\d+)?/?$", path):
            return

        is_raster = bool(re.search(r"\.(?:jpe?g|png|webp)(?:[?#]|$)", low))
        is_image_endpoint = (
            "imagedownload.php" in low or
            "imageprocessor" in low
        )
        if not (is_raster or is_image_endpoint):
            return

        s = score
        if is_raster:
            s += 300
        if "uploads/epaper/" in low:
            s += 700
        if "imagedownload.php" in low:
            s += 600
        if "imageprocessor" in low:
            s += 100
        if re.search(
            rf"(?:page|pageno|pg|pagenumber)[_-]?0*{page_no}(?:\D|$)",
            low, re.I
        ):
            s += 250
        if re.search(
            rf"(?:-|_)0*{page_no}\.(?:jpg|jpeg|png|webp)$",
            low, re.I
        ):
            s += 250
        s -= sum(200 for word in _BAD_WORDS if word in low)

        candidates.append((s, u, reason))

    # HTML image elements / lazy-loading attributes.
    for tag in soup.find_all(["img", "source"]):
        attrs = tag.attrs
        for key in (
            "src", "data-src", "data-original", "data-image",
            "data-img", "data-url", "data-lazy-src",
            "data-filename", "data-image-url"
        ):
            add(attrs.get(key), 200, key)

        if attrs.get("srcset"):
            for item in str(attrs["srcset"]).split(","):
                add(item.strip().split()[0], 180, "srcset")

    # Social/OG image metadata.
    for tag in soup.find_all("meta"):
        prop = (tag.get("property") or tag.get("name") or "").lower()
        if prop in {"og:image", "twitter:image"}:
            add(tag.get("content"), 120, prop)

    # Inline JavaScript is often where the real page-image URL lives.
    scripts = "\n".join(
        s.string or s.get_text() or "" for s in soup.find_all("script")
    )

    for match in re.finditer(
        r'(?:(?:https?:)?//|/)[^"\'\\\s<>]+?\.(?:jpe?g|png|webp)'
        r'(?:\?[^"\'\\\s<>]*)?',
        scripts, re.I
    ):
        add(match.group(0), 400, "script-raster")

    # Quoted endpoint/asset strings.
    for match in re.finditer(r'["\']([^"\']{3,1500})["\']', scripts):
        raw = match.group(1)
        low = raw.lower()
        if any(k in low for k in (
            "imagedownload", "imageprocessor",
            "uploads/epaper", "uploads/",
            ".jpg", ".jpeg", ".png", ".webp"
        )):
            add(raw, 350, "script-image")

    # data-* / onclick attributes can contain the same values.
    for tag in soup.find_all(True):
        for key, value in tag.attrs.items():
            if not (key.startswith("data-") or key.lower() in {"onclick", "href"}):
                continue
            if isinstance(value, list):
                value = " ".join(value)
            if not isinstance(value, str):
                continue
            for raw in re.findall(
                r'(?:https?:)?//[^"\'\s)]+|/[^"\'\s)]+',
                value
            ):
                add(raw, 180, key)

    # Deduplicate by URL, retaining strongest score.
    best = {}
    for score, url, reason in candidates:
        if url not in best or score > best[url][0]:
            best[url] = (score, url, reason)

    return sorted(best.values(), key=lambda x: x[0], reverse=True)


def _download_candidate(session, url, referer):
    """Fetch and strictly validate a real newspaper raster."""
    r = session.get(
        url,
        headers={
            "User-Agent": HEADERS["User-Agent"],
            "Referer": referer,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
        timeout=(10, 35),
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


def _image_from_page(session, edition, page_no, seen):
    """
    Resolve a newspaper page from the viewer HTML.

    The old implementation made the fundamental mistake of treating
    /page/<n> navigation URLs as image candidates. This resolver only
    accepts actual JPG/PNG/WebP assets or image download endpoints.
    """
    for page_url in _page_variants(edition, page_no):
        for attempt in range(3):
            try:
                if attempt:
                    import time
                    time.sleep(1.5 * (2 ** (attempt - 1)))

                url = page_url
                if attempt:
                    url += ("&" if "?" in url else "?") + f"_cb={int(datetime.now().timestamp())}{attempt}"

                response = session.get(
                    url,
                    headers={
                        **HEADERS,
                        "Accept": "text/html,application/xhtml+xml",
                        "Cache-Control": "no-cache",
                        "Pragma": "no-cache",
                    },
                    timeout=(10, 45),
                )
                response.raise_for_status()

                candidates = _extract_image_candidates(
                    response.text, response.url, page_no
                )

                print(
                    f"   🔎 resolved {len(candidates)} actual image candidates",
                    flush=True,
                )

                for idx, (score, image_url, reason) in enumerate(candidates, 1):
                    print(
                        f"   🔎 image candidate {idx}/{len(candidates)} — "
                        f"{image_url[:180]}",
                        flush=True,
                    )
                    try:
                        result = _download_candidate(
                            session, image_url, response.url
                        )
                    except requests.RequestException:
                        continue

                    if not result:
                        continue

                    data, width, height, fmt, final_url = result
                    digest = hashlib.sha256(data).hexdigest()

                    if digest in seen:
                        continue

                    return (
                        score, data, final_url, width, height,
                        reason, digest
                    )

            except requests.RequestException as exc:
                print(
                    f"   ⚠️ viewer attempt {attempt + 1}/3 failed: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )

    return None

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
            print(f"📄 Pragativadi page {n}/{total} — resolving viewer", flush=True)
            result = _image_from_page(session, edition, n, seen)
            if not result:
                raise RuntimeError(f"Pragativadi: no usable newspaper image found for page {n}")
            score, data, image_url, width, height, reason, digest = result
            if digest in seen:
                raise RuntimeError(f"Pragativadi: duplicate page image detected on page {n} ({image_url})")
            seen.add(digest)
            fn = Path(f"pragativadi_page_{n:02d}.jpg")
            fn.write_bytes(data); files.append(str(fn))
            print(f"✓ Page {n:02d} — {len(data)/1048576:.2f} MB — {width}x{height}")

        # Separate compression pass: all original pages have already been
        # downloaded and validated. Nothing is removed from the download stage.
        compressed_files = []
        try:
            for fn_str in files:
                src_page = Path(fn_str)
                compressed = src_page.with_name(src_page.stem + "_compressed.jpg")

                with Image.open(src_page) as im:
                    if im.mode != "RGB":
                        im = im.convert("RGB")
                    im.save(
                        compressed,
                        format="JPEG",
                        quality=PDF_JPEG_QUALITY,
                        optimize=True,
                        progressive=True,
                        subsampling="4:2:0",
                    )

                compressed_files.append(str(compressed))

            with out.open("wb") as f:
                f.write(img2pdf.convert(compressed_files))
        finally:
            for fn_str in compressed_files:
                try:
                    os.remove(fn_str)
                except OSError:
                    pass

        print(f"✅ Pragativadi PDF ready: {len(files)} pages / {out.stat().st_size/1048576:.2f} MB")
        return str(out)
    finally:
        for fn in files:
            try: os.remove(fn)
            except OSError: pass

if __name__ == "__main__":
    download_pragativadi()
