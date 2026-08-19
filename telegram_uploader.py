import asyncio
import html
import os
from pathlib import Path

import requests
from telethon import TelegramClient


BOT_API_LIMIT_MB = 50.0


def build_caption(name, date):
    return (
        f"📰 <b>{html.escape(name)} E-Paper</b>\n"
        f"📅 <b>{date}</b>\n\n"
        "📢 <b>Daily Odisha Newspapers</b>\n"
        '<a href="https://t.me/odisha_newspaper">🔗 @odisha_newspaper</a>'
    )


def _send_bot_api(pdf_path, caption):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("❌ Telegram Bot API credentials are missing.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendDocument"

    try:
        with open(pdf_path, "rb") as file:
            response = requests.post(
                url,
                data={
                    "chat_id": chat_id,
                    "caption": caption,
                    "parse_mode": "HTML",
                },
                files={
                    "document": (
                        Path(pdf_path).name,
                        file,
                        "application/pdf",
                    )
                },
                timeout=300,
            )

        if response.ok:
            print("✅ Telegram Bot API upload succeeded.")
            return True

        print(f"❌ Telegram Bot API upload failed: {response.text}")
        return False
    except requests.RequestException as exc:
        print(f"❌ Telegram Bot API request failed: {exc}")
        return False


async def _send_telethon(pdf_path, caption):
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not all((api_id, api_hash, bot_token, chat_id)):
        raise RuntimeError(
            "Telethon fallback requires TELEGRAM_API_ID, "
            "TELEGRAM_API_HASH, TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."
        )

    client = TelegramClient(
        "epaper_bot",
        int(api_id),
        api_hash,
    )

    await client.start(bot_token=bot_token)
    try:
        await client.send_file(
            chat_id,
            pdf_path,
            caption=caption,
            parse_mode="html",
            force_document=True,
        )
    finally:
        await client.disconnect()


def send_pdf_to_telegram(pdf_path, caption):
    size_mb = os.path.getsize(pdf_path) / (1024 * 1024)

    if size_mb < BOT_API_LIMIT_MB:
        return _send_bot_api(pdf_path, caption)

    print(
        f"📦 PDF is {size_mb:.2f} MB; switching to Telethon MTProto upload."
    )
    try:
        asyncio.run(_send_telethon(pdf_path, caption))
        print("✅ Telethon upload succeeded.")
        return True
    except Exception as exc:
        print(f"❌ Telethon upload failed: {type(exc).__name__}: {exc}")
        return False
