import os
import re
import time
import uuid
import shutil
import sqlite3
import zipfile
import asyncio
import logging
import tempfile
import mimetypes
import subprocess
from urllib.parse import urlparse, unquote
import aiohttp
import yt_dlp
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")  # Optional: For global stats access
CLOUD_CHANNEL_ID = os.getenv("CLOUD_CHANNEL_ID")  # Optional: For cloud archiving

# Setup logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Constants
MAX_PART_SIZE = 49 * 1024 * 1024  # 49MB (Telegram limit is 50MB)
DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━━━"

# State Cache & Databases
URL_CACHE = {}          # Store media details (uuid -> data)
USER_STATES = {}        # User states (user_id -> {'state': '...', 'url_id': '...'})
download_queue = asyncio.Queue()  # Global sequential task queue

# =====================================================================
# Database Manager (SQLite)
# =====================================================================
class DbManager:
    """Manages bot statistics using an embedded SQLite database."""
    def __init__(self, db_path="bot_data.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.create_tables()

    def create_tables(self):
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    download_type TEXT,
                    file_size INTEGER,
                    timestamp REAL
                )
            """)

    def log_download(self, user_id, download_type, file_size):
        with self.conn:
            self.conn.execute(
                "INSERT INTO stats (user_id, download_type, file_size, timestamp) VALUES (?, ?, ?, ?)",
                (user_id, download_type, file_size, time.time())
            )

    def get_user_stats(self, user_id):
        with self.conn:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT COUNT(*), SUM(file_size) FROM stats WHERE user_id = ?",
                (user_id,)
            )
            count, total_size = cursor.fetchone()
            return count or 0, total_size or 0

    def get_global_stats(self):
        with self.conn:
            cursor = self.conn.cursor()
            cursor.execute("SELECT COUNT(*), SUM(file_size), COUNT(DISTINCT user_id) FROM stats")
            count, total_size, unique_users = cursor.fetchone()
            return count or 0, total_size or 0, unique_users or 0

# Initialize DB
db = DbManager()

# =====================================================================
# Helpers & Formatting
# =====================================================================
def get_progress_bar(percent):
    """Generates an elegant premium progress bar using blocks."""
    completed = int(percent / 10)
    bar = "▰" * completed + "▱" * (10 - completed)
    return f"`{bar} {percent:.1f}%`"

def parse_time_str(time_str):
    """Parses time strings like MM:SS or HH:MM:SS to total seconds."""
    time_str = time_str.strip()
    parts = time_str.split(':')
    try:
        if len(parts) == 1:
            return int(parts[0])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return None
    return None

def format_seconds(seconds):
    """Formats seconds to MM:SS or HH:MM:SS format."""
    if seconds < 3600:
        return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"
    return f"{int(seconds // 3600):02d}:{int((seconds % 3600) // 60):02d}:{int(seconds % 60):02d}"

async def bypass_url(url):
    """Bypasses URL redirect shorteners (bit.ly, tinyurl, etc.) recursively."""
    async with aiohttp.ClientSession() as session:
        current_url = url
        headers = {"User-Agent": "Mozilla/5.0"}
        for _ in range(5):  # Max 5 hops
            try:
                async with session.head(current_url, headers=headers, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        next_url = resp.headers.get('Location')
                        if next_url:
                            current_url = next_url
                            continue
                    break
            except Exception:
                break
        return current_url

async def download_thumbnail(url, dest_dir):
    """Downloads video thumbnail in the background."""
    if not url:
        return None
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    path = os.path.join(dest_dir, "thumb.jpg")
                    with open(path, "wb") as f:
                        f.write(await resp.read())
                    return path
        except Exception as e:
            logger.warning(f"Failed to download thumbnail: {e}")
    return None

def yt_dlp_hook(d, tracker):
    """Processes yt-dlp download status hook and pushes formatted updates."""
    if d["status"] == "downloading":
        filename = os.path.basename(d.get("filename", "video"))
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        downloaded = d.get("downloaded_bytes", 0)
        speed = d.get("speed", 0) or 0
        
        speed_mb = speed / (1024 * 1024) if speed else 0
        downloaded_mb = downloaded / (1024 * 1024)
        total_mb = total / (1024 * 1024) if total else 0
        
        percent = (downloaded / total) * 100 if total else 0
        bar = get_progress_bar(percent) if total else "`Downloading...`"
        
        text = (
            f"📥 *DOWNLOADING MEDIA / در حال دانلود* 📥\n"
            f"{DIVIDER}\n"
            f"📁 *Name:* `{filename[:35]}`\n"
            f"⚡ *Progress:* {bar}\n"
            f"📦 *Downloaded:* `{downloaded_mb:.1f} MB` / `{total_mb:.1f} MB`\n"
            f"🚀 *Speed:* `{speed_mb:.2f} MB/s`"
        )
        tracker.update(text)
    elif d["status"] == "finished":
        tracker.update("📥 *Download finished! Processing file... / در حال پردازش فایل*")

class ProgressTracker:
    def __init__(self, bot, chat_id, message_id, loop):
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        self.loop = loop
        self.last_update = 0
        self.throttle_interval = 3.0
        self.last_text = ""

    def update(self, text):
        now = time.time()
        if now - self.last_update < self.throttle_interval:
            return
        if text == self.last_text:
            return
        self.last_update = now
        self.last_text = text

        asyncio.run_coroutine_threadsafe(
            self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=text,
                parse_mode="Markdown",
            ),
            self.loop,
        )

# =====================================================================
# Downloading Engine (Resilient & Range Support)
# =====================================================================
async def download_direct_resilient(url, filepath, progress_message, bot, custom_name=None):
    """Downloads a file with HTTP Range support to auto-resume on network drops."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    downloaded = 0
    total_size = 0
    retries = 5
    
    async with aiohttp.ClientSession(headers=headers) as session:
        while retries > 0:
            try:
                if os.path.exists(filepath):
                    downloaded = os.path.getsize(filepath)
                
                # Request only remaining bytes if we have a partial download
                if downloaded > 0:
                    headers['Range'] = f"bytes={downloaded}-"
                else:
                    headers.pop('Range', None)
                
                async with session.get(url, allow_redirects=True) as response:
                    # 206 is Partial Content, 200 is Full Content
                    if response.status not in (200, 206):
                        # Reset download if Range request was rejected by server
                        downloaded = 0
                        headers.pop('Range', None)
                        async with session.get(url, allow_redirects=True) as retry_resp:
                            response = retry_resp
                    
                    if total_size == 0:
                        total_size = int(response.headers.get("Content-Length", 0)) + downloaded
                        
                        # Set Custom Naming
                        if downloaded == 0:
                            cd = response.headers.get("Content-Disposition")
                            if cd:
                                fname_match = re.findall(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\']+)["\']?', cd)
                                if fname_match:
                                    filename = unquote(fname_match[0])
                                    filepath = os.path.join(os.path.dirname(filepath), filename)
                                    
                            content_type = response.headers.get("Content-Type", "").split(";")[0]
                            if content_type:
                                ext = mimetypes.guess_extension(content_type)
                                if ext and not filepath.endswith(ext):
                                    filepath += ext
                            
                            if custom_name:
                                _, ext = os.path.splitext(filepath)
                                if not os.path.splitext(custom_name)[1] and ext:
                                    custom_name += ext
                                filepath = os.path.join(os.path.dirname(filepath), custom_name)

                    mode = "ab" if downloaded > 0 else "wb"
                    last_update = 0
                    
                    with open(filepath, mode) as f:
                        async for chunk in response.content.iter_chunked(512 * 1024):  # 512KB chunks
                            f.write(chunk)
                            downloaded += len(chunk)
                            
                            now = time.time()
                            if now - last_update > 3.0:
                                last_update = now
                                percent = (downloaded / total_size) * 100 if total_size > 0 else 0
                                total_str = f"{total_size / (1024*1024):.1f} MB" if total_size > 0 else "unknown"
                                downloaded_str = f"{downloaded / (1024*1024):.1f} MB"
                                bar_str = get_progress_bar(percent) if total_size > 0 else "`Downloading...`"
                                
                                text = (
                                    f"📥 *DOWNLOADING DIRECT FILE / دانلود لینک مستقیم* 📥\n"
                                    f"{DIVIDER}\n"
                                    f"📁 *Name:* `{os.path.basename(filepath)[:35]}`\n"
                                    f"⚡ *Progress:* {bar_str}\n"
                                    f"📦 *Size:* `{downloaded_str}` / `{total_str}`"
                                )
                                try:
                                    await progress_message.edit_text(text, parse_mode="Markdown")
                                except Exception:
                                    pass
                    break  # Success
            except Exception as e:
                retries -= 1
                logger.warning(f"Download failed: {e}. Retries remaining: {retries}")
                await asyncio.sleep(2)
                if retries == 0:
                    raise e
                    
    return filepath

