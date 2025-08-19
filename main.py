import os
import re
import asyncio
import logging
import hashlib
import mimetypes
from aiohttp import ClientSession
from aiofiles import open as aio_open
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("downloader_bot")

TMP_DIR = "/tmp"
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "524288"))

if not BOT_TOKEN or not WEBHOOK_URL:
    raise RuntimeError("BOT_TOKEN and WEBHOOK_URL env vars are required!")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

GDRIVE_REGEX = re.compile(
    r"https://drive\.google\.com/file/d/([a-zA-Z0-9_-]+)/(?:[^?]*)(?:\?.*)?"
)
GDRIVE_DIRECT = "https://drive.google.com/uc?export=download&id={}"

app = FastAPI(title="Downloader Bot", version="1.0.0")

@app.get("/", response_class=PlainTextResponse)
@app.get("/kaithhealthcheck", response_class=PlainTextResponse)
async def health():
    return "OK"

class DownloadError(Exception):
    pass

async def download_file(url: str, dest_path: str, progress_cb=None) -> None:
    async with ClientSession() as session:
        async with session.get(url, timeout=None, allow_redirects=True) as resp:
            if resp.status != 200:
                raise DownloadError(f"HTTP {resp.status}")
            total = resp.content_length or 0
            downloaded = 0
            last_update = 0.0
            last_percent = 0
            async with aio_open(dest_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                    await f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb and total:
                        now = asyncio.get_event_loop().time()
                        percent = int(downloaded * 100 / total)
                        if now - last_update >= 1.0 or percent != last_percent:
                            await progress_cb(percent)
                            last_update = now
                            last_percent = percent

@dp.message(F.command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 <b>Welcome to the Downloader Bot!</b>\n\n"
        "Send me a Google Drive or direct download link and I'll forward it to you as a file."
    )

@dp.message(F.text.startswith("http"))
async def handle_link(message: Message):
    url = message.text.strip()
    match = GDRIVE_REGEX.match(url)
    if match:
        file_id = match.group(1)
        url = GDRIVE_DIRECT.format(file_id)
    
    if not url or not url.startswith("http"):
        await message.reply("❌ Invalid link format. Please send a valid HTTP/HTTPS link.")
        return
    
    safe_name = hashlib.md5(url.encode()).hexdigest()[:12]
    ext = mimetypes.guess_extension(
        mimetypes.guess_type(url)[0] or "application/octet-stream"
    ) or ".bin"
    file_path = os.path.join(TMP_DIR, f"{safe_name}{ext}")
    
    progress_msg = await message.reply("📥 Starting download…")
    
    async def report_progress(percent: int):
        try:
            await progress_msg.edit_text(f"📥 Downloading… {percent}%")
        except TelegramRetryAfter:
            pass
    
    try:
        await download_file(url, file_path, progress_cb=report_progress)
    except DownloadError as e:
        await progress_msg.edit_text(f"❌ Download failed: {e}")
        return
    except Exception as e:
        await progress_msg.edit_text(f"❌ Unexpected error: {str(e)}")
        return
    
    await progress_msg.edit_text("📤 Uploading…")
    try:
        await bot.send_chat_action(message.chat.id, "upload_document")
        await bot.send_document(
            chat_id=message.chat.id,
            document=file_path,
            caption=f"✅ <b>Downloaded via:</b> <a href='{url}'>link</a>",
            reply_to_message_id=message.message_id,
        )
    except TelegramBadRequest as e:
        error_msg = str(e)
        if "invalid file HTTP URL" in error_msg:
            await progress_msg.edit_text("❌ Invalid file URL. The link may not point to a valid file.")
        else:
            await progress_msg.edit_text(f"❌ Telegram error: {error_msg}")
    except Exception as e:
        await progress_msg.edit_text(f"❌ Upload failed: {str(e)}")
    finally:
        try:
            os.remove(file_path)
        except OSError:
            pass

@dp.message()
async def fallback(message: Message):
    await message.reply(
        "Send me a Google Drive or direct download link and I'll forward it to you as a file.\n\n"
        "Or use /start to see the welcome message."
    )

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
FULL_WEBHOOK_URL = f"{WEBHOOK_URL}{WEBHOOK_PATH}"

async def on_startup():
    log.info("Starting bot...")
    current = await bot.get_webhook_info()
    if current.url != FULL_WEBHOOK_URL:
        try:
            await bot.set_webhook(FULL_WEBHOOK_URL)
            log.info("Webhook set to %s", FULL_WEBHOOK_URL)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            await bot.set_webhook(FULL_WEBHOOK_URL)
            log.info("Webhook set after retry to %s", FULL_WEBHOOK_URL)

async def on_shutdown():
    log.info("Shutting down bot...")
    await bot.delete_webhook()

app.add_event_handler("startup", on_startup)
app.add_event_handler("shutdown", on_shutdown)

@app.post(WEBHOOK_PATH, include_in_schema=False)
async def telegram_webhook(request: Request):
    try:
        await dp.feed_webhook_update(bot, await request.json(), secret_token=None)
        return {"ok": True}
    except Exception as e:
        log.error("Error processing webhook: %s", e)
        return {"ok": False, "error": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
    )
