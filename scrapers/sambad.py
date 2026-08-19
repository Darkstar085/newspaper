import os
import requests
import img2pdf
from datetime import datetime

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def download_sambad() -> str | None:
    """Scrapes today's Sambad e-paper and returns the output PDF file path."""
    today_str = datetime.now().strftime("%d%m%Y")
    output_pdf = f"Sambad_{today_str}.pdf"
    downloaded_images = []

    try:
        for page in range(1, 20):  # Scans max 20 pages
            img_url = f"https://sambadepaper.com/epaperimages/{today_str}/{today_str}-md-hr-{page}.jpg"
            res = requests.get(img_url, headers=HEADERS, timeout=10)
            if res.status_code == 200:
                file_path = f"sambad_{page}.jpg"
                with open(file_path, "wb") as f:
                    f.write(res.content)
                downloaded_images.append(file_path)
            else:
                if page > 1:
                    break

        if downloaded_images:
            with open(output_pdf, "wb") as f:
                f.write(img2pdf.convert(downloaded_images))
            return output_pdf
        return None
    except Exception as e:
        print(f"Error downloading Sambad: {e}")
        return None
    finally:
        for img in downloaded_images:
            if os.path.exists(img):
                os.remove(img)