def download_yt(url, dest_dir, format_opt, start_time, end_time, tracker):
    """Downloads YouTube/Social content via yt-dlp, supports trimming and subtitle extraction."""
    ydl_format = "best"
    postprocessors = []

    if format_opt == "audio":
        ydl_format = "bestaudio/best"
        postprocessors = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }] if shutil.which("ffmpeg") else []

    ydl_opts = {
        "outtmpl": os.path.join(dest_dir, "%(title)s.%(ext)s"),
        "format": ydl_format,
        "progress_hooks": [lambda d: yt_dlp_hook(d, tracker)],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "writesubtitles": True,
        "allsubtitles": False,
        "subtitleslangs": ["en", "fa"],  # Grab English/Persian subtitles if available
    }
    
    if postprocessors:
        ydl_opts["postprocessors"] = postprocessors

    # Efficient Range Trimming (Downloads only the specified segment instead of full file)
    if start_time is not None and end_time is not None:
        ydl_opts['download_ranges'] = lambda info, self: [{'start_time': start_time, 'end_time': end_time}]
        ydl_opts['force_keyframes_at_cuts'] = True

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        
        # Check if MP3 conversion occurred
        if format_opt == "audio" and not filename.endswith(".mp3"):
            base, _ = os.path.splitext(filename)
            if os.path.exists(base + ".mp3"):
                filename = base + ".mp3"

        return filename

