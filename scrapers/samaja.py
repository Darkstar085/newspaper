import os
import requests
import img2pdf
from datetime import datetime

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def download_samaja() -> str | None:
    """Scrapes today's Samaja e-paper and returns the output PDF file path."""
    today_str = datetime.now().strftime("%Y%m%d")
    output_pdf = f"Samaja_{today_str}.pdf"
    
    base_url = "https://samajaepaper.in"
    downloaded_images = []

    try:
        page_num = 1
        while True:
            img_url = f"{base_url}/epaperimages/{today_str}/{today_str}-page-{page_num}.jpg"
            response = requests.get(img_url, headers=HEADERS, timeout=10)
            
            if response.status_code != 200:
                break  # Stop when no further pages exist
                
            file_path = f"samaja_page_{page_num}.jpg"
            with open(file_path, "wb") as f:
                f.write(response.content)
            downloaded_images.append(file_path)
            page_num += 1

        if not downloaded_images:
            print("Samaja: No pages downloaded.")
            return None

        with open(output_pdf, "wb") as f:
            f.write(img2pdf.convert(downloaded_images))

        return output_pdf

    except Exception as e:
        print(f"Error downloading Samaja: {e}")
        return None

    finally:
        for img_path in downloaded_images:
            if os.path.exists(img_path):
                os.remove(img_path)
