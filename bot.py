# bot.py

import os
import re
import shutil
import uuid
import logging
from pathlib import Path
from typing import Optional, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import yt_dlp

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("MediaBot")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
TOKEN: str = os.environ["8997095280:AAEgfJXENJCoM06wVG5LRVljVs5Y1YntC7w"]
BASE_DIR: Path = Path("/tmp/media_bot")
BASE_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE: int = 49 * 1024 * 1024  # 49 MB (safe limit for Telegram)
SUPPORTED_DOMAINS: list[str] = [
    "instagram.com",
    "tiktok.com",
    "youtube.com",
    "youtu.be",
    "facebook.com",
    "fb.watch",
    "twitter.com",
    "x.com",
    "threads.net",
]

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def find_ffmpeg() -> Optional[Path]:
    """Locate ffmpeg binary. Returns Path or None."""
    path = shutil.which("ffmpeg")
    return Path(path) if path else None


FFMPEG_PATH: Optional[Path] = find_ffmpeg()


def get_common_ydl_opts() -> dict:
    """Return base options shared by every download."""
    return {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 30,
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
    """Return cookie file path if it exists."""
    cookie_path = Path("cookies.txt")
    return str(cookie_path) if cookie_path.exists() else None


def generate_filepath() -> str:
    """Generate a unique file path inside the base directory."""
    return str(BASE_DIR / f"{uuid.uuid4()}.%(ext)s")


def cleanup() -> None:
    """Remove all temporary files."""
    try:
        for entry in BASE_DIR.iterdir():
            if entry.is_file():
                entry.unlink()
    except Exception:
        logger.warning("Cleanup failed – continuing anyway.")


def is_supported(url: str) -> bool:
    """Check whether the URL belongs to a supported platform."""
    return any(domain in url.lower() for domain in SUPPORTED_DOMAINS)


async def send_video(update: Update, filepath: Path) -> None:
    """Send a video file with size check."""
    size = filepath.stat().st_size
    if size > MAX_FILE_SIZE:
        raise ValueError(f"File too large ({size / 1024 / 1024:.1f} MB)")

    with open(filepath, "rb") as f:
        await update.message.reply_video(
            video=f,
            caption="✅ Готово!",
            supports_streaming=True,
        )


async def send_audio(update: Update, filepath: Path, title: str, duration: int) -> None:
    """Send an audio file with size check."""
    size = filepath.stat().st_size
    if size > MAX_FILE_SIZE:
        raise ValueError(f"File too large ({size / 1024 / 1024:.1f} MB)")

    with open(filepath, "rb") as f:
        await update.message.reply_audio(
            audio=f,
            title=title,
            duration=duration,
            caption=f"🎵 {title}",
        )


# --------------------------------------------------------------------------- #
# Download functions
# --------------------------------------------------------------------------- #
def download_video(url: str) -> Tuple[Path, dict]:
    """
    Download the best available video.
    Returns (filepath, info_dict).
    """
    outtmpl = generate_filepath()

    if FFMPEG_PATH:
        # Merge best video + best audio
        fmt = "bv*+ba/b"
        merge = "mp4"
        postprocessors = None
    else:
        # Single file already containing audio
        fmt = "best[ext=mp4]/best"
        merge = None
        postprocessors = None

    ydl_opts = {
        **get_common_ydl_opts(),
        "outtmpl": outtmpl,
        "format": fmt,
        "merge_output_format": merge,
        "postprocessors": postprocessors,
        "ffmpeg_location": str(FFMPEG_PATH) if FFMPEG_PATH else None,
        "cookiefile": get_cookie_file(),
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    # Find the downloaded file
    files = sorted(
        BASE_DIR.glob(f"{Path(outtmpl).stem}*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise FileNotFoundError("Downloaded file not found")

    return files[0], info


def download_audio(url: str) -> Tuple[Path, dict]:
    """
    Download best audio and convert to MP3.
    Requires FFmpeg.
    """
    if not FFMPEG_PATH:
        raise RuntimeError("FFmpeg is required for MP3 conversion")

    outtmpl = generate_filepath()

    ydl_opts = {
        **get_common_ydl_opts(),
        "outtmpl": outtmpl,
        "format": "bestaudio/best",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "ffmpeg_location": str(FFMPEG_PATH),
        "cookiefile": get_cookie_file(),
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    # After conversion the extension is .mp3
    mp3_files = list(BASE_DIR.glob(f"{Path(outtmpl).stem}*.mp3"))
    if mp3_files:
        return mp3_files[0], info

    # Fallback: any file
    files = sorted(
        BASE_DIR.glob(f"{Path(outtmpl).stem}*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise FileNotFoundError("Audio file not found")

    return files[0], info


# --------------------------------------------------------------------------- #
# Telegram handlers
# --------------------------------------------------------------------------- #
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
            await send_video(update, filepath)

        elif choice == "audio":
            if not FFMPEG_PATH:
                await query.edit_message_text("❌ MP3 недоступен (нет FFmpeg)")
                return

            await query.edit_message_text("⏳ Извлекаю аудио...")
            filepath, info = download_audio(url)
            title = info.get("title", "Audio")
            duration = int(info.get("duration", 0))
            await query.edit_message_text("📤 Отправляю...")
            await send_audio(update, filepath, title, duration)

        await query.edit_message_text("✅ Готово! Отправь новую ссылку.")

    except ValueError as e:
        await query.edit_message_text(f"❌ {e}")
    except FileNotFoundError:
        await query.edit_message_text("❌ Файл не найден после скачивания")
    except yt_dlp.DownloadError as e:
        msg = str(e).lower()
        if "video unavailable" in msg or "removed" in msg:
            await query.edit_message_text("❌ Видео удалено или недоступно")
        elif "private" in msg:
            await query.edit_message_text("❌ Приватное видео")
        elif "login" in msg:
            await query.edit_message_text("❌ Требуется авторизация (cookies.txt)")
        else:
            await query.edit_message_text("❌ Не удалось скачать")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        await query.edit_message_text("❌ Сервер временно недоступен")
    finally:
        cleanup()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    logger.info("Starting bot...")
    logger.info(f"FFmpeg: {'found' if FFMPEG_PATH else 'NOT FOUND'}")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler)
    )

    logger.info("Bot is running.")
    app.run_polling()