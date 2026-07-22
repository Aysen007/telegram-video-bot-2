import os
import re
import shutil
import uuid
import logging
import traceback
import requests
import json
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
    return "tiktok.com" in url.lower()

def is_supported(url: str) -> bool:
    return is_instagram(url) or is_tiktok(url)

def download_tiktok_video(url: str) -> Tuple[Path, dict]:
    """Скачивание TikTok через API с запасным вариантом"""
    # Извлекаем ID видео
    video_id = None
    
    if "/video/" in url:
        match = re.search(r'/video/(\d+)', url)
        if match:
            video_id = match.group(1)
    elif "vm.tiktok.com" in url or "vt.tiktok.com" in url:
        try:
            response = requests.head(url, allow_redirects=True, headers={
                "User-Agent": "Mozilla/5.0"
            }, timeout=10)
            final_url = response.url
            match = re.search(r'/video/(\d+)', final_url)
            if match:
                video_id = match.group(1)
        except:
            pass
    
    # Если нашли ID — пробуем API
    if video_id:
        try:
            api_url = f"https://api16-normal-c-useast1a.tiktokv.com/aweme/v1/feed/?aweme_id={video_id}"
            headers = {
                "User-Agent": "com.zhiliaoapp.musically/2022600040 (Linux; U; Android 13; en_US; Pixel 7; Build/TQ2A.230505.002; Cronet/113.0.5672.131)",
            }
            
            response = requests.get(api_url, headers=headers, timeout=30)
            
            if response.status_code == 200 and response.text:
                data = response.json()
                
                if data.get("status_code") == 0:
                    aweme_list = data.get("aweme_list", [])
                    if aweme_list:
                        video_data = aweme_list[0]
                        video_url = video_data.get("video", {}).get("play_addr", {}).get("url_list", [None])[0]
                        
                        if video_url:
                            video_response = requests.get(video_url, headers=headers, timeout=60)
                            filepath = BASE_DIR / f"{uuid.uuid4()}.mp4"
                            
                            with open(filepath, "wb") as f:
                                f.write(video_response.content)
                            
                            info = {
                                "title": video_data.get("desc", "TikTok Video"),
                                "duration": video_data.get("duration", 0),
                            }
                            
                            return filepath, info
        except:
            logger.warning("TikTok API failed, trying yt-dlp")
    
    # Запасной вариант — yt-dlp
    outtmpl = generate_filepath()
    ydl_opts = {
        **get_common_ydl_opts(),
        "outtmpl": outtmpl,
        "format": "best",
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent": "com.zhiliaoapp.musically/2022600040",
        },
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
    
    return files[0], info

def download_video(url: str) -> Tuple[Path, dict]:
    # Для TikTok используем свой метод
    if is_tiktok(url):
        return download_tiktok_video(url)
    
    # Для Instagram используем yt-dlp
    outtmpl = generate_filepath()
    
    ydl_opts = {
        **get_common_ydl_opts(),
        "outtmpl": outtmpl,
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "ffmpeg_location": str(FFMPEG_PATH) if FFMPEG_PATH else None,
        "cookiefile": get_cookie_file(),
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
    
    return files[0], info

def download_audio(url: str) -> Tuple[Path, dict]:
    if not FFMPEG_PATH:
        raise RuntimeError("FFmpeg is required for MP3 conversion")
    
    if is_tiktok(url):
        # Для TikTok сначала качаем видео, потом извлекаем аудио
        video_path, info = download_tiktok_video(url)
        audio_path = video_path.with_suffix(".mp3")
        
        import subprocess
        subprocess.run(
            [str(FFMPEG_PATH), "-i", str(video_path), "-q:a", "0", "-map", "a", str(audio_path)],
            check=True, capture_output=True
        )
        video_path.unlink()
        return audio_path, info
    
    # Для Instagram
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
        "🎥 *Скачаю что угодно, пока ты занят*\n\n"
        "Кидай ссылку — я сам разберусь.\n"
        "• Instagram\n"
        "• TikTok\n\n"
        "🎬 Видео — в лучшем качестве со звуком\n"
        "🎵 Аудио — MP3 320kbps\n\n"
        "Работаю 24/7. Даже когда ты спишь.",
        parse_mode="Markdown"
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
                await query.message.reply_video(video=f, caption="✅ Готово!", supports_streaming=True)
            
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
            title = info.get("title", "Audio")
            duration = int(info.get("duration", 0))
            
            size = filepath.stat().st_size
            if size > MAX_FILE_SIZE:
                raise ValueError(f"Файл слишком большой ({size / 1024 / 1024:.1f} MB)")
            
            await query.edit_message_text("📤 Отправляю...")
            
            with open(filepath, "rb") as f:
                await query.message.reply_audio(audio=f, title=title, duration=duration, caption=f"🎵 {title}")
            
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