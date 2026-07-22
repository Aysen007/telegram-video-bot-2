# Media Downloader Bot

Telegram bot for downloading videos / audio from popular platforms.

## Platforms
- Instagram (Reels, Posts, Stories)
- TikTok (with watermark-free support)

## Features
- 🎬 Video with sound in H.264 format (plays everywhere)
- 🎵 Audio extraction to MP3 320kbps
- 🚀 No lags, no freezes — original FPS preserved
- 🔐 Instagram auth via cookies.txt

## Setup
1. Deploy to Railway
2. Set environment variable `TOKEN` = your Telegram bot token
3. *(optional)* Upload `cookies.txt` for authenticated Instagram downloads
4. *(optional)* Upload `cookies.txt` with TikTok cookies for better reliability

## How to get cookies.txt
1. Install "Get cookies.txt LOCALLY" extension for Chrome/Firefox
2. Log in to Instagram (and TikTok if needed)
3. Export cookies to `cookies.txt`
4. Send the file to the bot via Telegram
