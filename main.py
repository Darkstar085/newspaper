import hashlib
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from pdf_utils import compress_pdf_for_upload
from scrapers.dharitri import download_dharitri
from scrapers.pragativadi import download_pragativadi
from scrapers.prameya import download_prameya
from scrapers.samaja import download_samaja
from scrapers.sambad import download_sambad
from state import (
    already_sent,
    edition_key,
    hash_owner,
    load_state,
    record_sent,
    save_state,
)
from telegram_uploader import build_caption, send_pdf_to_telegram


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
    state = load_state()

    scrapers = [
        ("Samaja", download_samaja),
        ("Sambad", download_sambad),
        ("Dharitri", download_dharitri),
        ("Pragativadi", download_pragativadi),
        ("Prameya", download_prameya),
    ]

    print("=" * 64)
    print(f"📰 ODISHA E-PAPER PIPELINE — {today}")
    print("=" * 64)

    for name, scraper_fn in scrapers:
        key = edition_key(name, today)

        print()
        print("=" * 64)
        print(f"📰 {name.upper()} — {today}")
        print("=" * 64)

        if already_sent(state, name, today):
            record = state["sent"][key]
            print("⏭ Already sent — skipping download.")
            print(f"   Key: {key}")
            print(f"   SHA256: {record.get('sha256', 'unknown')}")
            continue

        print(f"📥 Starting download for {name}...")
        pdf_file = None

        try:
            pdf_file = scraper_fn()

            if not pdf_file or not os.path.exists(pdf_file):
                print(f"❌ Failed to generate PDF for {name}.")
                continue

            pdf_file, _ = compress_pdf_for_upload(pdf_file)

            pdf_hash = sha256_file(pdf_file)
            size_mb = os.path.getsize(pdf_file) / (1024 * 1024)

            print(f"🔐 SHA-256: {pdf_hash}")
            print(f"📦 File size: {size_mb:.2f} MB")

            owner = hash_owner(state, pdf_hash)
            if owner:
                print(f"⏭ Exact PDF already sent under: {owner}")
                record_sent(
                    state,
                    name,
                    today,
                    pdf_hash,
                    size_mb=size_mb,
                    uploader="duplicate-skip",
                    duplicate_of=owner,
                )
                continue

            caption = build_caption(name, today)

            print("📤 Uploading to Telegram...")
            if send_pdf_to_telegram(pdf_file, caption):
                record_sent(
                    state,
                    name,
                    today,
                    pdf_hash,
                    size_mb=size_mb,
                    uploader="telethon" if size_mb >= 50 else "bot",
                )
                print(f"💾 Delivery state saved: {key}")
            else:
                print("⚠ Upload failed; edition will be retried next run.")

        except Exception as exc:
            print(f"❌ {name} failed: {type(exc).__name__}: {exc}")
            print("   Edition is NOT marked as sent; next run can retry.")

        finally:
            if pdf_file and os.path.exists(pdf_file):
                try:
                    os.remove(pdf_file)
                    print(f"🧹 Removed temporary PDF: {pdf_file}")
                except OSError as exc:
                    print(f"⚠ Could not remove {pdf_file}: {exc}")


if __name__ == "__main__":
    main()
