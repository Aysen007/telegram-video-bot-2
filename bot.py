import os
import re
import shutil
import uuid
import logging
import traceback
from pathlib import Path
from typing import Optional, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import yt_dlp

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("MediaBot")

TOKEN: str = os.environ["TOKEN"]
BASE_DIR: Path = Path("/tmp/media_bot")
BASE_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE: int = 49 * 1024 * 1024
SUPPORTED_DOMAINS: list[str] = [
    "instagram.com", "tiktok.com", "youtube.com", "youtu.be",
    "facebook.com", "fb.watch", "twitter.com", "x.com", "threads.net",
]

def find_ffmpeg() -> Optional[Path]:
    path = shutil.which("ffmpeg")
    if path:
        logger.info(f"FFmpeg found at: {path}")
        return Path(path)
    logger.warning("FFmpeg not found")
    return None

FFMPEG_PATH: Optional[Path] = find_ffmpeg()

def get_common_ydl_opts() -> dict:
    return {
        "quiet": False,
        "no_warnings": False,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "retries": 15,
        "fragment_retries": 15,
        "socket_timeout": 60,
        "extractor_retries": 10,
        "concurrent_fragment_downloads": 8,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        },
    }

def get_cookie_file() -> Optional[str]:
    cookie_path = Path("cookies.txt")
    return str(cookie_path) if cookie_path.exists() else None

def generate_filepath() -> str:
    return str(BASE_DIR / f"{uuid.uuid4()}.%(ext)s")

def cleanup() -> None:
    try:
        for entry in BASE_DIR.iterdir():
            if entry.is_file():
                entry.unlink()
    except Exception:
        logger.warning("Cleanup failed")

def is_supported(url: str) -> bool:
    return any(domain in url.lower() for domain in SUPPORTED_DOMAINS)

def download_video(url: str) -> Tuple[Path, dict]:
    outtmpl = generate_filepath()
    
    if FFMPEG_PATH:
        fmt = "bv*+ba/b"
        merge = "mp4"
    else:
        fmt = "best[ext=mp4]/best"
        merge = None
    
    ydl_opts = {
        **get_common_ydl_opts(),
        "outtmpl": outtmpl,
        "format": fmt,
        "merge_output_format": merge,
        "ffmpeg_location": str(FFMPEG_PATH) if FFMPEG_PATH else None,
        "cookiefile": get_cookie_file(),
    }
    
    logger.info(f"Downloading video: {url}")
    logger.info(f"Using format: {fmt}")
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
    
    files = sorted(
        BASE_DIR.glob(f"{Path(outtmpl).stem}*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise FileNotFoundError("Downloaded file not found")
    
    logger.info(f"Downloaded: {files[0]} ({files[0].stat().st_size} bytes)")
    return files[0], info

def download_audio(url: str) -> Tuple[Path, dict]:
    if not FFMPEG_PATH:
        raise RuntimeError("FFmpeg is required for MP3 conversion")
    
    outtmpl = generate_filepath()
    
    ydl_opts = {
        **get_common_ydl_opts(),
        "outtmpl": outtmpl,
        "format": "bestaudio/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "ffmpeg_location": str(FFMPEG_PATH),
        "cookiefile": get_cookie_file(),
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
    
    mp3_files = list(BASE_DIR.glob(f"{Path(outtmpl).stem}*.mp3"))
    if mp3_files:
        return mp3_files[0], info
    
    files = sorted(
        BASE_DIR.glob(f"{Path(outtmpl).stem}*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise FileNotFoundError("Audio file not found")
    
    return files[0], info

user_links: dict[int, str] = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🎥 Отправь ссылку на видео\n\n"
        "🎬 Видео — видео со звуком\n"
        "🎵 Аудио — MP3"
    )

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    user_id = update.effective_user.id
    
    urls = re.findall(r"https?://[^\s]+", text)
    if not urls:
        await update.message.reply_text("📎 Отправь ссылку на видео")
        return
    
    url = urls[0]
    
    if not is_supported(url):
        await update.message.reply_text("❌ Ссылка не поддерживается")
        return
    
    user_links[user_id] = url
    
    keyboard = [
        [InlineKeyboardButton("🎬 Видео", callback_data="video")],
        [InlineKeyboardButton("🎵 Аудио MP3", callback_data="audio")],
    ]
    await update.message.reply_text(
        "Выбери формат:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    url = user_links.pop(user_id, None)
    
    if not url:
        await query.edit_message_text("❌ Ссылка устарела. Отправь новую.")
        return
    
    choice = query.data
    cleanup()
    
    try:
        if choice == "video":
            await query.edit_message_text("⏳ Скачиваю видео...")
            filepath, info = download_video(url)
            await query.edit_message_text("📤 Отправляю...")
            
            size = filepath.stat().st_size
            if size > MAX_FILE_SIZE:
                raise ValueError(f"Файл слишком большой ({size / 1024 / 1024:.1f} MB)")
            
            with open(filepath, "rb") as f:
                await query.message.reply_video(video=f, caption="✅ Готово!", supports_streaming=True)
        
        elif choice == "audio":
            if not FFMPEG_PATH:
                await query.edit_message_text("❌ MP3 недоступен (нет FFmpeg)")
                return
            
            await query.edit_message_text("⏳ Извлекаю аудио...")
            filepath, info = download_audio(url)
            title = info.get("title", "Audio")
            duration = int(info.get("duration", 0))
            await query.edit_message_text("📤 Отправляю...")
            
            size = filepath.stat().st_size
            if size > MAX_FILE_SIZE:
                raise ValueError(f"Файл слишком большой ({size / 1024 / 1024:.1f} MB)")
            
            with open(filepath, "rb") as f:
                await query.message.reply_audio(audio=f, title=title, duration=duration, caption=f"🎵 {title}")
        
        await query.edit_message_text("✅ Готово! Отправь новую ссылку.")
    
    except ValueError as e:
        await query.edit_message_text(f"❌ {e}")
    except FileNotFoundError:
        await query.edit_message_text("❌ Файл не найден после скачивания")
    except yt_dlp.DownloadError as e:
        error_msg = str(e)
        logger.error(f"Download error: {error_msg}")
        
        if "video unavailable" in error_msg.lower() or "removed" in error_msg.lower():
            await query.edit_message_text("❌ Видео удалено или недоступно")
        elif "private" in error_msg.lower():
            await query.edit_message_text("❌ Приватное видео")
        elif "login" in error_msg.lower() or "sign in" in error_msg.lower():
            await query.edit_message_text("❌ Требуется авторизация. Отправь cookies.txt")
        elif "rate limit" in error_msg.lower() or "429" in error_msg:
            await query.edit_message_text("❌ Слишком много запросов. Подожди немного")
        elif "geo" in error_msg.lower() or "region" in error_msg.lower():
            await query.edit_message_text("❌ Видео недоступно в твоём регионе")
        elif "ffmpeg" in error_msg.lower():
            await query.edit_message_text("❌ Ошибка обработки видео")
        else:
            await query.edit_message_text(f"❌ Не удалось скачать: {error_msg[:100]}")
    except Exception as e:
        logger.error(f"Unexpected error: {traceback.format_exc()}")
        await query.edit_message_text("❌ Сервер временно недоступен")
    finally:
        cleanup()

if __name__ == "__main__":
    logger.info("Starting bot...")
    logger.info(f"FFmpeg: {'found' if FFMPEG_PATH else 'NOT FOUND'}")
    logger.info(f"Python: {os.sys.version}")
    
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    logger.info("Bot is running.")
    app.run_polling()