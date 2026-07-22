import os
import re
import shutil
import uuid
import logging
import traceback
import requests
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import yt_dlp

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.DEBUG,  # DEBUG для максимального логирования
)
logger = logging.getLogger("MediaBot")

# Логируем версии
logger.info(f"Python version: {sys.version}")
logger.info(f"yt-dlp version: {yt_dlp.version.__version__}")

# === КОНФИГУРАЦИЯ ===
TOKEN: str = os.environ.get("TOKEN", "")
if not TOKEN:
    logger.error("TOKEN environment variable is not set!")
    raise RuntimeError("TOKEN environment variable is required")

logger.info(f"TOKEN loaded: {TOKEN[:10]}... (length: {len(TOKEN)})")

BASE_DIR: Path = Path("/tmp/media_bot")
BASE_DIR.mkdir(parents=True, exist_ok=True)
logger.info(f"BASE_DIR: {BASE_DIR}")

MAX_FILE_SIZE: int = 49 * 1024 * 1024

# === FFMPEG ===
def find_ffmpeg() -> Optional[Path]:
    path = shutil.which("ffmpeg")
    if path:
        logger.info(f"FFmpeg found at: {path}")
        return Path(path)
    logger.warning("FFmpeg not found")
    return None

FFMPEG_PATH: Optional[Path] = find_ffmpeg()
logger.info(f"FFMPEG_PATH: {FFMPEG_PATH}")

# === YT-DLP ОПЦИИ ===
def get_common_ydl_opts() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 30,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

def get_cookie_file() -> Optional[str]:
    cookie_path = Path("cookies.txt")
    exists = cookie_path.exists()
    logger.info(f"cookies.txt exists: {exists}")
    return str(cookie_path) if exists else None

def generate_filepath() -> str:
    return str(BASE_DIR / f"{uuid.uuid4()}.%(ext)s")

def cleanup() -> None:
    try:
        count = 0
        for entry in BASE_DIR.iterdir():
            if entry.is_file():
                entry.unlink()
                count += 1
        logger.info(f"Cleanup removed {count} files")
    except Exception as e:
        logger.warning(f"Cleanup error: {e}")

# === URL ПРОВЕРКИ ===
def is_instagram(url: str) -> bool:
    return "instagram.com" in url.lower()

def is_tiktok(url: str) -> bool:
    return "tiktok.com" in url.lower() or "vm.tiktok.com" in url.lower() or "vt.tiktok.com" in url.lower()

def is_supported(url: str) -> bool:
    return is_instagram(url) or is_tiktok(url)

# === FFMPEG КОНВЕРТАЦИЯ ===
def run_ffmpeg_ensure_playable(input_path: Path, output_path: Path) -> bool:
    if not FFMPEG_PATH:
        return False

    try:
        probe_cmd = [str(FFMPEG_PATH), "-i", str(input_path)]
        probe = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)

        fps_match = re.search(r'(\d+(?:\.\d+)?) fps', probe.stderr)
        original_fps = fps_match.group(1) if fps_match else None

        cmd = [
            str(FFMPEG_PATH),
            "-i", str(input_path),
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            "-y",
        ]

        if original_fps:
            cmd.extend(["-r", original_fps])

        cmd.append(str(output_path))

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

        if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
            logger.info(f"FFmpeg conversion successful: {output_path}")
            return True
        else:
            logger.error(f"FFmpeg error: {result.stderr}")
            return False
    except Exception as e:
        logger.error(f"FFmpeg exception: {e}")
        return False

