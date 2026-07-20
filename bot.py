import os
import re
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = "8997095280:AAEgfJXENJCoM06wVG5LRVljVs5Y1YntC7w"

DOWNLOAD_FOLDER = "/tmp/downloads"
AUDIO_FOLDER = "/tmp/audio"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(AUDIO_FOLDER, exist_ok=True)

user_links = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎥 Привет! Отправь ссылку на видео из Instagram или TikTok\n\n"
        "🎬 Видео — скачивает видео со звуком\n"
        "🎵 Аудио — извлекает звук в MP3"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    urls = re.findall(r'https?://[^\s]+', text)
    
    if not urls:
        await update.message.reply_text("📎 Отправь ссылку на видео")
        return
    
    url = urls[0]
    
    if 'instagram.com' not in url.lower() and 'tiktok.com' not in url.lower():
        await update.message.reply_text("❌ Только Instagram и TikTok")
        return
    
    user_links[user_id] = url
    
    keyboard = [
        [InlineKeyboardButton("🎬 Скачать видео", callback_data="video")],
        [InlineKeyboardButton("🎵 Скачать аудио MP3", callback_data="audio")]
    ]
    
    await update.message.reply_text("Выбери формат:", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    
    if user_id not in user_links:
        await query.edit_message_text("❌ Ссылка устарела. Отправь новую.")
        return
    
    url = user_links[user_id]
    choice = query.data
    
    for folder in [DOWNLOAD_FOLDER, AUDIO_FOLDER]:
        for f in os.listdir(folder):
            try:
                os.remove(os.path.join(folder, f))
            except:
                pass
    
    try:
        if choice == "video":
            await query.edit_message_text("⏳ Скачиваю видео...")
            
            ydl_opts = {
                'outtmpl': os.path.join(DOWNLOAD_FOLDER, '%(title)s.%(ext)s'),
                'quiet': True,
                'format': 'mp4/best',  # Сразу mp4
                'merge_output_format': 'mp4',
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(url, download=True)
                
                files = os.listdir(DOWNLOAD_FOLDER)
                if not files:
                    raise Exception("Файл не найден")
                
                video_path = os.path.join(DOWNLOAD_FOLDER, files[0])
                size = os.path.getsize(video_path)
                
                if size > 50 * 1024 * 1024:
                    os.remove(video_path)
                    await query.edit_message_text(f"❌ Слишком большой: {size/1024/1024:.1f} MB")
                    return
                
                await query.edit_message_text("📤 Отправляю...")
                
                with open(video_path, 'rb') as f:
                    await query.message.reply_video(video=f, caption="✅ Готово!")
                
                os.remove(video_path)
                
        elif choice == "audio":
            await query.edit_message_text("⏳ Извлекаю аудио...")
            
            ydl_opts = {
                'outtmpl': os.path.join(AUDIO_FOLDER, '%(title)s.%(ext)s'),
                'quiet': True,
                'format': 'm4a/bestaudio',  # M4A не требует ffmpeg
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(url, download=True)
                
                audio_files = os.listdir(AUDIO_FOLDER)
                if not audio_files:
                    raise Exception("Аудио не найдено")
                
                audio_path = os.path.join(AUDIO_FOLDER, audio_files[0])
                size = os.path.getsize(audio_path)
                
                if size > 50 * 1024 * 1024:
                    os.remove(audio_path)
                    await query.edit_message_text(f"❌ Слишком большой: {size/1024/1024:.1f} MB")
                    return
                
                await query.edit_message_text("📤 Отправляю аудио...")
                
                with open(audio_path, 'rb') as f:
                    await query.message.reply_audio(audio=f, title="Audio", performer="Downloaded")
                
                os.remove(audio_path)
        
        del user_links[user_id]
        await query.edit_message_text("✅ Готово! Отправь новую ссылку.")
        
    except Exception as e:
        await query.edit_message_text(f"❌ Ошибка: {str(e)[:150]}")
        if user_id in user_links:
            del user_links[user_id]

if __name__ == '__main__':
    logger.info("Бот запускается...")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Бот запущен!")
    app.run_polling()