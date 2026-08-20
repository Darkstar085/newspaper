import os
from pathlib import Path
import pymupdf


MAX_BOT_API_MB = 49.0


def pdf_size_mb(path):
    return Path(path).stat().st_size / (1024 * 1024)


def compress_pdf_for_upload(pdf_path, max_mb=MAX_BOT_API_MB):
    path = Path(pdf_path)
    original_size = pdf_size_mb(path)

    if original_size <= max_mb:
        return str(path), False

    attempts = (
        (200, 68),
        (170, 60),
        (150, 52),
        (130, 45),
    )

    best_path = path
    best_size = original_size

    for dpi, quality in attempts:
        temp = path.with_name(f".{path.stem}_compressed_{dpi}.pdf")
        source = pymupdf.open(path)
        try:
            page_count = source.page_count
            source.rewrite_images(
                dpi_threshold=dpi + 40,
                dpi_target=dpi,
                quality=quality,
                lossy=True,
                lossless=True,
                bitonal=True,
                color=True,
                gray=True,
            )
            source.save(
                temp,
                garbage=4,
                clean=True,
                deflate=True,
                deflate_images=True,
                deflate_fonts=True,
                use_objstms=True,
            )
        finally:
            source.close()

        try:
            candidate_size = pdf_size_mb(temp)
            if candidate_size < best_size:
                check = pymupdf.open(temp)
                valid_pages = check.page_count == page_count
                check.close()
                if valid_pages:
                    if best_path != path:
                        try:
                            os.remove(best_path)
                        except OSError:
                            pass
                    best_path = temp
                    best_size = candidate_size
                else:
                    os.remove(temp)
            else:
                os.remove(temp)
        except Exception:
            try:
                os.remove(temp)
            except OSError:
                pass

        if best_size <= max_mb:
            break

    if best_path != path:
        os.replace(best_path, path)
        print(
            f"🗜 PDF compressed: {original_size:.2f} MB → "
            f"{pdf_size_mb(path):.2f} MB"
        )
        return str(path), True

    print(
        f"⚠ PDF remains {original_size:.2f} MB after compression attempts; "
        "Telethon fallback will be used."
    )
    return str(path), False