# === СКАЧИВАНИЕ ===
def download_tiktok_video(url: str) -> Tuple[Path, dict]:
    outtmpl = generate_filepath()

    ydl_opts = {
        **get_common_ydl_opts(),
        "outtmpl": outtmpl,
        "format": "best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best",
        "merge_output_format": "mp4",
        "ffmpeg_location": str(FFMPEG_PATH) if FFMPEG_PATH else None,
        "cookiefile": get_cookie_file(),
        "extractor_args": {
            "tiktok": {
                "webpage_download": True,
            }
        },
        "headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.tiktok.com/",
        }
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    files = sorted(
        BASE_DIR.glob(f"{Path(outtmpl).stem}*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise FileNotFoundError("Downloaded file not found")

    filepath = files[0]

    if FFMPEG_PATH:
        output_path = BASE_DIR / f"{uuid.uuid4()}.mp4"
        if run_ffmpeg_ensure_playable(filepath, output_path):
            filepath.unlink()
            return output_path, info

    return filepath, info

def download_instagram_video(url: str) -> Tuple[Path, dict]:
    outtmpl = generate_filepath()

    ydl_opts = {
        **get_common_ydl_opts(),
        "outtmpl": outtmpl,
        "format": "best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best",
        "merge_output_format": "mp4",
        "ffmpeg_location": str(FFMPEG_PATH) if FFMPEG_PATH else None,
        "cookiefile": get_cookie_file(),
        "headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.instagram.com/",
        }
    }

    logger.info(f"Downloading Instagram: {url}")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    files = sorted(
        BASE_DIR.glob(f"{Path(outtmpl).stem}*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise FileNotFoundError("Downloaded file not found")

    filepath = files[0]

    if FFMPEG_PATH:
        output_path = BASE_DIR / f"{uuid.uuid4()}.mp4"
        if run_ffmpeg_ensure_playable(filepath, output_path):
            filepath.unlink()
            return output_path, info

    return filepath, info

def download_video(url: str) -> Tuple[Path, dict]:
    if is_tiktok(url):
        return download_tiktok_video(url)
    elif is_instagram(url):
        return download_instagram_video(url)
    else:
        raise ValueError("Unsupported URL")

def download_audio(url: str) -> Tuple[Path, dict]:
    if not FFMPEG_PATH:
        raise RuntimeError("FFmpeg is required for MP3 conversion")

    video_path, info = download_video(url)
    audio_path = video_path.with_suffix(".mp3")

    try:
        subprocess.run(
            [
                str(FFMPEG_PATH),
                "-i", str(video_path),
                "-vn",
                "-c:a", "libmp3lame",
                "-q:a", "0",
                "-y",
                str(audio_path)
            ],
            check=True, capture_output=True, timeout=120
        )
        video_path.unlink()
        return audio_path, info
    except subprocess.CalledProcessError as e:
        video_path.unlink()
        raise RuntimeError(f"Audio extraction failed: {e.stderr.decode() if e.stderr else str(e)}")

# === ХРАНИЛИЩЕ ССЫЛОК ===
user_links: dict[int, str] = {}

# === ОБРАБОТЧИКИ ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info(f"=== /start received from user {update.effective_user.id} ===")
    try:
        text = (
            "🎥 <b>Скачаю что угодно, пока ты занят</b>\n\n"
            "Кидай ссылку — я сам разберусь.\n"
            "• Instagram\n"
            "• TikTok\n\n"
            "🎬 <b>Видео</b> — в лучшем качестве со звуком\n"
            "🎵 <b>Аудио</b> — MP3 320kbps\n\n"
            "Работаю 24/7. Даже когда ты спишь."
        )
        logger.info(f"Sending start message with HTML parse_mode")
        await update.message.reply_text(text, parse_mode="HTML")
        logger.info("Start message sent successfully")
    except Exception as e:
        logger.error(f"Error in start handler: {e}")
        logger.error(traceback.format_exc())
        try:
            # Fallback без parse_mode
            await update.message.reply_text("Привет! Кидай ссылку на Instagram или TikTok.")
        except Exception as e2:
            logger.error(f"Even fallback failed: {e2}")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info(f"Document received from user {update.effective_user.id}")
    try:
        file = await update.message.document.get_file()
        await file.download_to_drive("cookies.txt")
        await update.message.reply_text("✅ Cookies сохранены! Instagram будет работать!")
        logger.info("Cookies saved successfully")
    except Exception as e:
        logger.error(f"Error handling document: {e}")
        await update.message.reply_text("❌ Ошибка при сохранении cookies")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info(f"Message received from user {update.effective_user.id}: {update.message.text[:50]}...")

    text = update.message.text
    user_id = update.effective_user.id

    urls = re.findall(r"https?://[^\s]+", text)
    if not urls:
        await update.message.reply_text("📎 Отправь ссылку на видео")
        return

    url = urls[0]
    logger.info(f"Found URL: {url}")

    if not is_supported(url):
        await update.message.reply_text("❌ Поддерживаются только Instagram и TikTok")
        return

    user_links[user_id] = url
    logger.info(f"Link stored for user {user_id}")

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

            size = filepath.stat().st_size
            if size > MAX_FILE_SIZE:
                raise ValueError(f"Файл слишком большой ({size / 1024 / 1024:.1f} MB)")

            await query.edit_message_text("📤 Отправляю...")

            with open(filepath, "rb") as f:
                await query.message.reply_video(
                    video=f,
                    caption="✅ Готово!",
                    supports_streaming=True,
                    width=info.get("width", 0),
                    height=info.get("height", 0),
                    duration=int(info.get("duration", 0)) if info.get("duration") else None,
                )

            await query.message.reply_text(
                "💡 Больше информации в нашем канале: https://t.me/zvucovideo\n"
                "💡 Там ты найдёшь гайды по использованию бота и много чего ещё!"
            )

        elif choice == "audio":
            if not FFMPEG_PATH:
                await query.edit_message_text("❌ MP3 недоступен (нет FFmpeg)")
                return

            await query.edit_message_text("⏳ Извлекаю аудио...")
            filepath, info = download_audio(url)
            title = info.get("title", "Audio") or info.get("uploader", "Audio")
            duration = int(info.get("duration", 0)) if info.get("duration") else 0

            size = filepath.stat().st_size
            if size > MAX_FILE_SIZE:
                raise ValueError(f"Файл слишком большой ({size / 1024 / 1024:.1f} MB)")

            await query.edit_message_text("📤 Отправляю...")

            with open(filepath, "rb") as f:
                await query.message.reply_audio(
                    audio=f,
                    title=title,
                    duration=duration,
                    caption=f"🎵 {title}"
                )

            await query.message.reply_text(
                "💡 Больше информации в нашем канале: https://t.me/zvucovideo\n"
                "💡 Там ты найдёшь гайды по использованию бота и много чего ещё!"
            )

        await query.edit_message_text("✅ Готово! Отправь новую ссылку.")

    except ValueError as e:
        await query.edit_message_text(f"❌ {e}")
    except FileNotFoundError:
        await query.edit_message_text("❌ Файл не найден")
    except yt_dlp.DownloadError as e:
        error_msg = str(e)
        logger.error(f"Download error: {error_msg}")

        if "login" in error_msg.lower() or "empty media" in error_msg.lower():
            await query.edit_message_text("❌ Instagram требует авторизацию. Отправь мне cookies.txt")
        elif "unavailable" in error_msg.lower() or "not found" in error_msg.lower():
            await query.edit_message_text("❌ Видео недоступно или удалено")
        else:
            await query.edit_message_text("❌ Не удалось скачать. Попробуй другую ссылку")
    except Exception as e:
        logger.error(f"Unexpected error: {traceback.format_exc()}")
        await query.edit_message_text("❌ Сервер временно недоступен")
    finally:
        cleanup()

# === ГЛАВНЫЙ ЗАПУСК ===
if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info("STARTING BOT")
    logger.info("=" * 50)
    logger.info(f"FFmpeg: {'found' if FFMPEG_PATH else 'NOT FOUND'}")
    logger.info(f"Cookies: {get_cookie_file()}")

    try:
        logger.info("Building application...")
        app = Application.builder().token(TOKEN).build()
        logger.info("Application built successfully")

        logger.info("Registering handlers...")
        app.add_handler(CommandHandler("start", start))
        logger.info("- CommandHandler('start') registered")

        app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
        logger.info("- MessageHandler(DOCUMENT) registered")

        app.add_handler(CallbackQueryHandler(button_handler))
        logger.info("- CallbackQueryHandler registered")

        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
        logger.info("- MessageHandler(TEXT & ~COMMAND) registered")

        logger.info("All handlers registered. Starting polling...")
        app.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
    except Exception as e:
        logger.error(f"FATAL ERROR during startup: {e}")
        logger.error(traceback.format_exc())
        raise
