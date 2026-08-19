import os
import requests
from datetime import datetime

from scrapers.samaja import download_samaja
from scrapers.sambad import download_sambad
from scrapers.dharitri import download_dharitri
from scrapers.pragativadi import download_pragativadi
from scrapers.prameya import download_prameya

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_pdf_to_telegram(pdf_path: str, caption: str):
    """Sends a PDF file to the specified Telegram Chat or Channel."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    
    if not os.path.exists(pdf_path):
        print(f"File not found: {pdf_path}")
        return
        
    file_size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
    if file_size_mb > 49.0:
        print(f"Skipping {pdf_path}: File size ({file_size_mb:.2f} MB) exceeds Telegram 50MB Bot API limit.")
        return

    with open(pdf_path, "rb") as file:
        payload = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption}
        files = {"document": (os.path.basename(pdf_path), file, "application/pdf")}
        response = requests.post(url, data=payload, files=files, timeout=120)
        
        if response.status_code == 200:
            print(f"Successfully sent {pdf_path} to Telegram.")
        else:
            print(f"Failed to send {pdf_path}: {response.text}")

def main():
    today = datetime.now().strftime("%Y-%m-%d")
    
    scrapers = [
        ("Samaja", download_samaja),
        ("Sambad", download_sambad),
        ("Dharitri", download_dharitri),
        ("Pragativadi", download_pragativadi),
        ("Prameya", download_prameya),
    ]

    for name, scraper_fn in scrapers:
        print(f"Starting download for {name}...")
        pdf_file = scraper_fn()
        
        if pdf_file and os.path.exists(pdf_file):
            caption = f"📄 {name} E-Paper ({today})"
            send_pdf_to_telegram(pdf_file, caption)
            os.remove(pdf_file)
        else:
            print(f"Failed to generate PDF for {name}.")

if __name__ == "__main__":
    main()
