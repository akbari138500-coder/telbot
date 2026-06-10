# 📥 Telegram Downloader & Converter Bot (ربات دانلودر و مبدل تلگرام)

A highly advanced, standalone Telegram Bot written in Python that downloads YouTube/social media videos (including Instagram, TikTok, Twitter/X) and direct file links. It also features a fully-fledged, serverless **File Format Converter** allowing users to convert images, audio formats, and documents (Word/PDF) directly inside the chat.

یک ربات تلگرام پیشرفته و مستقل نوشته شده به زبان پایتون برای دانلود ویدیوهای یوتیوب/شبکه‌های اجتماعی (اینستاگرام، تیک‌تاک، توییتر)، لینک‌های مستقیم و همچنین **مبدل فرمت فایل‌های مختلف** (عکس، آهنگ، اسناد آفیس و PDF) به صورت کاملاً رایگان و مستقیم در محیط چت.

---

## ⚡ Features (قابلیت‌ها)

- 🎥 **YouTube & Social Media Downloader:** Downloads from YouTube, Instagram Reels, TikTok, Twitter/X, Facebook, and more.
- 🔄 **File Format Converter (مبدل فرمت فایل):**
  - **🖼 Images:** Convert between `PNG`, `JPG`, `WebP`, and `PDF` (creates a PDF booklet from your image).
  - **🎵 Audio:** Convert audio files and voice messages to `MP3`, `WAV`, or `OGG` (Telegram voice message compatible).
  - **📝 PDF to Text:** Extracts readable text from PDF files and sends it as a `.txt` file.
  - **📄 Word to PDF:** Converts Word documents (`.docx`) to portable document format (`.pdf`) using native system libraries (LibreOffice on cloud/Linux, MS Word on Windows).
- 🎵 **Audio Extraction:** Option to download only the audio stream as an MP3 file.
- 🎛️ **Interactive Menus:** Choose format options or conversion formats via inline buttons.
- 👥 **Queue System:** Sequentially processes downloads and file conversions one-by-one to prevent server congestion, updating users with their queue position.
- 📁 **Custom Renaming:** Rename direct links on-the-fly by adding `--name filename.ext` to your link message.
- 🎞️ **Playable Video Uploads:** Sends videos under 50MB as playable in-app videos instead of raw documents.
- 🎨 **Cover (Thumbnail) Support:** Automatically extracts video cover art and attaches it to the uploaded file.
- 📊 **Visual Progress Bar:** Shows live download progress using a graphical bar (e.g., `██████░░░░ 60%`).
- 🔍 **YouTube Search:** Search YouTube videos directly in chat using `/search <query>` and select which video to download.
- 📦 **Smart Splitting (FFmpeg & Binary):**
  - If **FFmpeg** is installed: Splits large videos into *playable video parts* (you can watch each part directly inside Telegram without merging).
  - If **FFmpeg** is missing: Falls back to splitting files into raw binary parts (`.part001`, `.part002`, etc.).
- 🧹 **Auto Cleanup:** Deletes downloaded files from the disk immediately after sending to save storage.
- 📈 **Stats Dashboard:** Users can check their download metrics with `/stats`, while administrators can monitor global usage, user numbers, and total bandwidth.

---

## 🛠️ Setup & Installation (راه‌اندازی و نصب)

### 1. Prerequisites (پیش‌نیازها)
Ensure you have **Python 3.12+** installed on your system.
*(Optional)* Install **FFmpeg** to enable playable video splitting and MP3/OGG conversions.

مطمئن شوید پایتون ۳.۱۲ یا بالاتر نصب است. (اختیاری: جهت استخراج MP3 و تقسیم ویدیوها به پارت‌های قابل پخش، برنامه FFmpeg را روی سیستم خود نصب کنید).

### 2. Project Directory (ورود به پوشه پروژه)
```bash
cd c:\Users\Admin\Desktop\telegram-downloader-bot
```

### 3. Create & Activate Virtual Environment (ساخت و فعال‌سازی محیط مجازی)

**Windows (cmd/powershell):**
```powershell
python -m venv venv
.\venv\Scripts\activate
```

**Linux/macOS:**
```bash
python3 -m venv venv
source venv/bin/activate
```

### 4. Install Dependencies (نصب کتابخانه‌ها)
```bash
pip install -r requirements.txt
```

### 5. Configuration (تنظیم توکن ربات)
Open the `.env` file and replace `YOUR_TELEGRAM_BOT_TOKEN_HERE` with your bot's token from **@BotFather**:
```env
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGhIJKlmNoPQRsTUVwxyZ
ADMIN_USER_ID=
CLOUD_CHANNEL_ID=
```

---

## 🚀 Running the Bot (اجرای ربات)

Ensure your virtual environment is active, then run:
```bash
python bot.py
```

---

## 📖 How to Use (نحوه استفاده)

### 1. File Format Converter (مبدل فرمت فایل)
Simply drag and drop or send any file (Photo, Document, PDF, DOCX, Audio) to the bot. The bot will automatically inspect it and offer interactive buttons to convert it to your desired format.

کافیست هر فایلی (عکس، پی‌دی‌اف، سند ورد، آهنگ) را به ربات بفرستید. ربات به صورت خودکار آن را تحلیل کرده و دکمه‌های تبدیل مربوطه را نشان می‌دهد.

### 2. Download YouTube/Social Media Video
Just send the link! The bot will extract information and show you inline buttons to select:
- **🎥 Download Video** (Best quality)
- **🎵 Download MP3** (Audio only)
- **✂️ Trim Range** (Select custom start/end time)

### 3. Search YouTube
Use the `/search` command followed by your query:
```text
/search coldplay yellow
```
The bot will return the top 5 search results with inline buttons to download any of them.

### 4. Direct Link with Custom Name
Send a direct URL and add `--name` with your desired filename:
```text
https://example.com/file_123456.zip --name my_app.zip
```

---

## 🔧 How to Merge Split Parts (نحوه سرهم‌کردن پارت‌ها)

If **FFmpeg is not installed**, large files will be split into binary chunks (`.part001`, `.part002`). To merge them back:

### 💻 Windows (CMD):
Open CMD in the folder containing the downloaded parts and run the copy command generated by the bot in the final message:
```cmd
copy /b "video.mp4.part001" + "video.mp4.part002" "video.mp4"
```

### 🍎 Linux & macOS:
```bash
cat "video.mp4.part*" > "video.mp4"
```

### 💡 Graphical Interface:
Put all parts in the same folder, right-click on the first part (`.part001`), and extract it using **WinRAR** or **7-Zip**.
