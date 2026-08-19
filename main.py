import hashlib
import os
from datetime import datetime

import requests

from scrapers.samaja import download_samaja
from scrapers.sambad import download_sambad
from scrapers.dharitri import download_dharitri
from scrapers.pragativadi import download_pragativadi
from scrapers.prameya import download_prameya
from state import load_state, save_state, edition_key, already_sent, record_sent, hash_owner


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def send_pdf_to_telegram(pdf_path: str, caption: str) -> bool:
    """Send a PDF with the Telegram Bot API and return True only on success."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"

    if not os.path.exists(pdf_path):
        print(f"❌ File not found: {pdf_path}")
        return False

    file_size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
    if file_size_mb > 49.0:
        print(
            f"⚠ Skipping {pdf_path}: {file_size_mb:.2f} MB exceeds "
            "the configured Bot API limit. Telethon can be added later."
        )
        return False

    try:
        with open(pdf_path, "rb") as file:
            payload = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption}
            files = {
                "document": (
                    os.path.basename(pdf_path),
                    file,
                    "application/pdf",
                )
            }

            response = requests.post(
                url,
                data=payload,
                files=files,
                timeout=180,
            )

        if response.ok:
            print(f"✅ Successfully sent {pdf_path} to Telegram.")
            return True

        print(f"❌ Failed to send {pdf_path}: {response.text}")
        return False

    except requests.RequestException as exc:
        print(f"❌ Telegram request failed: {exc}")
        return False


def main():
    today = datetime.now().strftime("%Y-%m-%d")
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

        # IMPORTANT: check before calling the scraper. This means an already
        # delivered edition is not downloaded again.
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

            pdf_hash = sha256_file(pdf_file)
            size_mb = os.path.getsize(pdf_file) / (1024 * 1024)

            print(f"🔐 SHA-256: {pdf_hash}")
            print(f"📦 File size: {size_mb:.2f} MB")

            # Second protection layer: the exact same PDF was already sent,
            # even if the edition key changed.
            owner = hash_owner(state, pdf_hash)

            if owner:
                print(f"⏭ Exact PDF already sent under: {owner}")
                print("   Skipping Telegram upload.")

                state["sent"][key] = {
                    "sent_at": state["sent"][owner].get("sent_at"),
                    "sha256": pdf_hash,
                    "size_mb": round(size_mb, 2),
                    "uploader": "duplicate-skip",
                    "duplicate_of": owner,
                }
                save_state(state)
                continue

            caption = f"📄 {name} E-Paper ({today})"

            print("📤 Uploading to Telegram...")
            if send_pdf_to_telegram(pdf_file, caption):
                # Only mark it sent AFTER Telegram confirms success.
                record_sent(
                    state,
                    name,
                    today,
                    pdf_hash,
                    size_mb=size_mb,
                    uploader="bot",
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