# =====================================================================
# Video Processing & Splitting
# =====================================================================
def split_video_ffmpeg(filepath, dest_dir, max_part_size=MAX_PART_SIZE):
    """Segments a video file into playable chunks using FFmpeg."""
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        return None
    try:
        file_size = os.path.getsize(filepath)
        cmd = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", filepath
        ]
        duration = float(subprocess.check_output(cmd).decode().strip())
        bitrate = file_size / duration
        segment_duration = int((max_part_size * 0.9) / bitrate)
        
        if segment_duration <= 0 or segment_duration >= duration:
            return None
            
        base_name = os.path.basename(filepath)
        name_part, ext = os.path.splitext(base_name)
        output_template = os.path.join(dest_dir, f"{name_part}.part%03d{ext}")
        
        cmd_split = [
            "ffmpeg", "-y", "-i", filepath, "-c", "copy", "-map", "0",
            "-segment_time", str(segment_duration), "-f", "segment", output_template
        ]
        subprocess.run(cmd_split, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        parts = []
        for f in sorted(os.listdir(dest_dir)):
            if f.startswith(name_part + ".part") and f.endswith(ext):
                parts.append(os.path.join(dest_dir, f))
        return parts
    except Exception as e:
        logger.error(f"FFmpeg segmenting failed: {e}")
        return None

def split_file_binary(file_path, chunk_size):
    """Splits raw files into chunks."""
    parts = []
    base_name = os.path.basename(file_path)
    dir_name = os.path.dirname(file_path)
    part_num = 1
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            part_name = f"{base_name}.part{part_num:03d}"
            part_path = os.path.join(dir_name, part_name)
            with open(part_path, "wb") as pf:
                pf.write(chunk)
            parts.append(part_path)
            part_num += 1
    return parts

def convert_to_gif_ffmpeg(input_path, output_path):
    """Converts a short video into a high-quality looping GIF."""
    if not shutil.which("ffmpeg"):
        return False
    try:
        # High quality palette-based GIF conversion using FFmpeg
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-vf", "fps=10,scale=480:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
            "-loop", "0", output_path
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except Exception as e:
        logger.error(f"GIF conversion failed: {e}")
        return False

# =====================================================================
# Conversion Engine (Pillow, pypdf, docx2pdf, FFmpeg)
# =====================================================================
async def run_file_conversion(input_path, target_format, temp_dir):
    """Executes file format conversions based on target format."""
    from PIL import Image
    from pypdf import PdfReader
    
    base_name = os.path.basename(input_path)
    name_part, _ = os.path.splitext(base_name)
    
    if target_format in ("png", "jpg", "webp", "pdf"):
        # Image Conversion using Pillow
        output_ext = f".{target_format}"
        output_path = os.path.join(temp_dir, name_part + output_ext)
        
        loop = asyncio.get_running_loop()
        
        def process_image():
            with Image.open(input_path) as img:
                # RGB mode conversions for JPEG/PDF
                if target_format in ("jpg", "pdf") and img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                img.save(output_path)
                return output_path
                
        return await loop.run_in_executor(None, process_image)
        
    elif target_format in ("mp3", "wav", "ogg"):
        # Audio Conversion using FFmpeg
        output_ext = f".{target_format}"
        output_path = os.path.join(temp_dir, name_part + output_ext)
        
        if not shutil.which("ffmpeg"):
            raise Exception("FFmpeg is not installed on this server. Audio conversion is unavailable.")
            
        cmd = ["ffmpeg", "-y", "-i", input_path]
        
        if target_format == "ogg":
            # Convert to Vorbis for OGG audio compatibility
            cmd.extend(["-c:a", "libvorbis", "-q:a", "4"])
        elif target_format == "mp3":
            cmd.extend(["-codec:a", "libmp3lame", "-b:a", "192k"])
        elif target_format == "wav":
            cmd.extend(["-codec:a", "pcm_s16le"])
            
        cmd.append(output_path)
        
        loop = asyncio.get_running_loop()
        
        def process_audio():
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return output_path
            
        return await loop.run_in_executor(None, process_audio)
        
    elif target_format == "txt":
        # Extract Text from PDF using pypdf
        output_path = os.path.join(temp_dir, name_part + ".txt")
        
        loop = asyncio.get_running_loop()
        
        def process_pdf():
            reader = PdfReader(input_path)
            full_text = ""
            for idx, page in enumerate(reader.pages):
                page_text = page.extract_text() or ""
                full_text += f"--- Page {idx + 1} ---\n{page_text}\n\n"
                
            if not full_text.strip():
                raise Exception("No extractable text found in PDF (it might be scanned images).")
                
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(full_text)
            return output_path
            
        return await loop.run_in_executor(None, process_pdf)
        
    elif target_format == "docx2pdf":
        # Word to PDF conversion
        output_path = os.path.join(temp_dir, name_part + ".pdf")
        
        loop = asyncio.get_running_loop()
        
        def process_docx():
            if os.name == 'nt':
                # Windows (Microsoft Word COM)
                from docx2pdf import convert
                convert(input_path, output_path)
                return output_path
            else:
                # Linux (LibreOffice)
                if shutil.which("libreoffice") or shutil.which("soffice"):
                    cmd_lo = [
                        shutil.which("libreoffice") or shutil.which("soffice"),
                        "--headless", "--convert-to", "pdf",
                        "--outdir", temp_dir, input_path
                    ]
                    subprocess.run(cmd_lo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    lo_output = os.path.join(temp_dir, name_part + ".pdf")
                    if os.path.exists(lo_output):
                        return lo_output
                raise Exception("Word conversion failed. LibreOffice/MS Word is not installed on this server.")
                
        return await loop.run_in_executor(None, process_docx)
        
    return None

# =====================================================================
# Messaging & Telegram Upload Engine
# =====================================================================
async def send_file_to_telegram(bot, chat_id, filepath, reply_to_message_id, thumbnail_path=None, as_gif=False, user_id=None):
    """Sends documents, videos or GIFs to the user, handling auto-splitting."""
    file_size = os.path.getsize(filepath)
    filename = os.path.basename(filepath)
    video_extensions = [".mp4", ".mkv", ".mov", ".avi", ".webm"]
    is_video = any(filename.lower().endswith(ext) for ext in video_extensions)

    # Log Stat
    if user_id:
        db.log_download(user_id, "video" if is_video else "document", file_size)

    # GIF Conversion requested
    if as_gif and is_video:
        gif_path = filepath + ".gif"
        status_msg = await bot.send_message(chat_id=chat_id, text="🖼 *Converting to GIF... / در حال تبدیل به گیف*", parse_mode="Markdown")
        if convert_to_gif_ffmpeg(filepath, gif_path):
            await status_msg.edit_text("📤 *Uploading GIF... / در حال آپلود*", parse_mode="Markdown")
            with open(gif_path, "rb") as f:
                sent_msg = await bot.send_animation(
                    chat_id=chat_id,
                    animation=f,
                    reply_to_message_id=reply_to_message_id,
                )
                await setup_cloud_button(bot, chat_id, sent_msg.message_id)
            await status_msg.delete()
            return
        else:
            await status_msg.edit_text("❌ *GIF conversion failed (requires FFmpeg). Sending raw file...*")
            await asyncio.sleep(2)
            await status_msg.delete()

    if file_size <= MAX_PART_SIZE:
        status_msg = await bot.send_message(
            chat_id=chat_id,
            text=f"📤 *Uploading / در حال ارسال* `{filename}`...",
            reply_to_message_id=reply_to_message_id,
            parse_mode="Markdown",
        )
        try:
            thumb_file = open(thumbnail_path, "rb") if thumbnail_path and os.path.exists(thumbnail_path) else None
            sent_msg = None
            if is_video:
                try:
                    with open(filepath, "rb") as f:
                        sent_msg = await bot.send_video(
                            chat_id=chat_id,
                            video=f,
                            filename=filename,
                            thumbnail=thumb_file,
                            reply_to_message_id=reply_to_message_id,
                            supports_streaming=True
                        )
                except Exception:
                    # Fallback to document
                    if thumb_file:
                        thumb_file.close()
                        thumb_file = open(thumbnail_path, "rb") if thumbnail_path else None
                    with open(filepath, "rb") as f:
                        sent_msg = await bot.send_document(
                            chat_id=chat_id,
                            document=f,
                            filename=filename,
                            thumbnail=thumb_file,
                            reply_to_message_id=reply_to_message_id
                        )
            else:
                with open(filepath, "rb") as f:
                    sent_msg = await bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        filename=filename,
                        thumbnail=thumb_file,
                        reply_to_message_id=reply_to_message_id
                    )
            
            if thumb_file:
                thumb_file.close()
            await status_msg.delete()
            
            if sent_msg:
                await setup_cloud_button(bot, chat_id, sent_msg.message_id)
                
        except Exception as e:
            await status_msg.edit_text(f"❌ *Failed to upload:* {str(e)}")
            raise e
    else:
        # File is too large, split it
        status_msg = await bot.send_message(
            chat_id=chat_id,
            text=f"📦 *File size ({file_size / (1024*1024):.1f} MB) exceeds 50MB limit.*\nSplitting into parts...",
            reply_to_message_id=reply_to_message_id,
            parse_mode="Markdown",
        )

        parts = None
        used_ffmpeg = False
        if is_video:
            await status_msg.edit_text("🎬 *Splitting video into playable parts using FFmpeg...*", parse_mode="Markdown")
            parts = split_video_ffmpeg(filepath, os.path.dirname(filepath), MAX_PART_SIZE)
            if parts:
                used_ffmpeg = True

        if not parts:
            await status_msg.edit_text("📦 *Splitting file into binary parts...*", parse_mode="Markdown")
            parts = split_file_binary(filepath, MAX_PART_SIZE)
            
        total_parts = len(parts)
        await status_msg.edit_text(f"📦 *Split into {total_parts} parts.* Starting upload...", parse_mode="Markdown")

        try:
            for idx, part_path in enumerate(parts):
                part_name = os.path.basename(part_path)
                await status_msg.edit_text(
                    f"📤 *Uploading part {idx + 1} of {total_parts}:* `{part_name}`...",
                    parse_mode="Markdown",
                )
                thumb_file = open(thumbnail_path, "rb") if thumbnail_path and os.path.exists(thumbnail_path) else None
                
                with open(part_path, "rb") as f:
                    if is_video and used_ffmpeg:
                        await bot.send_video(
                            chat_id=chat_id,
                            video=f,
                            filename=part_name,
                            thumbnail=thumb_file,
                            reply_to_message_id=reply_to_message_id,
                            supports_streaming=True
                        )
                    else:
                        await bot.send_document(
                            chat_id=chat_id,
                            document=f,
                            filename=part_name,
                            thumbnail=thumb_file,
                            reply_to_message_id=reply_to_message_id,
                        )
                if thumb_file:
                    thumb_file.close()
                await asyncio.sleep(1)

            if used_ffmpeg:
                instructions = (
                    f"✅ *All {total_parts} parts uploaded successfully!*\n\n"
                    f"🎬 *Note:* Video segments are *fully playable* individually inside Telegram!"
                )
            else:
                merge_cmd_win = f'copy /b ' + " + ".join([f'"{os.path.basename(p)}"' for p in parts]) + f' "{filename}"'
                merge_cmd_unix = f'cat ' + " ".join([f'"{os.path.basename(p)}"' for p in parts]) + f' > "{filename}"'
                instructions = (
                    f"✅ *All {total_parts} parts uploaded successfully!*\n\n"
                    f"🔧 *How to merge them back into a single file:*\n\n"
                    f"💻 *Windows (CMD):*\n```cmd\n{merge_cmd_win}\n```\n\n"
                    f"🍎🐧 *macOS / Linux:*\n```bash\n{merge_cmd_unix}\n```\n\n"
                    f"💡 *Alternative:* Put all parts in the same directory and extract the first part (`.part001`) using WinRAR or 7-Zip."
                )
                
            await bot.send_message(
                chat_id=chat_id,
                text=instructions,
                reply_to_message_id=reply_to_message_id,
                parse_mode="Markdown",
            )
            await status_msg.delete()
        except Exception as e:
            await status_msg.edit_text(f"❌ *Error during split upload:* {str(e)}")
            raise e
        finally:
            for part_path in parts:
                if os.path.exists(part_path) and part_path != filepath:
                    try:
                        os.remove(part_path)
                    except Exception:
                        pass

async def setup_cloud_button(bot, chat_id, message_id):
    """Presents a 'Send to Cloud' archiving option if CLOUD_CHANNEL_ID is configured."""
    if not CLOUD_CHANNEL_ID:
        return
    keyboard = [[InlineKeyboardButton("☁️ Archive to Cloud Channel", callback_data=f"cloud:{message_id}")]]
    await bot.send_message(
        chat_id=chat_id,
        text="💾 *Cloud Archiving available / آرشیو ابری:*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# =====================================================================
# Commands
# =====================================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends welcome message and instructions with premium visual layout."""
    welcome_text = (
        f"🤖 *SENIOR DOWNLOADER & CONVERTER* \n"
        f"{DIVIDER}\n"
        f"Welcome to the ultimate media downloader and file converter bot. "
        f"Built for speed, reliability, and rich functionality.\n\n"
        f"💡 *FEATURES & USAGE / راهنما:*\n"
        f" ├─ *Media Links:* Send YouTube/Insta/TikTok links directly.\n"
        f" ├─ *Direct Links:* Send direct URLs (support `--name file.ext`).\n"
        f" ├─ *Search YouTube:* Use `/search <query>` to explore.\n"
        f" ├─ *Convert Formats:* Send any file, photo, or audio.\n"
        f" ├─ *Trimming:* Click '✂️ Trim' to download a video range.\n"
        f" ├─ *Playlists:* Download as multiple files or a single ZIP.\n"
        f" └─ *Usage Statistics:* View stats with `/stats`.\n\n"
        f"✨ *Start by sending a link or a file!*"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Searches YouTube and returns top 5 results with inline select buttons."""
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("❌ Please specify query: `/search coldplay`")
        return

    status_msg = await update.message.reply_text("🔍 *Searching YouTube... / در حال جستجو*", parse_mode="Markdown")
    loop = asyncio.get_running_loop()
    
    try:
        def run_search():
            ydl_opts = {
                'format': 'best',
                'quiet': True,
                'no_warnings': True,
                'noplaylist': True,
                'extract_flat': True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch5:{query}", download=False)
                return info.get('entries', [])

        entries = await loop.run_in_executor(None, run_search)
        if not entries:
            await status_msg.edit_text("❌ No results found.")
            return

        text = f"🔍 *YOUTUBE SEARCH / جستجوی یوتیوب*\n{DIVIDER}\nQuery: `{query}`\n\n"
        keyboard = []
        
        for idx, entry in enumerate(entries):
            title = entry.get('title', 'Video')
            url = entry.get('url')
            text += f"{idx + 1}️⃣ *{title[:50]}...*\n      └─ 🔗 {url}\n\n"
            
            url_id = uuid.uuid4().hex[:8]
            URL_CACHE[url_id] = {
                'url': url,
                'title': title,
                'thumbnail': entry.get('thumbnail'),
                'duration': entry.get('duration', 0)
            }
            # Balanced 2-buttons per row layout
            if idx % 2 == 0:
                keyboard.append([InlineKeyboardButton(f"📥 Download #{idx + 1}", callback_data=f"opt:{url_id}")])
            else:
                keyboard[-1].append(InlineKeyboardButton(f"📥 Download #{idx + 1}", callback_data=f"opt:{url_id}"))

        await status_msg.delete()
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Search failed: {e}")
        await status_msg.edit_text(f"❌ *Search failed:* `{str(e)[:150]}`")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays personal statistics, or global statistics if requested by Admin."""
    user_id = update.effective_user.id
    count, total_size = db.get_user_stats(user_id)
    size_mb = total_size / (1024 * 1024)
    
    text = (
        f"📊 *STATISTICS / آمار مصرف* 📊\n"
        f"{DIVIDER}\n"
        f"👤 *Your Usage / مصرف شما:*\n"
        f" ├─ *Files Processed:* `{count}`\n"
        f" └─ *Bandwidth Used:* `{size_mb:.2f} MB`"
    )
    
    # Check if user is Admin and requested global stats
    if ADMIN_USER_ID and str(user_id) == str(ADMIN_USER_ID):
        g_count, g_size, g_users = db.get_global_stats()
        g_size_gb = g_size / (1024 * 1024 * 1024)
        text += (
            f"\n\n⚙️ *Global System Admin stats / آمار کل سیستم:*\n"
            f" ├─ *Active Userbase:* `{g_users}` users\n"
            f" ├─ *Global Files:* `{g_count}`\n"
            f" └─ *Global Bandwidth:* `{g_size_gb:.2f} GB`"
        )
        
    await update.message.reply_text(text, parse_mode="Markdown")

# =====================================================================
# Main Message Handler (Inputs & Routing)
# =====================================================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    user_id = update.effective_user.id
    text = message.text.strip()

    # Trimming State Check
    if user_id in USER_STATES and USER_STATES[user_id]['state'] == 'AWAITING_TRIM':
        url_id = USER_STATES[user_id]['url_id']
        cached = URL_CACHE.get(url_id)
        
        if not cached:
            await message.reply_text("❌ Trimming session expired. Please send link again.")
            USER_STATES.pop(user_id, None)
            return

        # Parse range (e.g. 01:00 - 02:30 or 60 - 150)
        range_match = re.match(r'(\d+(?::\d+){0,2})\s*[-–—]\s*(\d+(?::\d+){0,2})', text)
        if not range_match:
            await message.reply_text("❌ Invalid format. Please reply with start and end times: `MM:SS - MM:SS`")
            return
            
        start_sec = parse_time_str(range_match.group(1))
        end_sec = parse_time_str(range_match.group(2))
        
        if start_sec is None or end_sec is None or start_sec >= end_sec:
            await message.reply_text("❌ Invalid times. Verify start is less than end.")
            return

        # Apply trim bounds
        cached['start_time'] = start_sec
        cached['end_time'] = end_sec
        USER_STATES.pop(user_id, None)  # Reset state

        # Redisplay Keyboard with active trim values
        duration_str = f"{format_seconds(start_sec)} to {format_seconds(end_sec)} ({end_sec - start_sec}s)"
        keyboard = [
            [
                InlineKeyboardButton("🎥 Video Segment", callback_data=f"dl:{url_id}:video"),
                InlineKeyboardButton("🎵 Audio (MP3) Segment", callback_data=f"dl:{url_id}:audio")
            ],
            [InlineKeyboardButton("❌ Cancel (لغو)", callback_data=f"dl:{url_id}:cancel")]
        ]
        
        # Add GIF option if duration <= 15s
        if (end_sec - start_sec) <= 15:
            keyboard[0].append(InlineKeyboardButton("🖼 Convert to GIF", callback_data=f"dl:{url_id}:gif"))

        await message.reply_text(
            f"✂️ *TRIM WINDOW SELECTED / بازه زمانی انتخاب شده:*\n`{duration_str}`\n🎥 *Media:* `{cached['title']}`\n\n*Choose format:*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    # Parse Custom Rename Command
    custom_name = None
    name_match = re.search(r'--name\s+(\S+)', text)
    if name_match:
        custom_name = name_match.group(1)
        text = re.sub(r'--name\s+\S+', '', text).strip()

    # Extract URL
    urls = []
    for entity in message.entities or []:
        if entity.type == "url":
            urls.append(message.text[entity.offset : entity.offset + entity.length])
        elif entity.type == "text_link":
            urls.append(entity.url)

    if not urls:
        url_match = re.search(r"https?://\S+", text)
        if url_match:
            urls.append(url_match.group(0))

    if not urls:
        await message.reply_text("❌ Please send a valid link, a file to convert, or use `/search <query>`.")
        return

    raw_url = urls[0]
    # Bypass redirects (Link Shorteners)
    status_msg = await message.reply_text("🔍 *Resolving link redirects... / در حال بررسی لینک*", parse_mode="Markdown")
    url = await bypass_url(raw_url)

    # Simple domain checks
    social_domains = [
        "youtube.com", "youtu.be", "instagram.com", "tiktok.com",
        "twitter.com", "x.com", "facebook.com", "fb.watch", "vimeo.com"
    ]
    is_social = any(domain in url.lower() for domain in social_domains)

    if is_social:
        await status_msg.edit_text("🔍 *Extracting media info... / دریافت اطلاعات*", parse_mode="Markdown")
        loop = asyncio.get_running_loop()
        
        try:
            def extract_metadata():
                ydl_opts = {'quiet': True, 'no_warnings': True}
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    return ydl.extract_info(url, download=False)
            
            info = await loop.run_in_executor(None, extract_metadata)
            
            url_id = uuid.uuid4().hex[:8]
            URL_CACHE[url_id] = {
                'url': url,
                'title': info.get('title', 'Video'),
                'thumbnail': info.get('thumbnail'),
                'duration': info.get('duration', 0),
                'start_time': None,
                'end_time': None
            }
            
            # Detect Playlist or Playlist Album
            if 'entries' in info and not url.lower().startswith("ytsearch"):
                # Handle Playlist
                entries = info['entries']
                keyboard = [
                    [
                        InlineKeyboardButton("🎥 One-by-One (تکی)", callback_data=f"plist:{url_id}:each"),
                        InlineKeyboardButton("📦 Download as ZIP (زیپ)", callback_data=f"plist:{url_id}:zip")
                    ],
                    [InlineKeyboardButton("❌ Cancel (لغو)", callback_data=f"dl:{url_id}:cancel")]
                ]
                await status_msg.delete()
                await message.reply_text(
                    f"📦 *PLAYLIST DETECTED / پلی‌لیست* 📦\n"
                    f"{DIVIDER}\n"
                    f"📚 *Title:* `{info.get('title', 'Album')}`\n"
                    f"🔢 *Count:* `{len(entries)}` videos\n\n"
                    f"👇 *Choose strategy:* / یکی از گزینه‌ها را انتخاب کنید:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown"
                )
                return

            # Handle Single Video / Audio
            duration = info.get('duration', 0)
            duration_str = f"{format_seconds(duration)}" if duration else "unknown"
            
            keyboard = [
                [
                    InlineKeyboardButton("🎥 Video (ویدیو)", callback_data=f"dl:{url_id}:video"),
                    InlineKeyboardButton("🎵 MP3 Audio (صدا)", callback_data=f"dl:{url_id}:audio")
                ],
                [
                    InlineKeyboardButton("✂️ Trim Range (برش)", callback_data=f"dl:{url_id}:trim"),
                ],
                [InlineKeyboardButton("❌ Cancel (لغو)", callback_data=f"dl:{url_id}:cancel")]
            ]
            
            # Add GIF option directly if video <= 15s
            if duration and duration <= 15:
                keyboard[1].append(InlineKeyboardButton("🖼 Convert to GIF", callback_data=f"dl:{url_id}:gif"))
            
            await status_msg.delete()
            await message.reply_text(
                f"✨ *MEDIA METADATA / اطلاعات رسانه* ✨\n"
                f"{DIVIDER}\n"
                f"📝 *Title:* `{info.get('title', 'Social Media File')}`\n"
                f"⏱ *Duration:* `{duration_str}`\n\n"
                f"👇 *Choose action:* / یکی از گزینه‌ها را انتخاب کنید:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Metadata extraction failed: {e}")
            await status_msg.edit_text(f"❌ *Failed to extract details:* `{str(e)[:150]}`. Trying to download directly...", parse_mode="Markdown")
            await add_to_queue(url, message, None, custom_name, user_id=user_id)
    else:
        # Direct link download
        await status_msg.delete()
        await add_to_queue(url, message, None, custom_name, user_id=user_id)

# =====================================================================
# File Receiver & Processing Handler (File Converter Entrypoint)
# =====================================================================
async def handle_incoming_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches document, photo, or audio attachments and offers conversion choices."""
    message = update.message
    if not message:
        return

    # Identify file details
    file_id = None
    filename = None
    file_type = None

    if message.document:
        file_id = message.document.file_id
        filename = message.document.file_name or f"document_{message.document.file_id[:8]}"
        file_type = "document"
    elif message.photo:
        # Choose the highest resolution photo
        photo = message.photo[-1]
        file_id = photo.file_id
        filename = f"photo_{photo.file_id[:8]}.jpg"
        file_type = "image"
    elif message.audio:
        file_id = message.audio.file_id
        filename = message.audio.file_name or f"audio_{message.audio.file_id[:8]}.mp3"
        file_type = "audio"
    elif message.voice:
        file_id = message.voice.file_id
        filename = f"voice_{message.voice.file_id[:8]}.ogg"
        file_type = "audio"

    if not file_id:
        return

    # Classify file type based on extension
    _, ext = os.path.splitext(filename.lower())
    image_exts = [".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"]
    audio_exts = [".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".wma"]

    if ext in image_exts:
        file_type = "image"
    elif ext in audio_exts:
        file_type = "audio"
    elif ext == ".pdf":
        file_type = "pdf"
    elif ext == ".docx":
        file_type = "docx"

    # Save details to Cache
    file_uuid = uuid.uuid4().hex[:8]
    URL_CACHE[file_uuid] = {
        "file_id": file_id,
        "filename": filename,
        "file_type": file_type,
        "ext": ext,
    }

    # Generate keyboard options
    keyboard = []
    if file_type == "image":
        keyboard = [
            [
                InlineKeyboardButton("🖼 To PNG", callback_data=f"conv:{file_uuid}:png"),
                InlineKeyboardButton("🖼 To JPG", callback_data=f"conv:{file_uuid}:jpg"),
            ],
            [
                InlineKeyboardButton("🖼 To WebP", callback_data=f"conv:{file_uuid}:webp"),
                InlineKeyboardButton("📄 To PDF", callback_data=f"conv:{file_uuid}:pdf"),
            ],
        ]
    elif file_type == "audio":
        keyboard = [
            [
                InlineKeyboardButton("🎵 To MP3", callback_data=f"conv:{file_uuid}:mp3"),
                InlineKeyboardButton("🎵 To WAV", callback_data=f"conv:{file_uuid}:wav"),
            ],
            [InlineKeyboardButton("🗣 To OGG Audio", callback_data=f"conv:{file_uuid}:ogg")],
        ]
    elif file_type == "pdf":
        keyboard = [
            [InlineKeyboardButton("📝 Extract Text (PDF to TXT)", callback_data=f"conv:{file_uuid}:txt")]
        ]
    elif file_type == "docx":
        keyboard = [
            [InlineKeyboardButton("📄 Convert to PDF (Word -> PDF)", callback_data=f"conv:{file_uuid}:docx2pdf")]
        ]

    if not keyboard:
        # Unsupported format, send default response
        await message.reply_text(
            f"📥 *File Received:* `{filename}`\n\n❌ Format conversions are not supported for this file type.",
            parse_mode="Markdown",
        )
        return

    keyboard.append([InlineKeyboardButton("❌ Cancel (لغو)", callback_data=f"conv:{file_uuid}:cancel")])

    await message.reply_text(
        f"🔄 *FILE CONVERTER / مبدل فرمت فایل* 🔄\n"
        f"{DIVIDER}\n"
        f"📁 *File Name:* `{filename}`\n"
        f"📦 *Type:* `{file_type.upper()}`\n\n"
        f"👇 *Select target format:* / فرمت مورد نظر را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )

# =====================================================================
# Callbacks Handler (User Decisions)
# =====================================================================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    data = query.data.split(":")
    action = data[0]

    # Handle Search Options Choice
    if action == "opt":
        url_id = data[1]
        cached = URL_CACHE.get(url_id)
        if not cached:
            await query.edit_message_text("❌ Session expired. Please search again.")
            return

        keyboard = [
            [
                InlineKeyboardButton("🎥 Video (ویدیو)", callback_data=f"dl:{url_id}:video"),
                InlineKeyboardButton("🎵 MP3 Audio (صدا)", callback_data=f"dl:{url_id}:audio")
            ],
            [
                InlineKeyboardButton("✂️ Trim Range (برش)", callback_data=f"dl:{url_id}:trim"),
            ],
            [InlineKeyboardButton("❌ Cancel (لغو)", callback_data=f"dl:{url_id}:cancel")]
        ]
        if cached.get('duration') and cached['duration'] <= 15:
            keyboard[1].append(InlineKeyboardButton("🖼 Convert to GIF", callback_data=f"dl:{url_id}:gif"))

        duration_str = f"{format_seconds(cached['duration'])}" if cached.get('duration') else "unknown"
        await query.edit_message_text(
            f"🎥 *Media:* `{cached['title']}`\n⏱ *Duration:* `{duration_str}`\n\n*Choose download format:*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    # Handle Playlist Options Choice
    if action == "plist":
        url_id = data[1]
        strategy = data[2]
        cached = URL_CACHE.get(url_id)
        if not cached:
            await query.edit_message_text("❌ Playlist session expired.")
            return
            
        await query.edit_message_text("⏳ *Adding playlist download tasks to queue...*", parse_mode="Markdown")
        
        # Enqueue Playlist Task
        await add_playlist_to_queue(cached['url'], query.message, strategy, user_id)
        URL_CACHE.pop(url_id, None)
        return

    # Handle Direct Downloads / Cancellations / Trims
    if action == "dl":
        url_id = data[1]
        choice = data[2]
        cached = URL_CACHE.get(url_id)

        if not cached:
            await query.edit_message_text("❌ Session expired. Send link again.")
            return

        if choice == "cancel":
            await query.delete_message()
            URL_CACHE.pop(url_id, None)
            return

        if choice == "trim":
            # Set User State to Awaiting Trim Range input
            USER_STATES[user_id] = {
                'state': 'AWAITING_TRIM',
                'url_id': url_id
            }
            await query.edit_message_text(
                "✂️ *Trimming Video / برش ویدیو*\n\n"
                "Please reply to this message with the start and end times in `MM:SS` or `HH:MM:SS` format.\n"
                "Example: `01:30 - 02:15` or `00:00 - 00:45`.",
                parse_mode="Markdown"
            )
            return

        # Setup download parameters
        url = cached['url']
        start_t = cached.get('start_time')
        end_t = cached.get('end_time')
        as_gif = (choice == "gif")
        
        await query.edit_message_text("⏳ *Adding to download queue... / اضافه شدن به صف*", parse_mode="Markdown")
        await add_to_queue(
            url=url,
            message_to_reply=query.message,
            format_opt="audio" if choice == "audio" else "video",
            custom_name=None,
            start_time=start_t,
            end_time=end_t,
            as_gif=as_gif,
            cached_title=cached['title'],
            cached_thumb=cached['thumbnail'],
            user_id=user_id
        )
        URL_CACHE.pop(url_id, None)

    # Cloud Archiving Request
    if action == "cloud":
        target_msg_id = int(data[1])
        try:
            # Forward the message to Cloud Channel
            await context.bot.forward_message(
                chat_id=CLOUD_CHANNEL_ID,
                from_chat_id=query.message.chat_id,
                message_id=target_msg_id
            )
            await query.edit_message_text("☁️ *File forwarded successfully to Cloud storage channel!*", parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Cloud archiving failed: {e}")
            await query.edit_message_text(f"❌ *Failed to archive to Cloud:* `{str(e)[:100]}`", parse_mode="Markdown")

    # File Conversion Callback Choice
    if action == "conv":
        file_uuid = data[1]
        target_format = data[2]
        cached = URL_CACHE.get(file_uuid)

        if not cached:
            await query.edit_message_text("❌ Conversion session expired.")
            return

        if target_format == "cancel":
            await query.delete_message()
            URL_CACHE.pop(file_uuid, None)
            return

        file_id = cached["file_id"]
        filename = cached["filename"]

        await query.edit_message_text("⏳ *Adding conversion task to queue...*", parse_mode="Markdown")
        await add_conversion_to_queue(file_id, filename, target_format, query.message, user_id)
        URL_CACHE.pop(file_uuid, None)

# =====================================================================
# Queue Execution System
# =====================================================================
async def add_to_queue(url, message_to_reply, format_opt, custom_name, start_time=None, end_time=None, as_gif=False, cached_title=None, cached_thumb=None, user_id=None):
    """Enqueues single download task."""
    chat_id = message_to_reply.chat.id
    status_msg = await message_to_reply.reply_text("⏳ *Calculating position in queue...*", parse_mode="Markdown")

    async def download_task():
        try:
            await status_msg.edit_text("🚀 *Task started! Processing...*", parse_mode="Markdown")
            with tempfile.TemporaryDirectory() as temp_dir:
                loop = asyncio.get_running_loop()
                filepath = None
                thumbnail_path = None
                
                # Fetch thumbnail in background
                if cached_thumb:
                    thumbnail_path = await download_thumbnail(cached_thumb, temp_dir)

                if format_opt:
                    # yt-dlp Video / Audio download
                    await status_msg.edit_text("📥 *Downloading media... / در حال دانلود*", parse_mode="Markdown")
                    tracker = ProgressTracker(context_bot_wrapper(status_msg), chat_id, status_msg.message_id, loop)
                    filepath = await loop.run_in_executor(
                        None, lambda: download_yt(url, temp_dir, format_opt, start_time, end_time, tracker)
                    )
                else:
                    # Resilient Direct HTTP chunk downloader
                    await status_msg.edit_text("📥 *Downloading direct file...*", parse_mode="Markdown")
                    filepath = await download_direct_resilient(url, filepath=os.path.join(temp_dir, "file"), progress_message=status_msg, bot=status_msg.get_bot(), custom_name=custom_name)

                if filepath and os.path.exists(filepath):
                    # Send media
                    await send_file_to_telegram(
                        bot=status_msg.get_bot(),
                        chat_id=chat_id,
                        filepath=filepath,
                        reply_to_message_id=message_to_reply.message_id,
                        thumbnail_path=thumbnail_path,
                        as_gif=as_gif,
                        user_id=user_id
                    )
                    await status_msg.delete()
                else:
                    await status_msg.edit_text("❌ *Download completed but file was not found locally.*")
        except Exception as e:
            logger.error(f"Task failed: {e}", exc_info=True)
            await status_msg.edit_text(f"❌ *Failed download task:* `{str(e)[:200]}`", parse_mode="Markdown")

    pos = download_queue.qsize() + 1
    if pos > 1:
        await status_msg.edit_text(f"⏳ *Queue Position:* #{pos}\nOther tasks are currently executing. Please wait...")
    await download_queue.put(download_task)

async def add_playlist_to_queue(url, message_to_reply, strategy, user_id):
    """Enqueues playlist download tasks."""
    chat_id = message_to_reply.chat.id
    status_msg = await message_to_reply.reply_text("⏳ *Reading playlist metadata...*", parse_mode="Markdown")

    async def playlist_task():
        try:
            await status_msg.edit_text("🚀 *Processing playlist...*", parse_mode="Markdown")
            loop = asyncio.get_running_loop()
            
            # Fetch playlist entries
            def get_playlist_entries():
                ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True}
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    return ydl.extract_info(url, download=False)
            
            playlist_info = await loop.run_in_executor(None, get_playlist_entries)
            entries = playlist_info.get('entries', [])
            
            if not entries:
                await status_msg.edit_text("❌ Playlist has no entries or could not be parsed.")
                return
                
            if strategy == "zip":
                await status_msg.edit_text(f"📦 *Downloading all {len(entries)} items to pack in a ZIP archive...*", parse_mode="Markdown")
                with tempfile.TemporaryDirectory() as temp_dir:
                    downloaded_files = []
                    # Download each sequentially inside this task
                    for idx, entry in enumerate(entries):
                        entry_url = entry.get('url')
                        if not entry_url:
                            continue
                        await status_msg.edit_text(f"📥 *[ZIP Build] Downloading {idx + 1}/{len(entries)}:* `{entry.get('title')[:30]}...`", parse_mode="Markdown")
                        try:
                            tracker = ProgressTracker(context_bot_wrapper(status_msg), chat_id, status_msg.message_id, loop)
                            # Sync wrapper download
                            fn = await loop.run_in_executor(
                                None, lambda: download_yt(entry_url, temp_dir, "video", None, None, tracker)
                            )
                            if fn and os.path.exists(fn):
                                downloaded_files.append(fn)
                        except Exception as entry_err:
                            logger.error(f"Error downloading playlist item: {entry_err}")

                    if not downloaded_files:
                        await status_msg.edit_text("❌ No playlist files were successfully downloaded.")
                        return

                    # Create ZIP file
                    zip_path = os.path.join(temp_dir, f"{playlist_info.get('title', 'playlist')}.zip")
                    await status_msg.edit_text("📦 *Building ZIP archive file...*", parse_mode="Markdown")
                    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
                        for f in downloaded_files:
                            z.write(f, os.path.basename(f))
                    
                    await status_msg.edit_text("📤 *Uploading ZIP archive...*", parse_mode="Markdown")
                    await send_file_to_telegram(
                        bot=status_msg.get_bot(),
                        chat_id=chat_id,
                        filepath=zip_path,
                        reply_to_message_id=message_to_reply.message_id,
                        user_id=user_id
                    )
                    await status_msg.delete()
            else:
                # strategy == "each" -> download and send one by one
                await status_msg.edit_text(f"➕ *Adding {len(entries)} individual playlist tasks to queue...*", parse_mode="Markdown")
                for entry in entries:
                    entry_url = entry.get('url')
                    if entry_url:
                        # Append tasks to queue
                        await add_to_queue(
                            url=entry_url,
                            message_to_reply=message_to_reply,
                            format_opt="video",
                            custom_name=None,
                            cached_title=entry.get('title'),
                            cached_thumb=entry.get('thumbnail'),
                            user_id=user_id
                        )
                await status_msg.edit_text(f"✅ *Added {len(entries)} downloads to the queue!*")
                await asyncio.sleep(2)
                await status_msg.delete()
                
        except Exception as e:
            logger.error(f"Playlist extraction failed: {e}", exc_info=True)
            await status_msg.edit_text(f"❌ *Playlist processing failed:* `{str(e)[:150]}`", parse_mode="Markdown")

    await download_queue.put(playlist_task)

async def add_conversion_to_queue(file_id, filename, target_format, message_to_reply, user_id):
    """Enqueues conversion tasks to the background sequential queue."""
    chat_id = message_to_reply.chat.id
    status_msg = await message_to_reply.reply_text("⏳ *Calculating position in queue...*", parse_mode="Markdown")
    
    async def conversion_task():
        try:
            await status_msg.edit_text("🚀 *Conversion started...*", parse_mode="Markdown")
            with tempfile.TemporaryDirectory() as temp_dir:
                bot = status_msg.get_bot()
                
                # Fetch file
                await status_msg.edit_text("📥 *Downloading file from Telegram...*", parse_mode="Markdown")
                tg_file = await bot.get_file(file_id)
                input_filepath = os.path.join(temp_dir, filename)
                await tg_file.download_to_drive(input_filepath)
                
                # Convert
                await status_msg.edit_text(f"🔄 *Converting to {target_format.upper()}...*", parse_mode="Markdown")
                output_filepath = await run_file_conversion(input_filepath, target_format, temp_dir)
                
                if output_filepath and os.path.exists(output_filepath):
                    await status_msg.edit_text("📤 *Uploading converted file...*", parse_mode="Markdown")
                    file_size = os.path.getsize(output_filepath)
                    db.log_download(user_id, f"convert_{target_format}", file_size)
                    
                    reply_id = message_to_reply.reply_to_message.message_id if message_to_reply.reply_to_message else None
                    with open(output_filepath, "rb") as f:
                        await bot.send_document(
                            chat_id=chat_id,
                            document=f,
                            filename=os.path.basename(output_filepath),
                            reply_to_message_id=reply_id
                        )
                    await status_msg.delete()
                else:
                    await status_msg.edit_text("❌ *Conversion failed.* Could not build output file.")
        except Exception as e:
            logger.error(f"Conversion failed: {e}", exc_info=True)
            await status_msg.edit_text(f"❌ *Conversion failed:* `{str(e)[:150]}`", parse_mode="Markdown")

    pos = download_queue.qsize() + 1
    if pos > 1:
        await status_msg.edit_text(f"⏳ *Queue Position:* #{pos}\nWaiting for other tasks to complete...")
    await download_queue.put(conversion_task)

class context_bot_wrapper:
    def __init__(self, message_obj):
        self.message = message_obj
    async def edit_message_text(self, chat_id, message_id, text, parse_mode=None):
        return await self.message.edit_text(text, parse_mode=parse_mode)

async def queue_worker(bot):
    """Processes downloads sequentially from queue."""
    while True:
        task = await download_queue.get()
        try:
            await task()
        except Exception as e:
            logger.error(f"Error executing queue task: {e}")
        finally:
            download_queue.task_done()

# =====================================================================
# Main runner (Standard Polling & Async Web Server Mode)
# =====================================================================
async def start_web_server():
    """Starts a minimal web server for Render to keep the bot alive."""
    from aiohttp import web
    
    async def handle_ping(request):
        return web.Response(text="Bot is running!")

    app = web.Application()
    app.router.add_get("/", handle_ping)
    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Web server started on port {port}")

async def main_async(application):
    await start_web_server()
    # Start background queue worker
    asyncio.create_task(queue_worker(application.bot))
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    logger.info("Bot is polling in the background...")
    while True:
        await asyncio.sleep(3600)

async def local_main_async(application):
    asyncio.create_task(queue_worker(application.bot))
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    while True:
        await asyncio.sleep(3600)

def main():
    if not BOT_TOKEN or "YOUR_TELEGRAM_BOT_TOKEN_HERE" in BOT_TOKEN:
        print("❌ Error: TELEGRAM_BOT_TOKEN is not set in .env file!")
        print("Please edit c:/Users/Admin/Desktop/telegram-downloader-bot/.env and put your bot token.")
        return

    logger.info("Starting Telegram Bot...")
    application = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CallbackQueryHandler(handle_callback))
    
    # Catch textual requests (including URLs)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Catch incoming file attachments (images, PDFs, documents, audio streams)
    application.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO | filters.AUDIO | filters.VOICE, handle_incoming_file))

    if os.getenv("PORT"):
        logger.info("PORT environment variable detected. Running in cloud mode with Web Server...")
        try:
            asyncio.run(main_async(application))
        except (KeyboardInterrupt, SystemExit):
            pass
    else:
        logger.info("Running in standard polling mode with queue...")
        try:
            asyncio.run(local_main_async(application))
        except (KeyboardInterrupt, SystemExit):
            pass

if __name__ == "__main__":
    main()
