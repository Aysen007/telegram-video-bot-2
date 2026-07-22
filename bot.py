import os
import re
import shutil
import uuid
import logging
import traceback
import requests
import json
import subprocess
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
    return str(cookie_path) if cookie_path.exists() else None

def generate_filepath() -> str:
    return str(BASE_DIR / f"{uuid.uuid4()}.%(ext)s")

def cleanup() -> None:
    try:
        for entry in BASE_DIR.iterdir():
            if entry.is_file():
                entry.unlink()
    except Exception:
        pass

def is_instagram(url: str) -> bool:
    return "instagram.com" in url.lower()

def is_tiktok(url: str) -> bool:
    return "tiktok.com" in url.lower() or "vm.tiktok.com" in url.lower() or "vt.tiktok.com" in url.lower()

def is_supported(url: str) -> bool:
    return is_instagram(url) or is_tiktok(url)

def run_ffmpeg_ensure_playable(input_path: Path, output_path: Path) -> bool:
    """
    Конвертирует видео в H.264 + AAC в MP4 контейнере.
    Это гарантирует воспроизведение в Telegram и других плеерах.
    Сохраняет оригинальный FPS (не форсирует 60) чтобы избежать лагов.
    """
    if not FFMPEG_PATH:
        return False

    try:
        # Получаем оригинальный FPS
        probe_cmd = [
            str(FFMPEG_PATH),
            "-i", str(input_path),
        ]
        probe = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)

        # Определяем FPS из вывода ffmpeg
        fps_match = re.search(r'(\d+(?:\.\d+)?) fps', probe.stderr)
        if fps_match:
            original_fps = fps_match.group(1)
        else:
            original_fps = None

        # Базовые параметры для совместимости
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

        # Сохраняем оригинальный FPS, если определили
        if original_fps:
            cmd.extend(["-r", original_fps])

        cmd.append(str(output_path))

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

        if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
            return True
        else:
            logger.error(f"FFmpeg error: {result.stderr}")
            return False
    except Exception as e:
        logger.error(f"FFmpeg exception: {e}")
        return False

def download_tiktok_video(url: str) -> Tuple[Path, dict]:
    """
    Скачивание TikTok через yt-dlp.
    Используем cookies для обхода ограничений.
    """
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

    # Конвертируем в совместимый формат (H.264 + AAC)
    if FFMPEG_PATH:
        output_path = BASE_DIR / f"{uuid.uuid4()}.mp4"
        if run_ffmpeg_ensure_playable(filepath, output_path):
            filepath.unlink()
            return output_path, info
        else:
            logger.warning("FFmpeg conversion failed, returning original file")

    return filepath, info

def download_instagram_video(url: str) -> Tuple[Path, dict]:
    """
    Скачивание Instagram через yt-dlp с cookies.
    yt-dlp может скачать HEVC видео без аудио — проверяем и конвертируем.
    """
    outtmpl = generate_filepath()

    ydl_opts = {
        **get_common_ydl_opts(),
        "outtmpl": outtmpl,
        # Берём лучший формат с аудио, предпочтительно mp4
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

    # Проверяем, есть ли аудио в файле
    has_audio = False
    if FFMPEG_PATH:
        try:
            probe = subprocess.run(
                [str(FFMPEG_PATH), "-i", str(filepath)],
                capture_output=True, text=True, timeout=30
            )
            has_audio = "Audio:" in probe.stderr or "audio" in probe.stderr.lower()
            logger.info(f"File {filepath.name} has audio: {has_audio}")
        except Exception as e:
            logger.warning(f"Could not probe audio: {e}")

    # Конвертируем в H.264 + AAC для гарантированного воспроизведения
    if FFMPEG_PATH:
        output_path = BASE_DIR / f"{uuid.uuid4()}.mp4"
        if run_ffmpeg_ensure_playable(filepath, output_path):
            filepath.unlink()
            return output_path, info
        else:
            logger.warning("FFmpeg conversion failed, returning original file")

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

    # Сначала скачиваем видео
    video_path, info = download_video(url)
    audio_path = video_path.with_suffix(".mp3")

    try:
        subprocess.run(
            [
                str(FFMPEG_PATH),
                "-i", str(video_path),
                "-vn",  # no video
                "-c:a", "libmp3lame",
                "-q:a", "0",  # highest quality
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

user_links: dict[int, str] = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🎥 *Скачаю что угодно, пока ты занят*\n\n"
        "Кидай ссылку — я сам разберусь.\n"
        "• Instagram\n"
        "• TikTok\n\n"
        "🎬 Видео — в лучшем качестве со звуком\n"
        "🎵 Аудио — MP3 320kbps\n\n"
        "Работаю 24/7. Даже когда ты спишь.",
        parse_mode="MarkdownV2"
    )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    file = await update.message.document.get_file()
    await file.download_to_drive("cookies.txt")
    await update.message.reply_text("✅ Cookies сохранены! Instagram будет работать!")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    user_id = update.effective_user.id

    urls = re.findall(r"https?://[^\s]+", text)
    if not urls:
        await update.message.reply_text("📎 Отправь ссылку на видео")
        return

    url = urls[0]

    if not is_supported(url):
        await update.message.reply_text("❌ Поддерживаются только Instagram и TikTok")
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

if __name__ == "__main__":
    logger.info("Starting bot...")
    logger.info(f"FFmpeg: {'found' if FFMPEG_PATH else 'NOT FOUND'}")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("Bot is running.")
    app.run_polling()
