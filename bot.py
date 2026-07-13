import os
import re
import sys
import time
import uuid
import json
import shutil

try:
    import imageio_ffmpeg
    FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
    # ffprobe is in the same directory as ffmpeg in imageio_ffmpeg
    _ffmpeg_dir = os.path.dirname(FFMPEG_EXE)
    FFPROBE_EXE = os.path.join(_ffmpeg_dir, "ffprobe")
    if not os.path.exists(FFPROBE_EXE):
        FFPROBE_EXE = shutil.which("ffprobe") or "ffprobe"
except ImportError:
    FFMPEG_EXE = shutil.which("ffmpeg") or "ffmpeg"
    FFPROBE_EXE = shutil.which("ffprobe") or "ffprobe"

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

# Force stdout/stderr to UTF-8 encoding (especially on Windows)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

# Proxy Check & Configuration
def check_proxy_port(host="127.0.0.1", port=10808, timeout=1.0) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False

def get_proxy_url() -> str | None:
    # Check V2Ray/Nekoray SOCKS proxy (common ports)
    for port in [10808, 10809, 1080, 7890, 8080, 10800]:
        if check_proxy_port("127.0.0.1", port):
            return f"http://127.0.0.1:{port}"
    # Check environment proxy
    env_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("http_proxy") or os.environ.get("https_proxy")
    if env_proxy and env_proxy.strip():
        return env_proxy.strip()
    return None

# Configure V2Ray/Nekoray proxy globally if active
proxy_url = get_proxy_url()
if proxy_url:
    PROXY_URL = proxy_url
    os.environ['http_proxy'] = PROXY_URL
    os.environ['https_proxy'] = PROXY_URL
    os.environ['all_proxy'] = PROXY_URL
    os.environ['HTTP_PROXY'] = PROXY_URL
    os.environ['HTTPS_PROXY'] = PROXY_URL
    os.environ['ALL_PROXY'] = PROXY_URL
    os.environ['NO_PROXY'] = 'localhost,127.0.0.1'
    os.environ['no_proxy'] = 'localhost,127.0.0.1'
else:
    # No proxy detected — clear any stale proxy env vars that would break direct connections
    for var in ['http_proxy', 'https_proxy', 'all_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY']:
        os.environ.pop(var, None)
    os.environ['NO_PROXY'] = '*'
    os.environ['no_proxy'] = '*'

# Check if curl_cffi is installed to support browser impersonation targets
try:
    import curl_cffi
    _HAS_IMPERSONATE = True
except ImportError:
    _HAS_IMPERSONATE = False



# ---------------------------------------------------------------------------
# Auto-update yt-dlp on startup
# Render free tier ships a stale image; YouTube breaks every few weeks
# ---------------------------------------------------------------------------
def _auto_update_ytdlp():
    """Silently upgrades yt-dlp at bot startup so extractors stay fresh."""
    try:
        proxy_url = get_proxy_url()
        cmd = [sys.executable, "-m", "pip", "install", "-q"]
        if proxy_url:
            cmd += ["--proxy", proxy_url]
        cmd += ["--upgrade", "yt-dlp"]
        
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            # Fallback to --user if system-wide package is read-only (e.g. Docker/Render/Heroku)
            cmd_user = [sys.executable, "-m", "pip", "install", "-q"]
            if proxy_url:
                cmd_user += ["--proxy", proxy_url]
            cmd_user += ["--user", "--upgrade", "yt-dlp"]
            result = subprocess.run(
                cmd_user,
                capture_output=True, text=True, timeout=60
            )
        if result.returncode == 0:
            import importlib
            importlib.reload(yt_dlp)
            print("✅ yt-dlp auto-updated successfully")
        else:
            print(f"⚠️ yt-dlp update failed: {result.stderr.strip()[:200]}")
    except Exception as e:
        print(f"⚠️ yt-dlp auto-update skipped: {e}")

_auto_update_ytdlp()
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
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
MAX_PART_SIZE = 48 * 1024 * 1024  # 48MB hard limit (Telegram rejects at 50MB)
DIVIDER = "══════════════════════════"
THIN_DIVIDER = "────────────────────────────"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIES_FILE = os.getenv("COOKIES_FILE", os.path.join(BASE_DIR, "cookies.txt"))  # Path to cookies.txt for bot detection bypass
COOKIES_PH_FILE = os.getenv("COOKIES_PH_FILE", os.path.join(BASE_DIR, "cookiesph.txt"))  # Path to cookiesph.txt for Pornhub

# Initialize cookies.txt from environment variable (supports both raw text and base64)
cookies_content = os.getenv("YOUTUBE_COOKIES_CONTENT")
if cookies_content:
    try:
        import base64
        try:
            # Attempt to decode as base64 in case Render UI stripped newlines of raw text
            decoded = base64.b64decode(cookies_content.strip()).decode("utf-8")
            if "# Netscape" in decoded or "\t" in decoded:
                cookies_content = decoded
                logger.info("Successfully decoded YouTube cookies from base64 format")
        except Exception:
            pass

        with open(COOKIES_FILE, "w", encoding="utf-8") as f:
            f.write(cookies_content)
        logger.info(f"Initialized YouTube cookies file from YOUTUBE_COOKIES_CONTENT environment variable ({len(cookies_content)} bytes)")
    except Exception as e:
        logger.error(f"Failed to write YouTube cookies file from YOUTUBE_COOKIES_CONTENT: {e}")

# Initialize cookiesph.txt from environment variable
cookies_ph_content = os.getenv("PORNHUB_COOKIES_CONTENT")
if cookies_ph_content:
    try:
        import base64
        try:
            decoded = base64.b64decode(cookies_ph_content.strip()).decode("utf-8")
            if "# Netscape" in decoded or "\t" in decoded:
                cookies_ph_content = decoded
                logger.info("Successfully decoded Pornhub cookies from base64 format")
        except Exception:
            pass

        with open(COOKIES_PH_FILE, "w", encoding="utf-8") as f:
            f.write(cookies_ph_content)
        logger.info(f"Initialized Pornhub cookies file from PORNHUB_COOKIES_CONTENT environment variable ({len(cookies_ph_content)} bytes)")
    except Exception as e:
        logger.error(f"Failed to write Pornhub cookies file from PORNHUB_COOKIES_CONTENT: {e}")

# State Cache & Databases
URL_CACHE = {}          # Store media details (uuid -> data)
USER_STATES = {}        # User states (user_id -> {'state': '...', 'url_id': '...'})
download_queue: asyncio.Queue = asyncio.Queue()  # Global sequential task queue
MAX_CONCURRENT_DOWNLOADS = 3  # Allow up to 3 parallel downloads

def get_ydl_cookie_opts(url: str | None = None) -> dict:
    """Returns yt-dlp cookie options. YouTube uses android_vr client (no cookies needed).
    Cookies are only passed for adult sites (PornHub, etc.) that require auth."""
    opts = {}

    # IMPORTANT: Never pass cookies for YouTube - android_vr client works without them
    # and passing invalid/expired cookies causes instant bot detection block.
    if url and any(x in url.lower() for x in ["youtube.com", "youtu.be", "ytsearch"]):
        return opts

    cookie_path = COOKIES_PH_FILE if (url and "pornhub.com" in url.lower()) else COOKIES_FILE

    if os.path.exists(cookie_path):
        sanitize_cookies_file(cookie_path)
        try:
            with open(cookie_path, "r", encoding="utf-8", errors="ignore") as f:
                first_line = f.readline().strip().lstrip('\ufeff')
            if first_line.startswith("# Netscape"):
                opts["cookiefile"] = cookie_path
            else:
                logger.warning(f"Cookies file {cookie_path} does not start with Netscape header. Skipping.")
        except Exception as e:
            logger.error(f"Failed to read cookies file {cookie_path}: {e}")
    return opts

def sanitize_cookies_file(filepath: str):
    """Cleans a cookies.txt file to ensure valid Netscape format and LF line endings."""
    if not os.path.exists(filepath):
        return
    try:
        # Read the file contents safely
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
        except UnicodeDecodeError:
            with open(filepath, "r", encoding="latin-1") as f:
                content = f.read()

        # Remove UTF-8 BOM if present
        content = content.lstrip('\ufeff')

        lines = content.splitlines()
        cleaned_lines = []
        has_header = False

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            # Keep comments, but normalize the Netscape header
            if stripped.startswith("#"):
                if "Netscape HTTP Cookie File" in stripped:
                    if not has_header:
                        cleaned_lines.append("# Netscape HTTP Cookie File")
                        has_header = True
                    continue
                # Skip other comment headers to avoid issues, but keep other comments if they are not headers
                if "spec.html" not in stripped and "Do not edit" not in stripped:
                    cleaned_lines.append(stripped)
                continue

            # It's a cookie entry. Split by tab to verify
            cols = stripped.split("\t")
            if len(cols) >= 7:
                cleaned_lines.append(stripped)
            else:
                # Try space separation if they copy-pasted with spaces
                cols_space = stripped.split()
                if len(cols_space) >= 7:
                    # Reconstruct tab-separated line
                    reconstructed = "\t".join(cols_space[:6] + [" ".join(cols_space[6:])])
                    cleaned_lines.append(reconstructed)

        # Ensure the file starts with the header
        final_lines = [
            "# Netscape HTTP Cookie File",
            "# This is a sanitized Netscape cookie file.",
            ""
        ]

        for line in cleaned_lines:
            if "Netscape HTTP Cookie File" not in line:
                final_lines.append(line)

        # Write clean, LF-only file
        with open(filepath, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(final_lines) + "\n")

        logger.info(f"Successfully sanitized cookies file: {filepath}")
    except Exception as e:
        logger.error(f"Failed to sanitize cookies file: {e}")

# ---------------------------------------------------------------------------
# Cobalt API — residential-proxied downloader for datacenter-IP-blocked sites
# (PornHub, and others that 403 cloud/Render IPs)
# Cobalt.tools is a free public API. Falls back to yt-dlp if unavailable.
# ---------------------------------------------------------------------------
COBALT_API_INSTANCES = [
    "https://api.cobalt.tools",
    "https://cobalt-api.ente.io",
    "https://cobalt.api.timelessnesses.me",
    "https://api.cobalt.best",
]

async def _cobalt_download(url: str, dest_dir: str, audio_only: bool = False) -> str | None:
    """
    Downloads a URL through Cobalt API (bypasses datacenter IP blocks).
    Returns saved file path or None if all instances fail.
    """
    payload = {
        "url": url,
        "downloadMode": "audio" if audio_only else "auto",
        "videoQuality": "1080",
        "filenameStyle": "basic",
    }
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    proxy_url = get_proxy_url()

    async with aiohttp.ClientSession() as session:
        for instance in COBALT_API_INSTANCES:
            try:
                async with session.post(
                    f"{instance}/",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                    proxy=proxy_url
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"Cobalt {instance} returned HTTP {resp.status}")
                        continue
                    data = await resp.json()
                    status = data.get("status")
                    logger.info(f"Cobalt {instance} response status: {status}")

                    # tunnel / redirect — direct download URL
                    if status in ("tunnel", "redirect") and data.get("url"):
                        dl_url = data["url"]
                        ext = ".mp4" if not audio_only else ".mp3"
                        fname = os.path.join(dest_dir, f"cobalt_download{ext}")
                        async with session.get(dl_url, timeout=aiohttp.ClientTimeout(total=None, connect=20, sock_read=120), proxy=proxy_url) as dresp:
                            if dresp.status == 200:
                                with open(fname, "wb") as f:
                                    async for chunk in dresp.content.iter_chunked(512 * 1024):
                                        f.write(chunk)
                                logger.info(f"Cobalt download success via {instance}")
                                return fname

                    # picker — multiple files (e.g. gallery)
                    if status == "picker" and data.get("picker"):
                        dl_url = data["picker"][0].get("url")
                        if dl_url:
                            ext = ".mp4"
                            fname = os.path.join(dest_dir, f"cobalt_download{ext}")
                            async with session.get(dl_url, timeout=aiohttp.ClientTimeout(total=None, connect=20, sock_read=120), proxy=proxy_url) as dresp:
                                if dresp.status == 200:
                                    with open(fname, "wb") as f:
                                        async for chunk in dresp.content.iter_chunked(512 * 1024):
                                            f.write(chunk)
                                    return fname

                    # error status
                    if status and status != "tunnel" and status != "redirect" and status != "picker":
                        error_text = data.get("error", {}).get("description", "Unknown error")
                        logger.warning(f"Cobalt {instance} error: {status} - {error_text}")
                        
            except Exception as e:
                logger.warning(f"Cobalt instance {instance} failed: {e}")
                continue
    return None


# Working YouTube player clients (yt-dlp 2026.07+).
# android_vr is the default jsless client and works without PO tokens or cookies.
# tv_embedded, web_embedded are fallbacks. mweb also works without auth.
YOUTUBE_PLAYER_CLIENTS = ["android_vr", "web_embedded", "mweb"]


def get_youtube_extractor_args(extra: dict | None = None) -> dict:
    """Central YouTube extractor_args used by every yt-dlp call site."""
    args = {"player_client": list(YOUTUBE_PLAYER_CLIENTS)}
    if extra:
        args.update(extra)
    return {"youtube": args}


def is_youtube_url(url: str) -> bool:
    """True for youtube.com / youtu.be / ytsearch URLs."""
    return bool(re.search(r'(?:youtube\.com|youtu\.be|ytsearch)', url or "", re.IGNORECASE))


# Adult sites that need --impersonate chrome (and optionally Cobalt fallback).
# YouTube is intentionally NOT here: public Cobalt APIs require JWT now, and
# android_vr player client handles YouTube without browser impersonation.
_IMPERSONATE_SITES = [
    r'pornhub\.com',
    r'xvideos\.com',
    r'xhamster\.com',
    r'redtube\.com',
    r'youporn\.com',
    r'tube8\.com',
    r'spankbang\.com',
    r'xnxx\.com',
]

def is_impersonate_site(url: str) -> bool:
    """Returns True for sites that need --impersonate chrome in yt-dlp to work."""
    return any(re.search(p, url, re.IGNORECASE) for p in _IMPERSONATE_SITES)

# Keep is_datacenter_blocked_site as alias (used in error handling below)
def is_datacenter_blocked_site(url: str) -> bool:
    return is_impersonate_site(url)

def get_site_specific_opts(url: str) -> dict:
    """
    Returns site-specific yt-dlp options.
    For adult sites: impersonate Chrome to bypass bot detection.
    This is the fix recommended in yt-dlp issue tracker for PornHub 403.
    """
    if is_impersonate_site(url) and _HAS_IMPERSONATE:
        try:
            from yt_dlp.networking.impersonate import ImpersonateTarget
            target = ImpersonateTarget.from_str("chrome")
            return {"impersonate": target}
        except Exception:
            return {"impersonate": "chrome"}
    return {}


# ---------------------------------------------------------------------------
# reclip-style format probe: get real format_id before downloading
# Eliminates "Requested format is not available" errors entirely
# ---------------------------------------------------------------------------
def _probe_best_format_id(url: str, target_height: int | None, audio_only: bool) -> str | None:
    """
    Runs yt-dlp --dump-json to get available formats, then picks the best
    real format_id. Returns None if probe fails (caller falls back to format strings).
    NOTE: Never passes cookies for YouTube — android_vr client works without them.
    """
    is_youtube = any(x in url.lower() for x in ["youtube.com", "youtu.be"])
    try:
        cmd = [sys.executable, "-m", "yt_dlp",
               "--no-playlist", "--dump-json", "--no-download",
               "--socket-timeout", "35"]

        # Only pass cookies for non-YouTube sites
        if not is_youtube:
            cookie_path = COOKIES_PH_FILE if "pornhub.com" in url.lower() else None
            if cookie_path and os.path.exists(cookie_path):
                cmd += ["--cookies", cookie_path]

        if is_impersonate_site(url) and _HAS_IMPERSONATE and not is_youtube:
            cmd += ["--impersonate", "chrome"]

        proxy_url = get_proxy_url()
        if proxy_url:
            cmd += ["--proxy", proxy_url]

        # YouTube: use android_vr + web_embedded — no cookies, no PO token needed
        if is_youtube:
            cmd += ["--extractor-args", "youtube:player_client=android_vr,web_embedded,mweb"]

        cmd.append(url)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            err = result.stderr[-300:] if result.stderr else ""
            logger.warning(f"Format probe failed (rc={result.returncode}): {err}")
            return None

        info = json.loads(result.stdout)
        formats = info.get("formats", [])
        if not formats:
            return None

        if audio_only:
            audio_formats = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none"]
            if not audio_formats:
                audio_formats = formats
            best = max(audio_formats, key=lambda f: f.get("abr") or f.get("tbr") or 0)
            return best.get("format_id")

        if target_height:
            candidates = [f for f in formats if (f.get("height") or 0) <= target_height and f.get("vcodec") != "none"]
        else:
            candidates = [f for f in formats if f.get("vcodec") != "none"]

        if not candidates:
            candidates = formats

        best = max(candidates, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
        return best.get("format_id")
    except Exception as e:
        logger.warning(f"Format probe exception: {e}")
        return None

# =====================================================================
# Database Manager (SQLite)
# =====================================================================
class DbManager:
    """Manages bot statistics using an embedded SQLite database."""
    def __init__(self, db_path=None):
        if db_path is None:
            db_path = os.getenv("DB_PATH", os.path.join(BASE_DIR, "bot_data.db"))
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
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS tokens (
                    user_id INTEGER PRIMARY KEY,
                    github_token TEXT,
                    gitlab_token TEXT
                )
            """)

    def set_github_token(self, user_id, token):
        with self.conn:
            self.conn.execute("""
                INSERT INTO tokens (user_id, github_token) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET github_token = excluded.github_token
            """, (user_id, token))

    def set_gitlab_token(self, user_id, token):
        with self.conn:
            self.conn.execute("""
                INSERT INTO tokens (user_id, gitlab_token) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET gitlab_token = excluded.gitlab_token
            """, (user_id, token))

    def get_tokens(self, user_id):
        with self.conn:
            cursor = self.conn.cursor()
            cursor.execute("SELECT github_token, gitlab_token FROM tokens WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            if row:
                return row[0], row[1]
            return None, None

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
    proxy_url = get_proxy_url()
    async with aiohttp.ClientSession() as session:
        current_url = url
        headers = {"User-Agent": "Mozilla/5.0"}
        for _ in range(5):  # Max 5 hops
            try:
                async with session.head(current_url, headers=headers, allow_redirects=False, proxy=proxy_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        next_url = resp.headers.get('Location')
                        if next_url:
                            current_url = next_url
                            continue
                    break
            except Exception:
                break
        return current_url

INVIDIOUS_DOMAINS_CACHE = {
    "domains": [],
    "last_fetched": 0
}

async def get_invidious_domains():
    now = time.time()
    if INVIDIOUS_DOMAINS_CACHE["domains"] and (now - INVIDIOUS_DOMAINS_CACHE["last_fetched"]) < 3600:
        return INVIDIOUS_DOMAINS_CACHE["domains"]
        
    proxy_url = get_proxy_url()
    default_fallbacks = [
        "invidious.nerdvpn.de",
        "inv.nadeko.net",
        "invidious.fdn.fr",
        "iv.datura.network",
        "invidious.perennialte.ch",
        "yt.artemislena.eu",
        "invidious.privacyredirect.com",
        "vid.puffyan.us"
    ]
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.invidious.io/instances.json", proxy=proxy_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    instances_data = await resp.json()
                    domains = []
                    for item in instances_data:
                        if isinstance(item, list) and len(item) == 2:
                            domain = item[0]
                            details = item[1]
                            if not details:
                                continue
                            monitor = details.get("monitor") or {}
                            is_up = not monitor.get("down")
                            is_api = details.get("api") == True
                            if is_up:
                                domains.append((domain, is_api))
                    
                    # Sort API first
                    api_domains = [d[0] for d in domains if d[1]]
                    non_api_domains = [d[0] for d in domains if not d[1]]
                    candidates = api_domains + non_api_domains
                    if candidates:
                        INVIDIOUS_DOMAINS_CACHE["domains"] = candidates
                        INVIDIOUS_DOMAINS_CACHE["last_fetched"] = now
                        return candidates
    except Exception as e:
        logger.warning(f"Failed to fetch Invidious instances list: {e}")
        
    return default_fallbacks

async def search_youtube_invidious(query: str) -> str | None:
    import urllib.parse
    domains = await get_invidious_domains()
    proxy_url = get_proxy_url()
    encoded_query = urllib.parse.quote(query)
    
    for domain in domains[:8]:
        search_url = f"https://{domain}/api/v1/search?q={encoded_query}&type=video"
        logger.info(f"Trying Invidious search on {domain}...")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(search_url, proxy=proxy_url,
                                       timeout=aiohttp.ClientTimeout(total=12)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, list) and len(data) > 0:
                            for item in data[:3]:
                                video_id = item.get("videoId")
                                if video_id:
                                    length = item.get("lengthSeconds", 0)
                                    if length and length > 0:
                                        logger.info(f"Invidious search success ({domain}): {video_id}")
                                        return f"https://www.youtube.com/watch?v={video_id}"
                            video_id = data[0].get("videoId")
                            if video_id:
                                logger.info(f"Invidious search success ({domain}): {video_id}")
                                return f"https://www.youtube.com/watch?v={video_id}"
        except Exception as e:
            logger.warning(f"Invidious search failed on {domain}: {e}")
            
    return None

async def fetch_spotify_metadata(url):
    """Fetches track metadata from public Spotify page with robust fallbacks."""
    import re

    track_id = None
    m = re.search(r'/track/([a-zA-Z0-9]+)', url)
    if m:
        track_id = m.group(1)

    if not track_id:
        parsed = urlparse(url)
        path_parts = parsed.path.strip("/").split("/")
        if "track" in path_parts:
            idx = path_parts.index("track")
            if idx + 1 < len(path_parts):
                track_id = path_parts[idx + 1].split("?")[0]

    if not track_id:
        return None

    # Method 1: spotipy library (official)
    try:
        import spotipy
        from spotipy import SpotifyClientCredentials
        client_id = os.getenv("SPOTIFY_CLIENT_ID", "")
        client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "")
        if client_id and client_secret:
            sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
                client_id=client_id, client_secret=client_secret
            ))
            track_info = sp.track(track_id)
            if track_info:
                logger.info("Spotify metadata fetched via spotipy")
                return {
                    "title": track_info.get("name"),
                    "artist": track_info.get("artists", [{}])[0].get("name", "Unknown Artist"),
                    "thumbnail": track_info.get("album", {}).get("images", [{}])[0].get("url")
                }
    except Exception as e:
        logger.warning(f"spotipy failed: {e}")

    # Method 2: oEmbed API (no auth needed) — title only; artist often missing
    oembed_title = None
    oembed_thumb = None
    try:
        import urllib.parse as _up
        # Prefer clean track URL for oEmbed (spotify.link short URLs can fail)
        oembed_target = f"https://open.spotify.com/track/{track_id}" if track_id else url
        oembed_url = f"https://open.spotify.com/oembed?url={_up.quote(oembed_target)}"
        proxy_url = get_proxy_url()
        async with aiohttp.ClientSession() as session:
            async with session.get(oembed_url, timeout=aiohttp.ClientTimeout(total=10), proxy=proxy_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    title = (data.get("title") or "").strip()
                    if title:
                        logger.info("Spotify oEmbed title OK, refining artist via other methods...")
                        artist = "Unknown Artist"
                        if " - " in title:
                            parts = title.split(" - ", 1)
                            artist = parts[0].strip()
                            title = parts[1].strip()
                        elif " by " in title:
                            parts = title.rsplit(" by ", 1)
                            title = parts[0].strip()
                            artist = parts[1].strip()
                        oembed_title = title
                        oembed_thumb = data.get("thumbnail_url")
                        # If we already have a real artist, return immediately
                        if artist and artist.lower() not in ("unknown artist", "unknown"):
                            return {
                                "title": title,
                                "artist": artist,
                                "thumbnail": oembed_thumb,
                            }
    except Exception as e:
        logger.warning(f"oEmbed failed: {e}")

    # Method 3: curl_cffi browser impersonation (bypasses datacenter IP blocks)
    if _HAS_IMPERSONATE:
        try:
            from curl_cffi import requests as cffi_requests
            proxy_url = get_proxy_url()
            proxies = {"https": proxy_url, "http": proxy_url} if proxy_url else None
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            }
            track_url = f"https://open.spotify.com/track/{track_id}"
            resp = cffi_requests.get(track_url, impersonate="chrome", headers=headers, proxies=proxies, timeout=5)
            if resp.status_code == 200:
                html = resp.text
                title_match = re.search(r'<meta property="og:title" content="([^"]+)"', html)
                title = unquote(title_match.group(1)) if title_match else None
                image_match = re.search(r'<meta property="og:image" content="([^"]+)"', html)
                image = image_match.group(1) if image_match else None
                desc_match = re.search(r'<meta property="og:description" content="([^"]+)"', html)
                desc = unquote(desc_match.group(1)) if desc_match else ""
                artist = "Unknown Artist"
                if " · " in desc:
                    parts = desc.split(" · ")
                    if parts[0].startswith("Song by "):
                        artist = parts[0].replace("Song by ", "")
                    elif len(parts) > 1 and "by " in parts[0]:
                        artist = parts[0].split("by ")[-1]
                elif "by " in desc:
                    artist = desc.split("by ")[-1]
                if title:
                    logger.info("Spotify metadata fetched via curl_cffi impersonation")
                    return {"title": title, "artist": artist, "thumbnail": image or oembed_thumb}
        except Exception as e:
            logger.warning(f"curl_cffi Spotify fetch failed: {e}")

    # Method 4: og tags from embed page
    try:
        proxy_url = get_proxy_url()
        embed_url = f"https://open.spotify.com/embed/track/{track_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(embed_url, timeout=aiohttp.ClientTimeout(total=4), proxy=proxy_url) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    title_match = re.search(r'<meta property="og:title" content="([^"]+)"', html)
                    title = unquote(title_match.group(1)) if title_match else None
                    image_match = re.search(r'<meta property="og:image" content="([^"]+)"', html)
                    image = image_match.group(1) if image_match else None
                    desc_match = re.search(r'<meta property="og:description" content="([^"]+)"', html)
                    desc = unquote(desc_match.group(1)) if desc_match else ""
                    artist = "Unknown Artist"
                    if " · " in desc:
                        parts = desc.split(" · ")
                        if parts[0].startswith("Song by "):
                            artist = parts[0].replace("Song by ", "")
                        elif len(parts) > 1 and "by " in parts[0]:
                            artist = parts[0].split("by ")[-1]
                    elif "by " in desc:
                        artist = desc.split("by ")[-1]
                    if title:
                        logger.info("Spotify metadata fetched via embed page")
                        return {"title": title, "artist": artist, "thumbnail": image or oembed_thumb}
    except Exception as e:
        logger.warning(f"Embed page failed: {e}")

    # Last resort: oEmbed title alone is enough to search YouTube for the track
    if oembed_title:
        logger.info("Spotify metadata: using oEmbed title only (artist unknown)")
        return {"title": oembed_title, "artist": "Unknown Artist", "thumbnail": oembed_thumb}

    return None

async def upload_to_techpulse(filepath, custom_filename=None):
    """Programmatically uploads a local file to the TechPulse Uploader API."""
    url = os.getenv("TECHPULSE_UPLOAD_URL", "http://localhost:3000/api/uploads")
    filename = custom_filename or os.path.basename(filepath)
    
    import mimetypes
    mime_type, _ = mimetypes.guess_type(filepath)
    if not mime_type:
        mime_type = 'application/octet-stream'
        
    logger.info(f"Uploading {filename} ({mime_type}) to TechPulse: {url}")
    proxy_url = get_proxy_url()
    try:
        data = aiohttp.FormData()
        with open(filepath, 'rb') as f:
            data.add_field(
                'file',
                f,
                filename=filename,
                content_type=mime_type
            )
        data.add_field('category', 'دانلود بات')
        data.add_field('ownerId', 'downloader_bot')
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=120),
                                   proxy=proxy_url) as resp:
                if resp.status in (200, 201):
                    res_json = await resp.json()
                    logger.info(f"TechPulse upload succeeded: {res_json}")
                    return True, "Success"
                else:
                    res_text = await resp.text()
                    logger.error(f"TechPulse upload failed: status={resp.status}, response={res_text}")
                    return False, f"Status {resp.status}"
    except Exception as e:
        logger.error(f"TechPulse upload exception: {e}")
        return False, str(e)

async def upload_to_uplod_ir(filepath: str) -> str:
    """Uploads a file to uplod.ir and returns the download link."""
    try:
        proxy_url = get_proxy_url()
        UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        headers = {"User-Agent": UA}
        
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get("https://uplod.ir", ssl=False, proxy=proxy_url,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    raise ValueError(f"Failed to load uplod.ir homepage: HTTP {resp.status}")
                text = await resp.text()
            
            upload_url = None
            patterns = [
                r'action="([^"]*cgi-bin/upload\.cgi[^"]*)"',
                r'action=["\']([^"\']*upload\.cgi[^"\']*)["\']',
                r'action="(https?://[^"]*upload[^"]*)"',
                r'(https?://[^"\']*cgi-bin/upload\.cgi[^"\']*)',
            ]
            for pat in patterns:
                match = re.search(pat, text, re.IGNORECASE)
                if match:
                    upload_url = match.group(1)
                    break
            
            if not upload_url:
                raise ValueError("Could not find upload URL on uplod.ir (page structure may have changed)")
            
            if upload_url.startswith("/"):
                upload_url = "https://uplod.ir" + upload_url
            
            logger.info(f"Uplod.ir upload URL found: {upload_url}")
            
            file_size = os.path.getsize(filepath)
            filename = os.path.basename(filepath)
            
            data = aiohttp.FormData()
            data.add_field('upload_type', 'file')
            with open(filepath, 'rb') as f:
                data.add_field('file_0', f, filename=filename,
                              content_type='application/octet-stream')
            
            async with session.post(upload_url, data=data, ssl=False, proxy=proxy_url,
                                   timeout=aiohttp.ClientTimeout(total=300)) as resp:
                result_text = await resp.text()
                
                json_str = result_text
                ta_match = re.search(r'<textarea[^>]*>(.*?)</textarea>', result_text, re.IGNORECASE | re.DOTALL)
                if ta_match:
                    json_str = ta_match.group(1)
                
                try:
                    result = json.loads(json_str)
                    if isinstance(result, list) and len(result) > 0:
                        file_code = result[0].get("file_code") or result[0].get("code")
                        if file_code:
                            return f"http://uplod.ir/{file_code}"
                        dl_link = result[0].get("download_page") or result[0].get("url")
                        if dl_link:
                            return dl_link
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass
                
                fn_match = re.search(r'fn["\s:>]+([^<"\n]+)', result_text)
                if fn_match:
                    code = fn_match.group(1).strip()
                    if not code.startswith("http"):
                        return f"http://uplod.ir/{code}"
                    return code
                
                code_match = re.search(r'(?:file_code|code|download_link)["\s:=]+["\']?([a-zA-Z0-9]{5,20})', result_text)
                if code_match:
                    return f"http://uplod.ir/{code_match.group(1)}"
                    
                raise ValueError(f"Could not parse upload result: {result_text[:200]}")
    except Exception as e:
        logger.error(f"Uplod.ir upload failed: {e}")
        return None

UPLOAD_CACHE = {}

async def register_file_for_upload(bot, chat_id, filepath, filename, reply_to_message_id=None):
    """
    Moves filepath to a persistent downloads_cache directory,
    presents an inline button for optional TechPulse upload,
    and schedules automatic cleanup after 10 minutes.
    """
    if not filepath or not os.path.exists(filepath):
        return

    # Create unique cache directory
    unique_id = uuid.uuid4().hex[:8]
    cache_dir = os.path.join("downloads_cache", unique_id)
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except Exception as e:
        logger.error(f"Failed to create cache dir: {e}")
        return

    # Target path in cache
    cached_path = os.path.join(cache_dir, filename)
    try:
        shutil.move(filepath, cached_path)
    except Exception as e:
        logger.error(f"Failed to move file to cache, attempting copy: {e}")
        try:
            shutil.copy2(filepath, cached_path)
        except Exception as ec:
            logger.error(f"Failed to copy file to cache: {ec}")
            return

    # Store in global dictionary
    UPLOAD_CACHE[unique_id] = {
        'filepath': cached_path,
        'filename': filename
    }

    # Setup inline button
    keyboard = [
        [InlineKeyboardButton("☁️ Upload to TechPulse / آپلود روی پلتفرم", callback_data=f"tp_upload:{unique_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=f"☁️ *آپلود در پلتفرم TechPulse*\nآیا مایلید فایل `{filename}` روی سایت آپلود شود؟",
            reply_markup=reply_markup,
            reply_to_message_id=reply_to_message_id,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Failed to send upload button: {e}")

    # Schedule deletion in 10 minutes
    async def cleanup_task():
        await asyncio.sleep(600)
        try:
            if os.path.exists(cache_dir):
                shutil.rmtree(cache_dir)
                logger.info(f"Cleaned up cached upload dir: {cache_dir}")
            UPLOAD_CACHE.pop(unique_id, None)
        except Exception as ex:
            logger.error(f"Error cleaning up cache dir {cache_dir}: {ex}")

    asyncio.create_task(cleanup_task())

async def download_thumbnail(url, dest_dir):
    """Downloads video thumbnail in the background."""
    if not url:
        return None
    proxy_url = get_proxy_url()
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=10, proxy=proxy_url) as resp:
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
    if not tracker:
        return
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
            f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
            f"┃   📥  *DOWNLOADING*        ┃\n"
            f"┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
            f"📁 `{filename[:35]}`\n"
            f"⚡ {bar}\n"
            f"📦 `{downloaded_mb:.1f} MB` / `{total_mb:.1f} MB`\n"
            f"🚀 `{speed_mb:.2f} MB/s`"
        )
        tracker.update(text)
    elif d["status"] == "finished":
        tracker.update("📥 *Download finished! Processing...*")

class context_bot_wrapper:
    """Wraps a message object to provide edit_message_text compatible with ProgressTracker."""
    def __init__(self, message_obj):
        self.message = message_obj
    async def edit_message_text(self, chat_id, message_id, text, parse_mode=None):
        return await self.message.edit_text(text, parse_mode=parse_mode)

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
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    downloaded = 0
    total_size = 0
    retries = 5
    last_update = 0
    proxy_url = get_proxy_url()

    async with aiohttp.ClientSession() as session:
        while retries > 0:
            try:
                req_headers = {"User-Agent": UA}
                if os.path.exists(filepath):
                    downloaded = os.path.getsize(filepath)

                if downloaded > 0:
                    req_headers['Range'] = f"bytes={downloaded}-"

                async with session.get(
                    url,
                    headers=req_headers,
                    allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=None, connect=15, sock_read=60),
                    proxy=proxy_url
                ) as response:
                    # 416 = Range not satisfiable → file already complete
                    if response.status == 416:
                        break
                    # Non-Range servers return 200 for Range requests → treat as full restart
                    if response.status not in (200, 206):
                        raise Exception(f"Server returned HTTP {response.status}")

                    if total_size == 0:
                        content_length = int(response.headers.get("Content-Length", 0))
                        total_size = content_length + (downloaded if response.status == 206 else 0)

                        if downloaded == 0:
                            # Detect filename from headers
                            cd = response.headers.get("Content-Disposition", "")
                            if cd:
                                fname_match = re.findall(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\'\s;]+)', cd)
                                if fname_match:
                                    filepath = os.path.join(os.path.dirname(filepath), unquote(fname_match[0]))

                            content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
                            if content_type and not os.path.splitext(filepath)[1]:
                                ext = mimetypes.guess_extension(content_type)
                                if ext:
                                    filepath += ext

                            if custom_name:
                                _, ext = os.path.splitext(filepath)
                                if not os.path.splitext(custom_name)[1] and ext:
                                    custom_name += ext
                                filepath = os.path.join(os.path.dirname(filepath), custom_name)

                    # If server ignored Range and returned 200, restart from beginning
                    mode = "ab" if downloaded > 0 and response.status == 206 else "wb"
                    if mode == "wb":
                        downloaded = 0

                    with open(filepath, mode) as f:
                        async for chunk in response.content.iter_chunked(512 * 1024):
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
                                    f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                                    f"┃  📥  *DIRECT DOWNLOAD*   ┃\n"
                                    f"┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                                    f"📁 `{os.path.basename(filepath)[:40]}`\n"
                                    f"⚡ {bar_str}\n"
                                    f"📦 `{downloaded_str}` / `{total_str}`"
                                )
                                try:
                                    await progress_message.edit_text(text, parse_mode="Markdown")
                                except Exception:
                                    pass
                    break  # Download complete

            except Exception as e:
                retries -= 1
                logger.warning(f"Direct download error (retries left {retries}): {e}")
                await asyncio.sleep(3)
                if retries == 0:
                    raise Exception(f"Download failed after all retries: {e}") from e

    return filepath

def _resolve_yt_format(format_opt: str, has_ffmpeg: bool) -> tuple:
    """
    Returns (ydl_format_str, merge_fmt, postprocessors) for a given format option.
    Uses a smart fallback chain so yt-dlp always finds a valid format.
    Strategy: sort by quality/codec, prefer mp4/m4a, fall back to any available.
    """
    postprocessors = []
    merge_fmt = "mp4" if has_ffmpeg else None

    if format_opt == "audio":
        # Best audio, any container — FFmpeg will transcode to mp3
        ydl_format = "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best"
        merge_fmt = None
        if has_ffmpeg:
            postprocessors = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
    elif format_opt in ("1080p", "720p", "480p", "360p"):
        res = format_opt[:-1]
        # Prefer mp4+m4a merge, fall back through progressively looser constraints
        ydl_format = (
            f"bestvideo[height<={res}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={res}][ext=mp4]+bestaudio/"
            f"bestvideo[height<={res}]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={res}]+bestaudio/"
            f"best[height<={res}]/best"
        )
    else:
        # Default best quality — use format_sort instead of explicit codec to let
        # yt-dlp decide what's actually available (avoids "format not available" error)
        if has_ffmpeg:
            ydl_format = "bestvideo+bestaudio/best"
        else:
            # No FFmpeg: can't merge separate streams, get best single-file format
            ydl_format = "best[ext=mp4]/best"

    return ydl_format, merge_fmt, postprocessors


def download_yt(url, dest_dir, format_opt, start_time, end_time, tracker):
    """
    Downloads YouTube/Social content via yt-dlp.
    Strategy (inspired by reclip + upekshaip):
    1. Probe available format_ids via --dump-json (reclip pattern) → no "format not available"
    2. If probe succeeds, use exact format_id; otherwise fall back to format strings
    3. format_sort guides yt-dlp toward mp4/m4a/h264 when multiple formats match
    """
    has_ffmpeg = bool(FFMPEG_EXE)
    audio_only = (format_opt == "audio")
    target_height = int(format_opt[:-1]) if format_opt in ("1080p", "720p", "480p", "360p") else None

    # --- Step 1: reclip-style format probe (get real format_id) ---
    probed_format_id = _probe_best_format_id(url, target_height, audio_only)

    if probed_format_id:
        # Use exact format_id — guaranteed to exist
        if audio_only:
            ydl_format = probed_format_id
        elif has_ffmpeg:
            # Merge best video with best audio
            ydl_format = f"{probed_format_id}+bestaudio/best"
        else:
            ydl_format = probed_format_id
        merge_fmt = "mp4" if (not audio_only and has_ffmpeg) else None
        postprocessors = []
        if audio_only and has_ffmpeg:
            postprocessors = [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}]
        logger.info(f"Using probed format_id: {probed_format_id} → format string: {ydl_format}")
    else:
        # --- Step 2: fallback to smart format strings ---
        ydl_format, merge_fmt, postprocessors = _resolve_yt_format(format_opt, has_ffmpeg)
        logger.info(f"Format probe failed, using fallback format string: {ydl_format}")

    base_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    site_opts = get_site_specific_opts(url)

    ydl_opts = {
        "outtmpl": os.path.join(dest_dir, "%(title)s.%(ext)s"),
        "format": ydl_format,
        # format_sort: guides yt-dlp toward preferred containers when format is a wildcard
        "format_sort": ["res", "ext:mp4:m4a", "vcodec:avc", "acodec:aac", "filesize"],
        "progress_hooks": [lambda d: yt_dlp_hook(d, tracker)],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "concurrent_fragment_downloads": 16,
        "fragment_retries": 10,
        "retries": 10,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "writesubtitles": True,
        "allsubtitles": False,
        "subtitleslangs": ["en", "fa"],
        "http_headers": site_opts.pop("http_headers", base_headers),
        "ffmpeg_location": FFMPEG_EXE,
        "extractor_args": get_youtube_extractor_args({"skip": ["translated_subs"]}),
        "socket_timeout": 45,
        **get_ydl_cookie_opts(url),   # Returns {} for YouTube (no cookies)
        **site_opts,
    }

    proxy_url = get_proxy_url()
    if proxy_url:
        ydl_opts["proxy"] = proxy_url

    if merge_fmt and has_ffmpeg:
        ydl_opts["merge_output_format"] = merge_fmt
    if postprocessors:
        ydl_opts["postprocessors"] = postprocessors
    if start_time is not None and end_time is not None:
        _st, _et = start_time, end_time
        ydl_opts['download_ranges'] = lambda info, ydl: [{'start_time': _st, 'end_time': _et}]
        ydl_opts['force_keyframes_at_cuts'] = True

    filename = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
    except Exception as e:
        if "cookiefile" in ydl_opts:
            logger.warning("Download failed with cookies, retrying without cookies...")
            del ydl_opts["cookiefile"]
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    filename = ydl.prepare_filename(info)
            except Exception as e_inner:
                e = e_inner
        if "impersonate" in ydl_opts and not (filename and os.path.exists(filename)):
            logger.warning("Download failed with impersonate, retrying without...")
            del ydl_opts["impersonate"]
            ydl_opts["http_headers"] = base_headers
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    filename = ydl.prepare_filename(info)
            except Exception as e_inner:
                e = e_inner
        
        if not (filename and os.path.exists(filename)):
            raise e

    if audio_only:
        base, _ = os.path.splitext(filename)
        for ext in (".mp3", ".m4a", ".opus", ".ogg", ".webm"):
            if os.path.exists(base + ext):
                filename = base + ext
                break

    if merge_fmt == "mp4" and not filename.endswith(".mp4"):
        base, _ = os.path.splitext(filename)
        if os.path.exists(base + ".mp4"):
            filename = base + ".mp4"

    return filename

# =====================================================================
# Video Processing & Splitting
# =====================================================================
def split_video_ffmpeg(filepath, dest_dir, max_part_size=MAX_PART_SIZE):
    """Segments a video file into playable chunks using FFmpeg."""
    if not FFMPEG_EXE:
        return None
    try:
        file_size = os.path.getsize(filepath)
        cmd = [
            FFPROBE_EXE, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", filepath
        ]
        duration = float(subprocess.check_output(cmd, stderr=subprocess.PIPE).decode().strip())
        if duration <= 0:
            return None
        bitrate = file_size / duration
        segment_duration = int((max_part_size * 0.85) / bitrate)  # 85% safety margin

        if segment_duration <= 0 or segment_duration >= duration:
            return None

        _, ext = os.path.splitext(filepath)
        ext = ext or ".mp4"
        # Use a safe ASCII prefix to avoid filename matching issues with special chars
        safe_prefix = "videopart"
        output_template = os.path.join(dest_dir, f"{safe_prefix}%03d{ext}")

        cmd_split = [
            FFMPEG_EXE, "-y", "-i", filepath, "-c", "copy", "-map", "0",
            "-segment_time", str(segment_duration), "-f", "segment",
            "-reset_timestamps", "1", output_template
        ]
        subprocess.run(cmd_split, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        parts = sorted([
            os.path.join(dest_dir, f)
            for f in os.listdir(dest_dir)
            if f.startswith(safe_prefix) and f.endswith(ext)
        ])
        return parts if parts else None
    except Exception as e:
        logger.error(f"FFmpeg segmenting failed: {e}")
        return None

def split_file_binary(file_path, chunk_size):
    """Splits raw files into chunks of specified size."""
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
    if not FFMPEG_EXE:
        return False
    try:
        # High quality palette-based GIF conversion using FFmpeg
        cmd = [
            FFMPEG_EXE, "-y", "-i", input_path,
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
        
        if not FFMPEG_EXE:
            raise Exception("FFmpeg is not installed on this server. Audio conversion is unavailable.")
            
        cmd = [FFMPEG_EXE, "-y", "-i", input_path]
        
        if target_format == "ogg":
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
        
    elif target_format == "compress":
        # Video Compression using FFmpeg (libx264, medium speed, crf 28, aac 128k audio)
        output_path = os.path.join(temp_dir, "compressed_" + name_part + ".mp4")
        if not FFMPEG_EXE:
            raise Exception("FFmpeg is not installed on this server. Video compression is unavailable.")
        
        cmd = [
            FFMPEG_EXE, "-y", "-i", input_path,
            "-vf", "scale='min(1280,iw)':-2",
            "-vcodec", "libx264", "-crf", "28", "-preset", "fast",
            "-acodec", "aac", "-b:a", "128k",
            output_path
        ]
        
        loop = asyncio.get_running_loop()
        def process_compress():
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return output_path
        return await loop.run_in_executor(None, process_compress)

    elif target_format.startswith("vfx_"):
        # Voice Effects using FFmpeg filters
        effect = target_format.split("_")[1]
        output_path = os.path.join(temp_dir, f"vfx_{effect}_" + name_part + ".mp3")
        if not FFMPEG_EXE:
            raise Exception("FFmpeg is not installed on this server.")
            
        filters = {
            "alien": "asetrate=44100*0.6,atempo=1.66,vibrato=f=12:d=0.7",
            "chipmunk": "asetrate=44100*1.4,atempo=0.7",
            "robot": "aecho=0.8:0.88:6:0.4,asetrate=44100*0.9,atempo=1.1",
            "echo": "aecho=0.8:0.9:1000:0.3"
        }
        filter_str = filters.get(effect, "aecho=0.8:0.88:6:0.4")
        cmd = [FFMPEG_EXE, "-y", "-i", input_path, "-af", filter_str, output_path]
        
        loop = asyncio.get_running_loop()
        def process_vfx():
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return output_path
        return await loop.run_in_executor(None, process_vfx)

    elif target_format.startswith("tags:"):
        # Music Tag Editor using Mutagen EasyID3
        parts = target_format.split(":")
        artist = parts[1]
        title = parts[2]
        album = parts[3]
        output_path = os.path.join(temp_dir, "tagged_" + name_part + ".mp3")
        shutil.copy(input_path, output_path)
        
        loop = asyncio.get_running_loop()
        def process_tags():
            from mutagen.easyid3 import EasyID3
            try:
                audio = EasyID3(output_path)
            except Exception:
                from mutagen.mp3 import MP3
                audio_mp3 = MP3(output_path)
                audio_mp3.add_tags()
                audio_mp3.save()
                audio = EasyID3(output_path)
            audio['artist'] = artist
            audio['title'] = title
            audio['album'] = album
            audio.save()
            return output_path
        return await loop.run_in_executor(None, process_tags)

    elif target_format.startswith("pdfprotect:"):
        # Protect PDF using password
        password = target_format.split(":", 1)[1]
        output_path = os.path.join(temp_dir, "protected_" + name_part + ".pdf")
        
        loop = asyncio.get_running_loop()
        def process_protect():
            from pypdf import PdfReader, PdfWriter
            reader = PdfReader(input_path)
            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)
            writer.encrypt(user_password=password, owner_password=None, use_128bit=True)
            with open(output_path, "wb") as f:
                writer.write(f)
            return output_path
        return await loop.run_in_executor(None, process_protect)

    elif target_format.startswith("pdfunlock:"):
        # Unlock PDF using password
        password = target_format.split(":", 1)[1]
        output_path = os.path.join(temp_dir, "unlocked_" + name_part + ".pdf")
        
        loop = asyncio.get_running_loop()
        def process_unlock():
            from pypdf import PdfReader, PdfWriter
            reader = PdfReader(input_path)
            if reader.is_encrypted:
                reader.decrypt(password)
            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)
            with open(output_path, "wb") as f:
                writer.write(f)
            return output_path
        return await loop.run_in_executor(None, process_unlock)

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
            # Try LibreOffice first (handles Persian RTL formatting and custom fonts much better than MS Word COM)
            lo_path = None
            if shutil.which("libreoffice"):
                lo_path = shutil.which("libreoffice")
            elif shutil.which("soffice"):
                lo_path = shutil.which("soffice")
            else:
                # Check common installation path on Windows
                win_lo = r"C:\Program Files\LibreOffice\program\soffice.exe"
                if os.path.exists(win_lo):
                    lo_path = win_lo
            
            if lo_path:
                logger.info(f"Using LibreOffice for DOCX to PDF conversion: {lo_path}")
                cmd_lo = [
                    lo_path,
                    "--headless",
                    "--convert-to", "pdf",
                    "--outdir", temp_dir,
                    input_path
                ]
                subprocess.run(cmd_lo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                lo_output = os.path.join(temp_dir, name_part + ".pdf")
                if os.path.exists(lo_output):
                    return lo_output
            
            # Fallback to docx2pdf (MS Word COM) on Windows if LibreOffice is not available
            if os.name == 'nt':
                logger.warning("LibreOffice not found. Falling back to docx2pdf (MS Word COM)...")
                from docx2pdf import convert
                convert(input_path, output_path)
                return output_path
                
            raise Exception("Word conversion failed. LibreOffice is not installed on this server.")
                
        return await loop.run_in_executor(None, process_docx)
        
    elif target_format == "gif":
        output_path = os.path.join(temp_dir, name_part + ".gif")
        if convert_to_gif_ffmpeg(input_path, output_path):
            return output_path
        raise Exception("GIF conversion failed.")
        
    return None

# =====================================================================
# Messaging & Telegram Upload Engine
# =====================================================================
async def send_file_to_telegram(bot, chat_id, filepath, reply_to_message_id, thumbnail_path=None, as_gif=False, user_id=None, audio_title=None, audio_performer=None):
    """Sends documents, videos, audios or GIFs to the user, handling auto-splitting and custom tags."""
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
            
            # Send as Native Audio if Spotify/Audio metadata is present
            if audio_title and audio_performer and filepath.lower().endswith(".mp3"):
                with open(filepath, "rb") as f:
                    sent_msg = await bot.send_audio(
                        chat_id=chat_id,
                        audio=f,
                        title=audio_title,
                        performer=audio_performer,
                        thumbnail=thumb_file,
                        reply_to_message_id=reply_to_message_id
                    )
            elif is_video:
                try:
                    with open(filepath, "rb") as f:
                        sent_msg = await bot.send_video(
                            chat_id=chat_id,
                            video=f,
                            filename=filename,
                            thumbnail=thumb_file,
                            reply_to_message_id=reply_to_message_id,
                            supports_streaming=True,
                            read_timeout=120,
                            write_timeout=120,
                            connect_timeout=30,
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
                            reply_to_message_id=reply_to_message_id,
                            read_timeout=120,
                            write_timeout=120,
                            connect_timeout=30,
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
                await register_file_for_upload(bot, chat_id, filepath, filename)
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
        loop = asyncio.get_running_loop()

        if is_video:
            await status_msg.edit_text("🎬 *Splitting video into playable parts using FFmpeg...*", parse_mode="Markdown")
            # Run blocking ffmpeg split in thread to avoid blocking event loop
            _fp = filepath
            parts = await loop.run_in_executor(
                None, lambda: split_video_ffmpeg(_fp, os.path.dirname(_fp), MAX_PART_SIZE)
            )
            if parts:
                used_ffmpeg = True

        if not parts:
            await status_msg.edit_text("📦 *Splitting file into binary parts...*", parse_mode="Markdown")
            _fp = filepath
            parts = await loop.run_in_executor(
                None, lambda: split_file_binary(_fp, MAX_PART_SIZE)
            )

        total_parts = len(parts)
        await status_msg.edit_text(f"📦 *Split into {total_parts} parts.* Starting upload...", parse_mode="Markdown")

        try:
            for idx, part_path in enumerate(parts):
                part_name = os.path.basename(part_path)
                part_size_mb = os.path.getsize(part_path) / (1024 * 1024)
                await status_msg.edit_text(
                    f"📤 *Uploading part {idx + 1}/{total_parts}:* `{part_name}`\n"
                    f"📦 *Size:* `{part_size_mb:.1f} MB`",
                    parse_mode="Markdown",
                )
                thumb_file = open(thumbnail_path, "rb") if thumbnail_path and os.path.exists(thumbnail_path) else None

                # Large file uploads need extended timeouts (5 minutes per part)
                UPLOAD_TIMEOUT = 300

                with open(part_path, "rb") as f:
                    if is_video and used_ffmpeg:
                        await bot.send_video(
                            chat_id=chat_id,
                            video=f,
                            filename=part_name,
                            thumbnail=thumb_file,
                            reply_to_message_id=reply_to_message_id,
                            supports_streaming=True,
                            read_timeout=UPLOAD_TIMEOUT,
                            write_timeout=UPLOAD_TIMEOUT,
                            connect_timeout=30,
                        )
                    else:
                        await bot.send_document(
                            chat_id=chat_id,
                            document=f,
                            filename=part_name,
                            thumbnail=thumb_file,
                            reply_to_message_id=reply_to_message_id,
                            read_timeout=UPLOAD_TIMEOUT,
                            write_timeout=UPLOAD_TIMEOUT,
                            connect_timeout=30,
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
            await register_file_for_upload(bot, chat_id, filepath, filename)
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
        text="☁️ *Cloud Archiving available:*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# =====================================================================
# Commands
# =====================================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends welcome message and instructions with premium visual layout."""
    welcome_text = (
        f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
        f"┃  🌟  *S E N I O R*  🌟  ┃\n"
        f"┃   𝗗 𝗢 𝗪 𝗡 𝗟 𝗢 𝗔 𝗗 𝗘 𝗥   ┃\n"
        f"┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
        f"✨ _The ultimate media & utility bot_\n"
        f"پیشرفته‌ترین ربات دانلودر و دستیار هوشمند\n\n"
        f"{THIN_DIVIDER}\n"
        f"  🔹 *Media / رسانه* — Send any YouTube, Insta, or TikTok link\n"
        f"  🔹 *Music / موزیک* — Send a Spotify link to get MP3\n"
        f"  🔹 *Files / فایل* — Send any file/photo to convert\n"
        f"  🔹 *Code / کدنویسی* — Send a GitHub/GitLab link\n"
        f"  🔹 *AI / هوش مصنوعی* — Click AI Chat to start talking\n"
        f"{THIN_DIVIDER}\n"
        f"💡 *Quick Commands:*\n"
        f"  `/search <name>` — Search YouTube\n"
        f"  `/direct <url>` — Force direct download\n"
        f"  `/stats` — View your statistics\n"
        f"{THIN_DIVIDER}\n"
        f"👇 _Choose an option from the menu below:_"
    )
    keyboard = [
        [
            KeyboardButton("🔍 YouTube"),
            KeyboardButton("🎵 Spotify"),
            KeyboardButton("🐙 Git")
        ],
        [
            KeyboardButton("📥 Direct DL"),
            KeyboardButton("🔄 Converter"),
            KeyboardButton("🤖 AI Chat")
        ],
        [
            KeyboardButton("📊 Stats"),
            KeyboardButton("❓ Help")
        ]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown")

def run_youtube_search(query, page=1):
    """Internal helper to execute YouTube search for a specific page (5 items per page)."""
    limit = page * 5
    search_url = f"ytsearch{limit}:{query}"
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'extract_flat': True,
        'extractor_args': get_youtube_extractor_args(),
        'js_runtimes': {'node': {}},
        **get_ydl_cookie_opts(search_url),
        **get_site_specific_opts(search_url),
    }
    proxy_url = get_proxy_url()
    if proxy_url:
        ydl_opts["proxy"] = proxy_url
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(search_url, download=False)
        entries = info.get('entries', [])
        start_idx = (page - 1) * 5
        return entries[start_idx:start_idx + 5]

async def render_search_page(message, query, page, query_uuid, edit=False):
    """Renders search results page with 5 downloads and next/prev buttons."""
    loop = asyncio.get_running_loop()
    try:
        entries = await loop.run_in_executor(None, lambda: run_youtube_search(query, page))
        if not entries:
            if edit:
                await message.edit_text("❌ No more results found.")
            else:
                await message.reply_text("❌ No results found.")
            return

        text = f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n┃  🔍  *YOUTUBE RESULTS*   ┃\n┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n🔍 *Query:* `{query}`  •  📄 *Page:* `{page}`\n\n"
        keyboard = []
        download_buttons = []
        
        for idx, entry in enumerate(entries):
            global_idx = (page - 1) * 5 + idx + 1
            title = entry.get('title', 'Video')
            # Build proper YouTube watch URL from id or url field
            video_url = entry.get('url') or entry.get('webpage_url') or ''
            video_id = entry.get('id', '')
            if video_url and not video_url.startswith('http'):
                video_url = f"https://www.youtube.com/watch?v={video_url}"
            if not video_url and video_id:
                video_url = f"https://www.youtube.com/watch?v={video_id}"
            if not video_url:
                continue
            title_display = title[:50] + ('...' if len(title) > 50 else '')
            text += f"  {global_idx}️⃣  *{title_display}*\n          └─ `{video_url}`\n\n"

            url_id = uuid.uuid4().hex[:8]
            URL_CACHE[url_id] = {
                'url': video_url,
                'title': title,
                'thumbnail': entry.get('thumbnail'),
                'duration': entry.get('duration', 0)
            }
            download_buttons.append(InlineKeyboardButton(f"📥 #{global_idx}", callback_data=f"opt:{url_id}"))

        # Add downloads to keyboard
        keyboard.append(download_buttons[:3])
        if len(download_buttons) > 3:
            keyboard.append(download_buttons[3:])

        # Add pagination buttons
        pagination_row = []
        if page > 1:
            pagination_row.append(InlineKeyboardButton("◀️ Prev (قبلی)", callback_data=f"src:{page - 1}:{query_uuid}"))
        pagination_row.append(InlineKeyboardButton("▶️ Next (بعدی)", callback_data=f"src:{page + 1}:{query_uuid}"))
        keyboard.append(pagination_row)

        if edit:
            await message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        else:
            await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Search failed: {e}")
        err_msg = f"❌ *Search failed:* `{str(e)[:150]}`"
        if edit:
            await message.edit_text(err_msg, parse_mode="Markdown")
        else:
            await message.reply_text(err_msg, parse_mode="Markdown")

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Searches YouTube and returns top 5 results with inline select buttons and pagination."""
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("❌ Please specify query: `/search coldplay`")
        return

    status_msg = await update.message.reply_text("🔍 *Searching YouTube... / در حال جستجو*", parse_mode="Markdown")
    query_uuid = uuid.uuid4().hex[:8]
    URL_CACHE[query_uuid] = {"query": query}
    
    await status_msg.delete()
    await render_search_page(update.message, query, 1, query_uuid, edit=False)

async def run_pornhub_search(query, page=1):
    """Search Pornhub via yt-dlp (most reliable) with HTML scraping as fallback."""
    import urllib.parse
    loop = asyncio.get_running_loop()  # Must use get_running_loop() in async context
    per_page = 5
    offset = (page - 1) * per_page

    # Primary: yt-dlp search on PornHub search URL
    try:
        search_url = f"https://www.pornhub.com/video/search?search={urllib.parse.quote(query)}&page={((page-1)//4)+1}"

        def _ytdlp_ph_search():
            opts = {
                'quiet': True,
                'no_warnings': True,
                'extract_flat': True,
                'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                },
                **get_ydl_cookie_opts(search_url),
            }
            proxy_url = get_proxy_url()
            if proxy_url:
                opts["proxy"] = proxy_url
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(search_url, download=False)
                return info.get('entries', []) if info else []

        entries = await loop.run_in_executor(None, _ytdlp_ph_search)
        slice_offset = ((page - 1) % 4) * per_page
        entries = entries[slice_offset:slice_offset + per_page]

        if entries:
            items = []
            for e in entries:
                items.append({
                    'vkey': e.get('id', ''),
                    'title': e.get('title', 'Pornhub Video'),
                    'thumbnail': e.get('thumbnail', ''),
                    'preview_url': None,
                    'duration': str(int(e.get('duration', 0) or 0)) + 's' if e.get('duration') else '00:00',
                    'url': e.get('url') or e.get('webpage_url') or f"https://www.pornhub.com/view_video.php?viewkey={e.get('id','')}",
                })
            return items
    except Exception as e:
        logger.warning(f"yt-dlp PH search failed, falling back to HTML scraping: {e}")

    # Fallback: HTML scraping
    import urllib.parse as _up
    url = f"https://www.pornhub.com/video/search?search={_up.quote(query)}&page={page}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://www.pornhub.com/'
    }
    proxy_url = get_proxy_url()
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15), proxy=proxy_url) as r:
                if r.status != 200:
                    return []
                html = await r.text()
        except Exception as e:
            logger.error(f"Pornhub HTML scrape failed: {e}")
            return []

    vkeys = re.findall(r'data-video-vkey="([^"]+)"', html)
    items = []
    seen = set()
    for vk in vkeys:
        if vk in seen or len(items) >= per_page:
            continue
        seen.add(vk)
        idx = html.find(f'data-video-vkey="{vk}"')
        if idx == -1:
            continue
        sub = html[idx:idx + 2500]
        title_match = re.search(r'title="([^"<>]{3,100})"', sub)
        title = title_match.group(1) if title_match else "Pornhub Video"
        preview_match = re.search(r'data-mediabook="([^"]+)"', sub)
        preview_url = preview_match.group(1) if preview_match else None
        img_match = (re.search(r'data-medium-img="([^"]+)"', sub) or
                     re.search(r'data-thumb="([^"]+)"', sub))
        thumbnail_url = img_match.group(1) if img_match else ""
        dur_match = (re.search(r'<var class="duration">([^<]+)</var>', sub) or
                     re.search(r'<span class="duration">([^<]+)</span>', sub))
        duration = dur_match.group(1).strip() if dur_match else "00:00"
        items.append({
            'vkey': vk, 'title': title, 'thumbnail': thumbnail_url,
            'preview_url': preview_url, 'duration': duration,
            'url': f"https://www.pornhub.com/view_video.php?viewkey={vk}"
        })
    return items

async def render_phsearch_page(message, query, item_index, query_uuid, edit=False):
    import telegram
    try:
        fetch_page = ((item_index - 1) // 5) + 1
        entries = await run_pornhub_search(query, fetch_page)

        if not entries:
            err_msg = "❌ No results found." if item_index == 1 else "❌ No more results."
            if edit:
                try:
                    await message.edit_caption(err_msg)
                except Exception:
                    await message.edit_text(err_msg)
            else:
                await message.reply_text(err_msg)
            return

        local_index = (item_index - 1) % 5
        if local_index >= len(entries):
            err_msg = "❌ No more results."
            if edit:
                try:
                    await message.edit_caption(err_msg)
                except Exception:
                    await message.edit_text(err_msg)
            else:
                await message.reply_text(err_msg)
            return

        entry = entries[local_index]
        title = entry.get('title', 'Pornhub Video')
        duration = entry.get('duration', '00:00')
        url = entry.get('url', '')
        thumbnail = entry.get('thumbnail', '')

        caption = (
            f"🔞 *PORNHUB SEARCH / جستجوی پورن‌هاب*\n"
            f"{DIVIDER}\n"
            f"🎬 *{title}*\n"
            f"⏱ Duration: `{duration}`\n"
            f"🔢 Result: `{item_index}`\n\n"
            f"👇 Choose an action below:"
        )

        url_id = uuid.uuid4().hex[:8]
        URL_CACHE[url_id] = {
            'url': url,
            'title': title,
            'thumbnail': thumbnail,
            'preview_url': entry.get('preview_url'),
            'duration': parse_time_str(str(duration)) or 0,
            'is_ph': True
        }

        keyboard = []
        row = []
        if entry.get('preview_url'):
            row.append(InlineKeyboardButton("🎬 Send GIF Preview", callback_data=f"phprev:{url_id}"))
        row.append(InlineKeyboardButton("📥 Download Media", callback_data=f"opt:{url_id}"))
        keyboard.append(row)

        pagination_row = []
        if item_index > 1:
            pagination_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"phsrc:{item_index - 1}:{query_uuid}"))
        pagination_row.append(InlineKeyboardButton("▶️ Next", callback_data=f"phsrc:{item_index + 1}:{query_uuid}"))
        keyboard.append(pagination_row)
        reply_markup = InlineKeyboardMarkup(keyboard)

        if edit:
            if thumbnail:
                try:
                    await message.edit_media(
                        media=telegram.InputMediaPhoto(media=thumbnail, caption=caption, parse_mode="Markdown"),
                        reply_markup=reply_markup
                    )
                except telegram.error.BadRequest as e:
                    if "There is no media in the message to edit" in str(e) or "Message is not modified" in str(e):
                        await message.delete()
                        await message.reply_photo(photo=thumbnail, caption=caption, reply_markup=reply_markup, parse_mode="Markdown")
                    else:
                        raise e
            else:
                try:
                    await message.edit_text(caption, reply_markup=reply_markup, parse_mode="Markdown")
                except Exception:
                    pass
        else:
            if thumbnail:
                await message.reply_photo(photo=thumbnail, caption=caption, reply_markup=reply_markup, parse_mode="Markdown")
            else:
                await message.reply_text(caption, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Pornhub search rendering failed: {e}", exc_info=True)
        err_msg = f"❌ *Search failed:* `{str(e)[:150]}`"
        if edit:
            try:
                await message.edit_caption(err_msg)
            except Exception:
                await message.edit_text(err_msg)
        else:
            await message.reply_text(err_msg, parse_mode="Markdown")

async def phsearch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Searches Pornhub and returns top results with preview and download options."""
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("❌ Please specify query: `/phsearch milf`")
        return
        
    status_msg = await update.message.reply_text("🔍 *Searching Pornhub... / در حال جستجو*", parse_mode="Markdown")
    query_uuid = uuid.uuid4().hex[:8]
    URL_CACHE[query_uuid] = {"query": query}
    
    await status_msg.delete()
    await render_phsearch_page(update.message, query, 1, query_uuid, edit=False)

async def send_ph_preview(bot, chat_id, preview_url, title, reply_to_message_id):
    try:
        await bot.send_video(
            chat_id=chat_id,
            video=preview_url,
            caption=f"🎬 *Preview:* `{title}`",
            parse_mode="Markdown",
            reply_to_message_id=reply_to_message_id
        )
    except Exception as e:
        logger.warning(f"Direct URL preview send failed, trying local download: {e}")
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_file = os.path.join(temp_dir, "preview.mp4")
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                }
                proxy_url = get_proxy_url()
                async with aiohttp.ClientSession(headers=headers) as session:
                    async with session.get(preview_url, proxy=proxy_url) as resp:
                        if resp.status == 200:
                            with open(temp_file, "wb") as f:
                                f.write(await resp.read())
                with open(temp_file, "rb") as f:
                    await bot.send_video(
                        chat_id=chat_id,
                        video=f,
                        caption=f"🎬 *Preview:* `{title}`",
                        parse_mode="Markdown",
                        reply_to_message_id=reply_to_message_id
                    )
        except Exception as ex:
            logger.error(f"Failed to send preview locally: {ex}")
            await bot.send_message(chat_id=chat_id, text="❌ Failed to load preview video.")

# =====================================================================
# GitHub & GitLab Downloader Utility Functions
# =====================================================================
def parse_git_url(url):
    from urllib.parse import urlparse
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    parts = [p for p in path.split("/") if p]
    
    is_github = "github.com" in parsed.netloc.lower()
    is_gitlab = "gitlab.com" in parsed.netloc.lower() or "gitlab" in parsed.netloc.lower()
    
    if not is_github and not is_gitlab:
        return None
        
    platform = "github" if is_github else "gitlab"
    
    if len(parts) < 2:
        return None
        
    owner = parts[0]
    repo = parts[1]
    
    result = {
        'platform': platform,
        'owner': owner,
        'repo': repo,
        'type': 'repo',
        'branch': None,
        'path': None,
        'tag': None
    }
    
    if is_github:
        if len(parts) >= 4:
            action = parts[2]
            if action == "blob":
                result['type'] = 'file'
                result['branch'] = parts[3]
                result['path'] = "/".join(parts[4:])
            elif action == "tree":
                result['type'] = 'folder'
                result['branch'] = parts[3]
                result['path'] = "/".join(parts[4:])
            elif action in ("releases", "tags"):
                if len(parts) >= 5 and parts[3] == "tag":
                    result['type'] = 'release_tag'
                    result['tag'] = parts[4]
                else:
                    result['type'] = 'releases'
    else:
        if "-" in parts:
            try:
                dash_idx = parts.index("-")
                if len(parts) > dash_idx + 2:
                    action = parts[dash_idx + 1]
                    if action == "blob":
                        result['type'] = 'file'
                        result['branch'] = parts[dash_idx + 2]
                        result['path'] = "/".join(parts[dash_idx + 3:])
                    elif action == "tree":
                        result['type'] = 'folder'
                        result['branch'] = parts[dash_idx + 2]
                        result['path'] = "/".join(parts[dash_idx + 3:])
                    elif action in ("releases", "tags"):
                        result['type'] = 'releases'
            except ValueError:
                pass
                
    return result

async def download_github_folder_recursive(session, owner, repo, path, branch, local_dir, token=None):
    import aiofiles
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"token {token}"
    
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    params = {}
    if branch:
        params["ref"] = branch
        
    proxy_url = get_proxy_url()
    async with session.get(url, headers=headers, params=params, proxy=proxy_url) as resp:
        if resp.status != 200:
            raise Exception(f"GitHub API error: {resp.status}")
        items = await resp.json()
        
    for item in items:
        item_name = item["name"]
        item_type = item["type"]
        item_path = item["path"]
        
        local_item_path = os.path.join(local_dir, item_name)
        
        if item_type == "dir":
            os.makedirs(local_item_path, exist_ok=True)
            await download_github_folder_recursive(session, owner, repo, item_path, branch, local_item_path, token)
        elif item_type == "file":
            download_url = item["download_url"]
            raw_headers = {}
            if token:
                raw_headers["Authorization"] = f"token {token}"
            proxy_url = get_proxy_url()
            async with session.get(download_url, headers=raw_headers, proxy=proxy_url) as file_resp:
                if file_resp.status == 200:
                    async with aiofiles.open(local_item_path, "wb") as f:
                        await f.write(await file_resp.read())

async def download_gitlab_folder_recursive(session, owner, repo, path, branch, local_dir, token=None):
    import urllib.parse
    import aiofiles
    project_id = urllib.parse.quote_plus(f"{owner}/{repo}")
    url = f"https://gitlab.com/api/v4/projects/{project_id}/repository/tree"
    headers = {}
    if token:
        headers["PRIVATE-TOKEN"] = token
        
    params = {"path": path, "recursive": True}
    if branch:
        params["ref"] = branch
        
    proxy_url = get_proxy_url()
    async with session.get(url, headers=headers, params=params, proxy=proxy_url) as resp:
        if resp.status != 200:
            raise Exception(f"GitLab API error: {resp.status}")
        items = await resp.json()
        
    for item in items:
        item_type = item["type"]
        item_path = item["path"]
        
        rel_path = os.path.relpath(item_path, path)
        local_item_path = os.path.join(local_dir, rel_path)
        
        if item_type == "tree":
            os.makedirs(local_item_path, exist_ok=True)
        elif item_type == "blob":
            os.makedirs(os.path.dirname(local_item_path), exist_ok=True)
            enc_path = urllib.parse.quote_plus(item_path)
            file_url = f"https://gitlab.com/api/v4/projects/{project_id}/repository/files/{enc_path}/raw"
            file_params = {}
            if branch:
                file_params["ref"] = branch
            async with session.get(file_url, headers=headers, params=file_params, proxy=proxy_url) as file_resp:
                if file_resp.status == 200:
                    async with aiofiles.open(local_item_path, "wb") as f:
                        await f.write(await file_resp.read())

async def fetch_git_branches(platform, owner, repo, token=None):
    """Fetches branches from GitHub or GitLab API."""
    headers = {"User-Agent": "Mozilla/5.0"}
    proxy_url = get_proxy_url()
    
    if platform == "github":
        headers["Accept"] = "application/vnd.github.v3+json"
        if token:
            headers["Authorization"] = f"token {token}"
        url = f"https://api.github.com/repos/{owner}/{repo}/branches"
    else:
        if token:
            headers["PRIVATE-TOKEN"] = token
        import urllib.parse
        project_id = urllib.parse.quote_plus(f"{owner}/{repo}")
        url = f"https://gitlab.com/api/v4/projects/{project_id}/repository/branches"
        
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, proxy=proxy_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    branches_data = await resp.json()
                    return [b.get("name") for b in branches_data]
    except Exception as e:
        logger.error(f"Failed to fetch branches: {e}")
    return []

async def render_git_explorer(message, url_id, user_id, edit=True):
    """Renders the interactive explorer keyboard for Git repositories."""
    cached = URL_CACHE.get(url_id)
    if not cached:
        return
        
    platform = cached['platform']
    owner = cached['owner']
    repo = cached['repo']
    branch = cached.get('branch')
    path = cached.get('path', "")
    page = cached.get('page', 0)
    
    gh_token, gl_token = db.get_tokens(user_id)
    token = gh_token if platform == "github" else gl_token
    
    # 1. Fetch default branch if None
    if not branch:
        default_branch = "main"
        headers = {"User-Agent": "Mozilla/5.0"}
        proxy_url = get_proxy_url()
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                if platform == "github":
                    headers["Accept"] = "application/vnd.github.v3+json"
                    if token:
                        headers["Authorization"] = f"token {token}"
                    url = f"https://api.github.com/repos/{owner}/{repo}"
                    async with session.get(url, proxy=proxy_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status == 200:
                            repo_info = await r.json()
                            default_branch = repo_info.get("default_branch", "main")
                else:
                    if token:
                        headers["PRIVATE-TOKEN"] = token
                    import urllib.parse
                    project_id = urllib.parse.quote_plus(f"{owner}/{repo}")
                    url = f"https://gitlab.com/api/v4/projects/{project_id}"
                    async with session.get(url, proxy=proxy_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status == 200:
                            repo_info = await r.json()
                            default_branch = repo_info.get("default_branch", "master")
        except Exception as e:
            logger.error(f"Failed to fetch repo default branch: {e}")
        branch = default_branch
        cached['branch'] = branch

    # 2. Fetch contents of current path
    items = []
    error_msg = None
    headers = {"User-Agent": "Mozilla/5.0"}
    proxy_url = get_proxy_url()
    
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            if platform == "github":
                headers["Accept"] = "application/vnd.github.v3+json"
                if token:
                    headers["Authorization"] = f"token {token}"
                url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
                params = {"ref": branch}
                async with session.get(url, params=params, proxy=proxy_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, dict):
                            data = [data]
                        for item in data:
                            items.append({
                                'name': item.get('name'),
                                'type': item.get('type'), # 'dir' or 'file'
                                'path': item.get('path'),
                                'download_url': item.get('download_url')
                            })
                    else:
                        error_msg = f"HTTP {resp.status}"
            else:
                if token:
                    headers["PRIVATE-TOKEN"] = token
                import urllib.parse
                project_id = urllib.parse.quote_plus(f"{owner}/{repo}")
                url = f"https://gitlab.com/api/v4/projects/{project_id}/repository/tree"
                params = {"path": path, "ref": branch}
                async with session.get(url, params=params, proxy=proxy_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for item in data:
                            item_type = 'dir' if item.get('type') == 'tree' else 'file'
                            items.append({
                                'name': item.get('name'),
                                'type': item_type,
                                'path': item.get('path'),
                                'download_url': None
                            })
                    else:
                        error_msg = f"HTTP {resp.status}"
    except Exception as e:
        logger.error(f"Failed to fetch contents: {e}")
        error_msg = str(e)

    items.sort(key=lambda x: (0 if x['type'] == 'dir' else 1, x['name'].lower()))
    cached['items'] = items

    text = (
        f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
        f"┃  🐙  *GIT EXPLORER*       ┃\n"
        f"┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
        f"🌐 `{platform.upper()}`  •  📦 `{owner}/{repo}`\n"
        f"🌿 Branch: `{branch}`  •  📁 `/{path}`"
    )
    
    if error_msg:
        text += f"\n❌ *Error loading contents:* `{error_msg}`\n"
        keyboard = [
            [InlineKeyboardButton("🌿 Change Branch", callback_data=f"gitnav:{url_id}:branches")],
            [InlineKeyboardButton("❌ Close Explorer", callback_data=f"gitnav:{url_id}:cancel")]
        ]
    else:
        ITEMS_PER_PAGE = 7
        total_items = len(items)
        total_pages = max(1, (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
        if page >= total_pages:
            page = 0
            cached['page'] = 0
            
        start_idx = page * ITEMS_PER_PAGE
        end_idx = start_idx + ITEMS_PER_PAGE
        page_items = items[start_idx:end_idx]
        
        keyboard = []
        
        if path:
            keyboard.append([InlineKeyboardButton("⬆️ .. (Go Up / برگشت به پوشه قبل)", callback_data=f"gitnav:{url_id}:up")])
            
        for item_idx, item in enumerate(page_items):
            global_idx = start_idx + item_idx
            icon = "📁" if item['type'] == 'dir' else "📄"
            label = f"{icon} {item['name']}"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"gitnav:{url_id}:go:{global_idx}")])
            
        if total_pages > 1:
            pagination_row = []
            if page > 0:
                pagination_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"gitnav:{url_id}:page:prev"))
            pagination_row.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="gitnav:noop"))
            if page < total_pages - 1:
                pagination_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"gitnav:{url_id}:page:next"))
            keyboard.append(pagination_row)
            
        action_label = "📥 Download Folder (.zip)" if path else "📦 Download Repository (.zip)"
        keyboard.append([
            InlineKeyboardButton(action_label, callback_data=f"gitnav:{url_id}:dlzip"),
            InlineKeyboardButton("🏷 Releases", callback_data=f"gitnav:{url_id}:releases")
        ])
        keyboard.append([
            InlineKeyboardButton("🌿 Branches", callback_data=f"gitnav:{url_id}:branches"),
            InlineKeyboardButton("❌ Close", callback_data=f"gitnav:{url_id}:cancel")
        ])
        
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if edit:
        try:
            await message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        except Exception:
            pass
    else:
        await message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

async def handle_git_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, git_info):
    message = update.message
    user_id = update.effective_user.id
    
    url_id = uuid.uuid4().hex[:8]
    URL_CACHE[url_id] = {
        'is_git': True,
        'git_info': git_info,
        'platform': git_info['platform'],
        'owner': git_info['owner'],
        'repo': git_info['repo'],
        'branch': git_info['branch'],
        'path': git_info['path'] or "",
        'page': 0
    }
    
    await render_git_explorer(message, url_id, user_id, edit=False)

async def github_token_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    token = " ".join(context.args).strip()
    if not token:
        await update.message.reply_text("❌ Please specify your GitHub Personal Access Token:\n`/github_token ghp_xxxx` (or `/github_token clear` to delete)")
        return
    
    if token.lower() == "clear":
        db.set_github_token(user_id, None)
        await update.message.reply_text("🗑 *GitHub Personal Access Token cleared!*", parse_mode="Markdown")
    else:
        db.set_github_token(user_id, token)
        await update.message.reply_text("🔑 *GitHub Personal Access Token saved successfully!*", parse_mode="Markdown")

async def gitlab_token_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    token = " ".join(context.args).strip()
    if not token:
        await update.message.reply_text("❌ Please specify your GitLab Personal Access Token:\n`/gitlab_token glpat-xxxx` (or `/gitlab_token clear` to delete)")
        return
    
    if token.lower() == "clear":
        db.set_gitlab_token(user_id, None)
        await update.message.reply_text("🗑 *GitLab Personal Access Token cleared!*", parse_mode="Markdown")
    else:
        db.set_gitlab_token(user_id, token)
        await update.message.reply_text("🔑 *GitLab Personal Access Token saved successfully!*", parse_mode="Markdown")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays personal statistics, or global statistics if requested by Admin."""
    user_id = update.effective_user.id
    count, total_size = db.get_user_stats(user_id)
    size_mb = total_size / (1024 * 1024)
    
    text = (
        f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
        f"┃  📊  *STATISTICS*         ┃\n"
        f"┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
        f"👤 *Your Usage:*\n"
        f"  ├─ Files Processed: `{count}`\n"
        f"  └─ Bandwidth Used: `{size_mb:.2f} MB`"
    )
    
    # Check if user is Admin and requested global stats
    if ADMIN_USER_ID and str(user_id) == str(ADMIN_USER_ID):
        g_count, g_size, g_users = db.get_global_stats()
        g_size_gb = g_size / (1024 * 1024 * 1024)
        text += (
            f"\n\n⚙️ *Global System Stats:*\n"
            f"  ├─ Users: `{g_users}`\n"
            f"  ├─ Files: `{g_count}`\n"
            f"  └─ Bandwidth: `{g_size_gb:.2f} GB`"
        )
        
    await update.message.reply_text(text, parse_mode="Markdown")

async def query_gemini(prompt: str, file_data: dict | None = None) -> str:
    import aiohttp
    import os
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return "⚠️ AI Error: GEMINI_API_KEY is not set in .env file."
    
    models = [
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite-preview-06-17",
        "gemini-2.0-flash",
        "gemini-1.5-flash",
    ]
    
    proxy_url = get_proxy_url()
    
    parts = []
    if prompt:
        parts.append({"text": prompt})
    if file_data:
        parts.append({
            "inlineData": {
                "mimeType": file_data["mime_type"],
                "data": file_data["data"]
            }
        })
        
    if not parts:
        return "⚠️ AI Error: No text or file provided."
        
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "maxOutputTokens": 8192,
            "temperature": 0.7
        }
    }
    
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, proxy=proxy_url, 
                                       timeout=aiohttp.ClientTimeout(total=120, connect=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        candidates = data.get("candidates", [])
                        if candidates:
                            parts_out = candidates[0].get("content", {}).get("parts", [])
                            text = "".join(p.get("text", "") for p in parts_out)
                            if text:
                                logger.info(f"Gemini success with model {model}")
                                return text
                    else:
                        error_text = await resp.text()
                        logger.warning(f"Gemini model {model} failed: {resp.status} - {error_text[:150]}")
        except asyncio.TimeoutError:
            logger.warning(f"Gemini model {model} timed out")
        except Exception as e:
            logger.warning(f"Gemini model {model} exception: {e}")
    
    return "⚠️ AI Error: All Gemini models failed. Check your API key."

async def query_aerolink(prompt: str, file_data: dict | None = None) -> str:
    import aiohttp
    import os
    api_key = os.getenv("AEROLINK_API_KEY", "aero_live_Th0c_y4qggPiffUZK0Gt0NyNUFCVnKETnA15UleEN4Q")
    if not api_key:
        return "⚠️ AI Error: AEROLINK_API_KEY is not set."
    
    models = [
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
    ]
    
    proxy_url = get_proxy_url()
    url = "https://conduit.ozdoev.net/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json"
    }
    
    content = []
    if file_data:
        mime = file_data["mime_type"]
        if mime.startswith("image/"):
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime,
                    "data": file_data["data"]
                }
            })
        elif mime == "application/pdf":
            content.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": mime,
                    "data": file_data["data"]
                }
            })
            
    if prompt:
        content.append({
            "type": "text",
            "text": prompt
        })
        
    if not content:
        return "⚠️ AI Error: No text or file provided."
        
    for model in models:
        payload = {
            "model": model,
            "max_tokens": 8192,
            "messages": [{"role": "user", "content": content}]
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers, proxy=proxy_url,
                                       timeout=aiohttp.ClientTimeout(total=120, connect=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        content_blocks = data.get("content", [])
                        if content_blocks:
                            text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
                            if text:
                                logger.info(f"Aerolink success with model {model}")
                                return text
                    else:
                        error_text = await resp.text()
                        logger.warning(f"Aerolink model {model} failed: {resp.status} - {error_text[:100]}")
        except asyncio.TimeoutError:
            logger.warning(f"Aerolink model {model} timed out")
        except Exception as e:
            logger.warning(f"Aerolink model {model} exception: {e}")
    
    return "⚠️ AI Error: All Aerolink models failed."

async def query_nvidia(prompt: str, file_data: dict | None = None) -> str:
    import aiohttp
    import os
    api_key = os.getenv("NVIDIA_API_KEY")
    if not api_key:
        return "⚠️ AI Error: NVIDIA_API_KEY is not set."
        
    url = "https://integrate.api.nvidia.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    message_content = []
    if prompt:
        message_content.append({"type": "text", "text": prompt})
    
    if file_data:
        mime = file_data["mime_type"]
        if mime.startswith("image/"):
            message_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime};base64,{file_data['data']}"
                }
            })
        elif mime == "application/pdf":
            message_content.append({
                "type": "text",
                "text": f"[Attached PDF file of type {mime}]"
            })
            
    if len(message_content) == 1 and message_content[0]["type"] == "text":
        messages = [{"role": "user", "content": prompt}]
    else:
        messages = [{"role": "user", "content": message_content}]
        
    payload = {
        "model": "z-ai/glm-5.2",
        "messages": messages,
        "temperature": 0.6,
        "max_tokens": 4096
    }
    
    proxy_url = get_proxy_url()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, proxy=proxy_url,
                                   timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    choices = data.get("choices", [])
                    if choices:
                        text = choices[0].get("message", {}).get("content", "")
                        if text:
                            logger.info("Nvidia NIM success with model z-ai/glm-5.2")
                            return text
                else:
                    error_text = await resp.text()
                    logger.warning(f"Nvidia API failed: {resp.status} - {error_text[:100]}")
    except Exception as e:
        logger.warning(f"Nvidia API exception: {e}")
        
    return "⚠️ AI Error: Nvidia NIM GLM 5.2 model call failed."

# =====================================================================
# Main Message Handler (Inputs & Routing)
# =====================================================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    user_id = update.effective_user.id
    text = message.text.strip()

    # Check for text state inputs
    if user_id in USER_STATES:
        state_info = USER_STATES[user_id]
        state = state_info.get('state')
        
        if state == 'AWAITING_PDF_PROTECT_PASS':
            password = text
            file_uuid = state_info['file_uuid']
            cached = URL_CACHE.get(file_uuid)
            USER_STATES.pop(user_id, None) # Clear state
            
            if not cached:
                await message.reply_text("❌ Session expired. Please send file again.")
                return
            
            # Enqueue protect conversion
            await message.reply_text("⏳ *Adding PDF Protect task to queue...*")
            await add_conversion_to_queue(cached['file_id'], cached['filename'], f"pdfprotect:{password}", message, user_id)
            URL_CACHE.pop(file_uuid, None)
            return

        elif state == 'AWAITING_PDF_UNLOCK_PASS':
            password = text
            file_uuid = state_info['file_uuid']
            cached = URL_CACHE.get(file_uuid)
            USER_STATES.pop(user_id, None) # Clear state
            
            if not cached:
                await message.reply_text("❌ Session expired. Please send file again.")
                return
            
            # Enqueue unlock conversion
            await message.reply_text("⏳ *Adding PDF Unlock task to queue...*")
            await add_conversion_to_queue(cached['file_id'], cached['filename'], f"pdfunlock:{password}", message, user_id)
            URL_CACHE.pop(file_uuid, None)
            return

        elif state == 'AWAITING_TAG_EDIT':
            # Format: Artist - Title - Album (split only on first 2 hyphens to preserve hyphens in names)
            parts = [p.strip() for p in text.split(" - ", 2)]
            artist = parts[0] if len(parts) > 0 else "Unknown"
            title = parts[1] if len(parts) > 1 else "Unknown"
            album = parts[2] if len(parts) > 2 else "Unknown"
            
            file_uuid = state_info['audio_file_uuid']
            cached = URL_CACHE.get(file_uuid)
            USER_STATES.pop(user_id, None) # Clear state
            
            if not cached:
                await message.reply_text("❌ Session expired. Please send file again.")
                return
            
            # Enqueue tag edit
            await message.reply_text("⏳ *Adding music tag editor task to queue...*")
            await add_conversion_to_queue(cached['file_id'], cached['filename'], f"tags:{artist}:{title}:{album}", message, user_id)
            URL_CACHE.pop(file_uuid, None)
            return

    # Handle Bottom Menu Buttons - clear any pending states first
    if text in ("🔍 YouTube", "🎵 Spotify", "🐙 Git", 
                "📥 Direct DL", "🔄 Converter", "🤖 AI Chat",
                "📊 Stats", "❓ Help"):
        # Clear any pending interactive states when menu button is pressed
        if user_id in USER_STATES:
            state = USER_STATES[user_id].get('state', '')
            if state in ('AWAITING_TRIM', 'AWAITING_SUBTITLE', 'AWAITING_TAG_EDIT',
                        'AWAITING_PDF_PROTECT_PASS', 'AWAITING_PDF_UNLOCK_PASS',
                        'AWAITING_PDF_ALBUM_IMAGES'):
                USER_STATES.pop(user_id, None)

    if text == "🔍 YouTube":
        await message.reply_text(
            "┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
            "┃   🔍  *YOUTUBE SEARCH*   ┃\n"
            "┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
            "Search for any video and download in HQ!\n"
            "جستجو و دانلود با کیفیت بالا\n\n"
            f"{THIN_DIVIDER}\n"
            "⌨️ *Usage:*  `/search <query>`\n"
            "📌 *Example:*  `/search interstellar soundtrack`",
            parse_mode="Markdown"
        )
        return
    elif text == "🔞 Pornhub Search":
        await message.reply_text(
            "🔞 *Pornhub Search / جستجوی پورن‌هاب*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Search for adult content securely.\n\n"
            "⌨️ *Usage:* `/phsearch <query>`\n"
            "📌 *Example:* `/phsearch amateur`", 
            parse_mode="Markdown"
        )
        return
    elif text == "🎵 Spotify":
        await message.reply_text(
            "┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
            "┃   🎵  *SPOTIFY DL*       ┃\n"
            "┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
            "Get high-quality MP3s from Spotify!\n"
            "دانلود با کیفیت بالا از اسپاتیفای\n\n"
            f"{THIN_DIVIDER}\n"
            "📌 _Just paste the Spotify Track Link_\n"
            "I will fetch metadata, download the song,\n"
            "and attach the original album cover!\n\n"
            f"`https://open.spotify.com/track/4PTG3Z6ehGkBF3zI7YkR5C`",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
        return
    elif text == "🐙 Git":
        await message.reply_text(
            "┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
            "┃   🐙  *GIT DOWNLOADER*   ┃\n"
            "┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
            "Download repos, folders, files & releases\n"
            "from GitHub & GitLab.\n\n"
            f"{THIN_DIVIDER}\n"
            "💡 _Send any repo, folder, or file URL_\n\n"
            "📝 *Examples:*\n"
            "  Repo: `https://github.com/owner/repo`\n"
            "  Folder: `.../repo/tree/main/src`\n\n"
            "🔑 *Private Repos:*\n"
            "  `/github_token <token>`\n"
            "  `/gitlab_token <token>`",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
        return
    elif text == "📥 Direct DL":
        await message.reply_text(
            "┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
            "┃  📥  *DIRECT DOWNLOADER*  ┃\n"
            "┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
            "Download any file from the web.\n"
            "دانلود مستقیم فایل از وب\n\n"
            f"{THIN_DIVIDER}\n"
            "💡 _Send any direct file link_\n"
            "   (`.zip`, `.mp4`, `.pdf`, etc.)\n\n"
            "⌨️ *Command:*\n"
            "`/direct <url> [--name file.ext]`\n\n"
            "📌 *Example:*\n"
            "`/direct https://example.com/data --name info.zip`",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
        return
    elif text == "🔄 Converter":
        await message.reply_text(
            "┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
            "┃  🔄  *FILE CONVERTER*     ┃\n"
            "┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
            "Convert almost anything! Just send a file.\n"
            "تبدیل فرمت فایل‌ها — فقط فایل را بفرستید\n\n"
            f"{THIN_DIVIDER}\n"
            "🎨 *Images:*  PNG, JPG, WebP, PDF\n"
            "🎧 *Audio:*  MP3, WAV, OGG, Voice Effects\n"
            "🎥 *Video:*  Extract MP3, GIF, Compress\n"
            "📄 *Docs:*  DOCX→PDF, PDF→Text, Lock/Unlock PDF",
            parse_mode="Markdown"
        )
        return
    elif text == "🤖 AI Chat":
        current_engine = USER_STATES.get(user_id, {}).get("ai_engine")
        if current_engine:
            engine_names = {"gemini": "✨ Gemini", "aerolink": "🚀 Aerolink AI", "nvidia": "🟢 Nvidia GLM 5.2"}
            engine_name = engine_names.get(current_engine, "AI")
            keyboard = [
                [InlineKeyboardButton(f"✅ Currently: {engine_name}", callback_data="ainoop")],
                [InlineKeyboardButton("🛑 Stop AI", callback_data="ai_stop")]
            ]
            await message.reply_text(
                "┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                "┃   🤖  *AI ASSISTANT*      ┃\n"
                "┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                f"AI is *active* with: *{engine_name}*\n"
                f"هوش مصنوعی فعال است — هر پیام متنی پاسخ داده می‌شود\n\n"
                f"🛑 *برای غیرفعال کردن، دکمه زیر را بزنید*\n"
                f"{THIN_DIVIDER}",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        else:
            keyboard = [
                [
                    InlineKeyboardButton("✨ Gemini", callback_data="ai_engine:gemini"),
                    InlineKeyboardButton("🚀 Aerolink AI", callback_data="ai_engine:aerolink")
                ],
                [
                    InlineKeyboardButton("🟢 Nvidia GLM 5.2", callback_data="ai_engine:nvidia")
                ]
            ]
            await message.reply_text(
                "┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                "┃   🤖  *AI ASSISTANT*      ┃\n"
                "┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                "Choose your AI engine below.\n"
                "موتور هوش مصنوعی خود را انتخاب کنید:\n\n"
                f"{THIN_DIVIDER}",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        return
    elif text == "📊 Stats":
        await stats_command(update, context)
        return
    elif text == "❓ Help":
        await start_command(update, context)
        return

    # Trimming State Check
    if user_id in USER_STATES and USER_STATES[user_id].get('state') == 'AWAITING_TRIM':
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
                InlineKeyboardButton("🎥 Video Segment", callback_data=f"dmethod:{url_id}:video"),
                InlineKeyboardButton("🎵 Audio (MP3) Segment", callback_data=f"dmethod:{url_id}:audio")
            ],
            [InlineKeyboardButton("❌ Cancel (لغو)", callback_data=f"dl:{url_id}:cancel")]
        ]
        
        # Add GIF option if duration <= 15s
        if (end_sec - start_sec) <= 15:
            keyboard[0].append(InlineKeyboardButton("🖼 Convert to GIF", callback_data=f"dmethod:{url_id}:gif"))

        await message.reply_text(
            f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
            f"┃  ✂️  *TRIM SELECTED*       ┃\n"
            f"┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
            f"⏱ `{duration_str}`\n"
            f"🎥 `{cached['title']}`\n\n"
            f"👇 _Choose format:_",
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

    # Extract URL safely without UTF-16 offset bugs
    urls = []
    
    # 1. Check for text_link (inline hyperlinks)
    for entity in message.entities or []:
        if entity.type == "text_link":
            urls.append(entity.url)
            
    # 2. Robust Regex for explicit URLs in text (avoids offset issues with emojis)
    url_matches = re.findall(r"(?:https?://|www\.)?[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}[^\s]*", message.text)
    for match in url_matches:
        if match not in urls:
            urls.append(match)
            
    # 3. Fallback for raw domain inputs like "youtube.com/watch?v=..."
    if not urls and re.match(r"^[a-zA-Z0-9-]+\.[a-zA-Z]{2,}/[^\s]+", text):
        urls.append("https://" + text)

    if not urls:
        engine = USER_STATES.get(user_id, {}).get("ai_engine")
        
        if engine:
            engine_names = {
                "gemini": "Gemini",
                "aerolink": "Aerolink AI",
                "nvidia": "Nvidia GLM 5.2"
            }
            engine_name = engine_names.get(engine, "AI")
            
            status_msg = await message.reply_text(f"🤖 *Thinking ({engine_name})... / در حال پردازش*", parse_mode="Markdown")

            # Keep typing indicator alive during heavy AI tasks
            stop_typing = asyncio.Event()
            async def keep_typing():
                while not stop_typing.is_set():
                    try:
                        await context.bot.send_chat_action(chat_id=message.chat_id, action="typing")
                    except Exception:
                        pass
                    await asyncio.sleep(4)
            typing_task = asyncio.create_task(keep_typing())

            try:
                # Route to the correct AI engine
                if engine == "gemini":
                    response = await query_gemini(text)
                elif engine == "aerolink":
                    response = await query_aerolink(text)
                elif engine == "nvidia":
                    response = await query_nvidia(text)
                else:
                    response = await query_gemini(text)
            except asyncio.TimeoutError:
                response = "⚠️ AI Error: Request timed out. Try a shorter prompt or try again later."
            except Exception as e:
                response = f"⚠️ AI Error: {str(e)[:200]}"
            finally:
                stop_typing.set()
                typing_task.cancel()
            
            # Format Markdown to be more compatible with Telegram's legacy Markdown
            formatted_text = response
            formatted_text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', formatted_text, flags=re.DOTALL)
            formatted_text = re.sub(r'^###\s+(.+)$', r'*\1*', formatted_text, flags=re.MULTILINE)
            formatted_text = re.sub(r'^##\s+(.+)$', r'*\1*', formatted_text, flags=re.MULTILINE)
            formatted_text = re.sub(r'^#\s+(.+)$', r'*\1*', formatted_text, flags=re.MULTILINE)
            
            header = f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n┃  ✨  *{engine_name}*          ┃\n┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
            MAX_TG_LEN = 4000

            async def send_ai_response(text_body: str, use_markdown: bool = True):
                """Send long AI response split into chunks if needed."""
                chunks = [text_body[i:i + MAX_TG_LEN] for i in range(0, len(text_body), MAX_TG_LEN)]
                first = True
                for chunk in chunks:
                    if first:
                        msg_text = header + chunk
                        first = False
                    else:
                        msg_text = chunk
                    try:
                        if first is False and chunk == chunks[0]:
                            # Edit first message
                            if use_markdown:
                                await status_msg.edit_text(msg_text, parse_mode="Markdown")
                            else:
                                await status_msg.edit_text(msg_text)
                        else:
                            if use_markdown:
                                await message.reply_text(msg_text, parse_mode="Markdown")
                            else:
                                await message.reply_text(msg_text)
                    except Exception:
                        await message.reply_text(msg_text)

            # Split long responses into multiple messages
            all_chunks = [formatted_text[i:i + MAX_TG_LEN] for i in range(0, max(1, len(formatted_text)), MAX_TG_LEN)]
            try:
                first_chunk = header + all_chunks[0]
                await status_msg.edit_text(first_chunk, parse_mode="Markdown")
                for chunk in all_chunks[1:]:
                    await message.reply_text(chunk, parse_mode="Markdown")
            except Exception:
                # Fallback: plain text
                try:
                    plain_header = f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n┃  ✨  {engine_name}          ┃\n┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                    plain_chunks = [response[i:i + MAX_TG_LEN] for i in range(0, max(1, len(response)), MAX_TG_LEN)]
                    await status_msg.edit_text(plain_header + plain_chunks[0])
                    for chunk in plain_chunks[1:]:
                        await message.reply_text(chunk)
                except Exception:
                    pass
            return

    raw_url = urls[0]
    if not raw_url.startswith(("http://", "https://")):
        raw_url = "https://" + raw_url
        
    # Bypass redirects (Link Shorteners)
    status_msg = await message.reply_text("🔍 *Resolving link redirects... / در حال بررسی لینک*", parse_mode="Markdown")
    url = await bypass_url(raw_url)

    # Check if URL is GitHub or GitLab
    git_info = parse_git_url(url)
    if git_info:
        await status_msg.delete()
        await handle_git_flow(update, context, git_info)
        return

    # Check if URL is Spotify
    is_spotify = "spotify.com/track/" in url.lower() or "spotify.link" in url.lower()
    if is_spotify:
        await status_msg.edit_text("🎵 *Extracting Spotify metadata... / دریافت اطلاعات اسپاتیفای*", parse_mode="Markdown")
        meta = await fetch_spotify_metadata(url)
        if not meta or not meta.get("title"):
            await status_msg.edit_text("❌ *Failed to retrieve Spotify track metadata.*")
            return
            
        track_title = meta["title"]
        track_artist = meta["artist"]
        track_thumb = meta["thumbnail"]
        
        # Build search query: skip placeholder artist so oEmbed-only results still match
        artist_ok = track_artist and track_artist.lower() not in ("unknown artist", "unknown", "")
        search_query = f"{track_artist} - {track_title}" if artist_ok else track_title
        await status_msg.edit_text(f"🔍 *Searching for:* `{search_query}` on YouTube...", parse_mode="Markdown")
        
        # Search YouTube for the track using Invidious (fallback to yt-dlp)
        loop = asyncio.get_running_loop()
        try:
            yt_url = await search_youtube_invidious(search_query)
            
            if not yt_url:
                logger.info("Invidious search failed. Falling back to yt-dlp search...")
                def search_yt_track():
                    queries = [
                        f"ytsearch1:{search_query}",
                        f"ytsearch1:{track_title} official audio",
                        f"ytsearch1:{track_title} lyrics",
                    ]
                    if artist_ok:
                        queries.insert(1, f"ytsearch1:{track_artist} {track_title} official audio")
                        queries.append(f"ytsearch1:{track_title} {track_artist}")
                    for query_fmt in queries:
                        ydl_opts = {
                            'quiet': True,
                            'no_warnings': True,
                            'noplaylist': True,
                            'extract_flat': True,
                            'socket_timeout': 20,
                            'extractor_args': get_youtube_extractor_args(),
                            'js_runtimes': {'node': {}},
                            **get_ydl_cookie_opts(url),
                        }
                        proxy_url = get_proxy_url()
                        if proxy_url:
                            ydl_opts["proxy"] = proxy_url
                        try:
                            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                                info = ydl.extract_info(query_fmt, download=False)
                                entries = info.get('entries', [])
                                if entries:
                                    entry = entries[0]
                                    yt_url = entry.get('url') or entry.get('webpage_url') or ''
                                    video_id = entry.get('id', '')
                                    if yt_url and not yt_url.startswith('http'):
                                        yt_url = f"https://www.youtube.com/watch?v={yt_url}"
                                    if not yt_url and video_id:
                                        yt_url = f"https://www.youtube.com/watch?v={video_id}"
                                    if yt_url:
                                        return yt_url
                        except Exception as e:
                            logger.warning(f"yt-dlp search failed for '{query_fmt}': {e}")
                    return None
                
                yt_url = await loop.run_in_executor(None, search_yt_track)
            if not yt_url:
                await status_msg.edit_text("❌ *Song not found on YouTube search.*")
                return
                
            # Store Spotify info and ask delivery method
            spotify_id = uuid.uuid4().hex[:8]
            URL_CACHE[spotify_id] = {
                'url': yt_url,
                'title': f"{track_artist} - {track_title}",
                'thumbnail': track_thumb,
                'duration': 0,
                'start_time': None,
                'end_time': None,
                'audio_title': track_title,
                'audio_performer': track_artist
            }
            await status_msg.delete()
            keyboard = [
                [
                    InlineKeyboardButton("📱 Send to Telegram", callback_data=f"dl:{spotify_id}:audio:tg"),
                    InlineKeyboardButton("🌐 Upload to uplod.ir", callback_data=f"dl:{spotify_id}:audio:web")
                ],
                [InlineKeyboardButton("❌ Cancel (لغو)", callback_data=f"dl:{spotify_id}:cancel")]
            ]
            await message.reply_text(
                f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                f"┃  🎵  *SPOTIFY TRACK*     ┃\n"
                f"┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                f"🎵 `{track_artist} - {track_title}`\n\n"
                f"How do you want to receive the file?\n"
                f"فایل رو چطوری تحویل بگیرید?\n\n"
                f"{THIN_DIVIDER}",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return
        except Exception as e:
            logger.error(f"Spotify YouTube search failed: {e}")
            await status_msg.edit_text(f"❌ *Error search-matching song:* `{str(e)[:150]}`")
            return

    # Check if we should use yt-dlp to download (e.g. streaming format or video platform)
    direct_file_extensions = (
        ".zip", ".rar", ".7z", ".tar", ".gz", ".apk", ".exe", ".msi", ".dmg", ".pdf", 
        ".epub", ".docx", ".xlsx", ".pptx", ".jpg", ".jpeg", ".png", ".webp", ".gif", 
        ".mp3", ".wav", ".ogg", ".mp4", ".mkv", ".avi", ".mov", ".flv"
    )
    parsed = urlparse(url)
    path = parsed.path.lower()
    
    # Check streaming/playlist extension
    is_streaming = any(ext in path for ext in (".m3u8", ".mpd", ".manifest"))
    
    # Check if known video/audio domains (including generic platforms)
    media_domains = [
        "youtube.com", "youtu.be", "instagram.com", "tiktok.com",
        "twitter.com", "x.com", "facebook.com", "fb.watch", "vimeo.com",
        "pornhub.com", "xvideos.com", "xnxx.com", "twitch.tv", "dailymotion.com",
        "soundcloud.com", "spankbang.com", "redtube.com", "youporn.com", "tube8.com",
        "pinterest.com", "pin.it"
    ]
    is_media_domain = any(domain in parsed.netloc.lower() for domain in media_domains)
    
    # Use yt-dlp only for media domains and streaming content
    use_ytdlp = is_media_domain or is_streaming

    if use_ytdlp:
        await status_msg.edit_text("🔍 *Extracting media info... / دریافت اطلاعات*", parse_mode="Markdown")
        loop = asyncio.get_running_loop()
        
        try:
            def extract_metadata():
                ydl_opts = {
                    'quiet': True,
                    'no_warnings': True,
                    'socket_timeout': 15,
                    'extractor_args': get_youtube_extractor_args(),
                    'js_runtimes': {'node': {}},
                    **get_ydl_cookie_opts(url),
                    **get_site_specific_opts(url),
                }
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        return ydl.extract_info(url, download=False)
                except Exception as e:
                    if "cookiefile" in ydl_opts:
                        logger.warning("Metadata extraction failed with cookies, retrying without...")
                        del ydl_opts["cookiefile"]
                        try:
                            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                                return ydl.extract_info(url, download=False)
                        except Exception as e_inner:
                            e = e_inner
                    if "impersonate" in ydl_opts:
                        logger.warning("Metadata extraction failed with impersonate, retrying without...")
                        del ydl_opts["impersonate"]
                        try:
                            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                                return ydl.extract_info(url, download=False)
                        except Exception as e_inner:
                            e = e_inner
                    raise e
            
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
                    f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                    f"┃  📦  *PLAYLIST FOUND*     ┃\n"
                    f"┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                    f"📚 `{info.get('title', 'Album')}`\n"
                    f"🔢 `{len(entries)}` videos\n\n"
                    f"👇 _Choose download strategy:_",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown"
                )
                return

            # Handle Single Video / Audio
            duration = info.get('duration', 0)
            duration_str = f"{format_seconds(duration)}" if duration else "unknown"
            
            keyboard = [
                [
                    InlineKeyboardButton("🎥 Video (ویدیو)", callback_data=f"dmethod:{url_id}:video"),
                    InlineKeyboardButton("⚙️ Resolution (کیفیت)", callback_data=f"resopts:{url_id}")
                ],
                [
                    InlineKeyboardButton("🎵 MP3 Audio (صدا)", callback_data=f"dmethod:{url_id}:audio"),
                    InlineKeyboardButton("✂️ Trim Range (برش)", callback_data=f"dl:{url_id}:trim"),
                ],
                [InlineKeyboardButton("❌ Cancel (لغو)", callback_data=f"dl:{url_id}:cancel")]
            ]
            
            # Add GIF option directly if video <= 15s
            if duration and duration <= 15:
                keyboard[1].append(InlineKeyboardButton("🖼 Convert to GIF", callback_data=f"dmethod:{url_id}:gif"))
            
            await status_msg.delete()
            await message.reply_text(
                f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                f"┃  ✨  *MEDIA INFO*         ┃\n"
                f"┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                f"📝 `{info.get('title', 'Social Media File')}`\n"
                f"⏱ Duration: `{duration_str}`\n\n"
                f"👇 _Choose action:_",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Metadata extraction failed: {e}")
            err_str = str(e)

            # Detect YouTube/PornHub bot detection blocking
            if (isinstance(e, AssertionError) or "assertion" in err_str.lower()) and "cookie" in err_str.lower():
                await status_msg.edit_text(
                    "⚠️ *Cookie Format Error / خطای فرمت کوکی*\n\n"
                    "🍪 The uploaded `cookies.txt` contains formatting errors (invalid Netscape format) which causes `yt-dlp` to crash.\n\n"
                    "Please re-export your cookies using a browser extension (like *Get cookies.txt LOCALLY*) in Netscape format and re-send it.\n\n"
                    "🔄 *Attempting queue download anyway...*",
                    parse_mode="Markdown"
                )
                await add_to_queue(url, message, "video", custom_name, user_id=user_id)
            elif any(x in err_str.lower() for x in ["confirm you", "not a bot", "format is not available", "cookies", "forbidden", "403", "429"]):
                await status_msg.edit_text(
                    "⚠️ *YouTube Bot Detection Active / بلاک توسط یوتیوب*\n\n"
                    "🍪 YouTube has blocked this request. To bypass this, please upload a `cookies.txt` file "
                    "or run the /cookies command for step-by-step instructions.\n\n"
                    "🔄 *Attempting queue download anyway...*",
                    parse_mode="Markdown"
                )
                await add_to_queue(url, message, "video", custom_name, user_id=user_id)
            elif is_media_domain or is_streaming:
                if is_impersonate_site(url):
                    # Real fix: updated yt-dlp + --impersonate chrome handles this
                    # The 403 was caused by stale extractor, not IP blocking
                    await status_msg.edit_text(
                        f"⚠️ *Could not fetch video metadata.*\n`{err_str[:150]}`\n\n"
                        "🔄 *Retrying with browser impersonation (updated yt-dlp)...*",
                        parse_mode="Markdown"
                    )
                    # Queue direct download — get_site_specific_opts adds impersonate=chrome
                    await add_to_queue(url, message, "video", custom_name, user_id=user_id)
                else:
                    # Not an impersonate site — standard yt-dlp retry
                    await status_msg.edit_text(
                        f"⚠️ *Could not fetch video metadata.*\n`{err_str[:180]}`\n\n"
                        "🔄 Attempting direct yt-dlp download...",
                        parse_mode="Markdown"
                    )
                    await add_to_queue(url, message, "video", custom_name, user_id=user_id)
            else:
                await status_msg.edit_text(
                    "⚠️ Could not auto-detect format. Attempting direct download...",
                    parse_mode="Markdown"
                )
                await add_to_queue(url, message, None, custom_name, user_id=user_id)
    else:
        # Direct link download
        await status_msg.delete()
        url_id = uuid.uuid4().hex[:8]
        URL_CACHE[url_id] = {
            'url': url,
            'title': custom_name or "Direct File",
            'thumbnail': None,
            'duration': 0,
            'start_time': None,
            'end_time': None
        }
        
        keyboard = [
            [
                InlineKeyboardButton("📥 Download to Telegram (تلگرام)", callback_data=f"dl:{url_id}:file"),
                InlineKeyboardButton("🌐 Upload to Web (مستقیم)", callback_data=f"dl:{url_id}:web")
            ],
            [InlineKeyboardButton("❌ Cancel (لغو)", callback_data=f"dl:{url_id}:cancel")]
        ]
        
        await message.reply_text(
            f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
            f"┃  📥  *DIRECT LINK*        ┃\n"
            f"┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
            f"🔗 `{url}`\n\n"
            f"👇 _Choose action:_",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

async def handle_subtitle_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message = update.message
    state_info = USER_STATES.get(user_id)
    if not state_info or state_info.get('state') != 'AWAITING_SUBTITLE':
        return

    file_uuid = state_info['video_file_uuid']
    cached = URL_CACHE.get(file_uuid)
    if not cached:
        await message.reply_text("❌ Video conversion session expired.")
        USER_STATES.pop(user_id, None)
        return

    # Reset state
    USER_STATES.pop(user_id, None)

    # Download subtitle file
    srt_file_id = message.document.file_id
    srt_filename = message.document.file_name or "sub.srt"

    # Enqueue the subtitle hardcoding task
    await add_subtitle_conversion_to_queue(cached['file_id'], cached['filename'], srt_file_id, srt_filename, message, user_id)

async def add_subtitle_conversion_to_queue(video_file_id, video_filename, srt_file_id, srt_filename, message_to_reply, user_id):
    chat_id = message_to_reply.chat.id
    status_msg = await message_to_reply.reply_text("⏳ *Calculating position in queue...*", parse_mode="Markdown")
    
    async def subtitle_task():
        try:
            await status_msg.edit_text("🚀 *Burning subtitles started...*", parse_mode="Markdown")
            with tempfile.TemporaryDirectory() as temp_dir:
                bot = status_msg.get_bot()
                
                # Download video
                await status_msg.edit_text("📥 *Downloading video from Telegram...*", parse_mode="Markdown")
                video_file = await bot.get_file(video_file_id)
                video_path = os.path.join(temp_dir, "input.mp4")
                await video_file.download_to_drive(video_path)
                
                # Download subtitle
                await status_msg.edit_text("📥 *Downloading subtitle file...*", parse_mode="Markdown")
                srt_file = await bot.get_file(srt_file_id)
                srt_path = os.path.join(temp_dir, "sub.srt")
                await srt_file.download_to_drive(srt_path)
                
                # Burn subtitles using FFmpeg
                await status_msg.edit_text("🎬 *Hardcoding subtitles (encoding)... / در حال چسباندن زیرنویس*", parse_mode="Markdown")
                output_path = os.path.join(temp_dir, "subbed_video.mp4")
                
                cmd = [
                    FFMPEG_EXE, "-y", "-i", "input.mp4",
                    "-vf", "subtitles=sub.srt",
                    "-c:v", "libx264", "-crf", "28", "-preset", "fast",
                    "-c:a", "copy",
                    "subbed_video.mp4"
                ]
                
                loop = asyncio.get_running_loop()
                def run_ffmpeg():
                    subprocess.run(cmd, cwd=temp_dir, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    return output_path
                
                await loop.run_in_executor(None, run_ffmpeg)
                
                if os.path.exists(output_path):
                    await status_msg.edit_text("📤 *Uploading video with subtitles...*", parse_mode="Markdown")
                    file_size = os.path.getsize(output_path)
                    db.log_download(user_id, "video_subbed", file_size)
                    
                    reply_id = message_to_reply.message_id
                    with open(output_path, "rb") as f:
                        await bot.send_video(
                            chat_id=chat_id,
                            video=f,
                            reply_to_message_id=reply_id
                        )
                    await status_msg.delete()
                else:
                    await status_msg.edit_text("❌ *Subtitling failed.* Could not build output file.")
        except Exception as e:
            logger.error(f"Subtitle burn failed: {e}", exc_info=True)
            await status_msg.edit_text(f"❌ *Subtitle burn failed:* `{str(e)[:150]}`", parse_mode="Markdown")
            
    await download_queue.put(subtitle_task)

# =====================================================================
# File Receiver & Processing Handler (File Converter Entrypoint)
# =====================================================================
async def handle_incoming_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches document, photo, video, or audio attachments and offers conversion choices."""
    message = update.message
    if not message:
        return

    user_id = update.effective_user.id

    # Check if user is actively building a PDF album
    if user_id in USER_STATES and USER_STATES[user_id].get('state') == 'AWAITING_PDF_ALBUM_IMAGES':
        img_id = None
        if message.photo:
            img_id = message.photo[-1].file_id
        elif message.document:
            _, ext = os.path.splitext((message.document.file_name or "").lower())
            if ext in [".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"]:
                img_id = message.document.file_id
        
        if img_id:
            USER_STATES[user_id]['pdf_album'].append(img_id)
            album_size = len(USER_STATES[user_id]['pdf_album'])
            
            keyboard = [
                [InlineKeyboardButton("📄 Compile PDF Album Now", callback_data="pdfalbum_build_active")],
                [InlineKeyboardButton("❌ Cancel / Exit Mode", callback_data="pdfalbum_cancel")]
            ]
            await message.reply_text(
                f"📸 *Image added to your album!* (Total: `{album_size}`)\n\n"
                f"Send more images, or click below to build the PDF album now.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return

    # Identify file details
    file_id = None
    filename = None
    file_type = None

    if message.document:
        file_id = message.document.file_id
        filename = message.document.file_name or f"document_{message.document.file_id[:8]}"
        file_type = "document"
        
        # Check if this is a subtitle file and user is awaiting one
        if filename.lower().endswith(".srt") and user_id in USER_STATES and USER_STATES[user_id].get('state') == 'AWAITING_SUBTITLE':
            await handle_subtitle_upload(update, context)
            return

        # Admin: save cookies.txt or cookiesph.txt for bot detection bypass
        if filename.lower() in ("cookies.txt", "cookiesph.txt"):
            is_admin = False
            if ADMIN_USER_ID and ADMIN_USER_ID.strip() and "YOUR_ADMIN_USER_ID" not in ADMIN_USER_ID:
                if str(user_id) == str(ADMIN_USER_ID):
                    is_admin = True
            else:
                is_admin = True  # Allow if no admin configured yet

            if not is_admin:
                await message.reply_text(
                    f"❌ Only the admin can upload cookies.\n"
                    f"Your Telegram User ID is: `{user_id}`\n"
                    f"Set `ADMIN_USER_ID={user_id}` in your `.env` file.",
                    parse_mode="Markdown"
                )
                return

            target_file = COOKIES_FILE if filename.lower() == "cookies.txt" else COOKIES_PH_FILE
            status = await message.reply_text(f"🍪 *Saving {filename}...*", parse_mode="Markdown")
            tg_file = await context.bot.get_file(file_id)
            await tg_file.download_to_drive(target_file)
            file_sz = os.path.getsize(target_file)
            await status.edit_text(
                f"✅ *{filename} saved!*\n`{file_sz} bytes`\n\n"
                f"🔄 Bot will now use your cookies to bypass bot detection on the respective platform.",
                parse_mode="Markdown"
            )
            return
    elif message.photo:
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
    elif message.video:
        file_id = message.video.file_id
        filename = message.video.file_name or f"video_{message.video.file_id[:8]}.mp4"
        file_type = "video"

    if not file_id:
        return

    # Check if AI mode is active
    engine = USER_STATES.get(user_id, {}).get("ai_engine")
    if engine:
        status_msg = await message.reply_text(f"🤖 *Processing file ({engine})... / در حال پردازش فایل*", parse_mode="Markdown")
        try:
            tg_file = await context.bot.get_file(file_id)
            import tempfile, base64
            with tempfile.TemporaryDirectory() as temp_dir:
                filepath = os.path.join(temp_dir, filename)
                await tg_file.download_to_drive(filepath)
                with open(filepath, "rb") as f:
                    file_bytes = f.read()
                base64_data = base64.b64encode(file_bytes).decode("utf-8")
                
            # Mime type detection
            mime_type = None
            if message.photo:
                mime_type = "image/jpeg"
            elif message.document:
                mime_type = message.document.mime_type
            elif message.audio:
                mime_type = message.audio.mime_type
            elif message.voice:
                mime_type = message.voice.mime_type
            elif message.video:
                mime_type = message.video.mime_type
            
            if not mime_type:
                import mimetypes
                mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                
            file_data = {
                "mime_type": mime_type,
                "data": base64_data
            }
            
            caption = message.caption or "توضیح دهید"
            
            if engine == "gemini":
                response = await query_gemini(caption, file_data)
            elif engine == "aerolink":
                response = await query_aerolink(caption, file_data)
            elif engine == "nvidia":
                response = await query_nvidia(caption, file_data)
            else:
                response = await query_gemini(caption, file_data)
                
            # Format markdown
            formatted_text = response
            formatted_text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', formatted_text)
            formatted_text = re.sub(r'^###\s+(.+)$', r'*\1*', formatted_text, flags=re.MULTILINE)
            formatted_text = re.sub(r'^##\s+(.+)$', r'*\1*', formatted_text, flags=re.MULTILINE)
            formatted_text = re.sub(r'^#\s+(.+)$', r'*\1*', formatted_text, flags=re.MULTILINE)
            
            engine_names = {"gemini": "Gemini", "aerolink": "Aerolink AI"}
            engine_name = engine_names.get(engine, "AI")
            final_msg = f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n┃  ✨  *{engine_name}*          ┃\n┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n{formatted_text}"
            
            try:
                await status_msg.edit_text(final_msg, parse_mode="Markdown")
            except Exception:
                plain_msg = f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n┃  ✨  {engine_name}          ┃\n┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n{response}"
                await status_msg.edit_text(plain_msg)
            return
        except Exception as ex:
            logger.error(f"AI file processing failed: {ex}")
            await status_msg.edit_text(f"❌ *AI processing error:* `{str(ex)[:150]}`", parse_mode="Markdown")
            return

    # Classify file type based on extension
    _, ext = os.path.splitext(filename.lower())
    image_exts = [".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"]
    audio_exts = [".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".wma"]
    video_exts = [".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm"]

    if ext in image_exts:
        file_type = "image"
    elif ext in audio_exts:
        file_type = "audio"
    elif ext in video_exts:
        file_type = "video"
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
            [
                InlineKeyboardButton("📁 Add to PDF Album", callback_data=f"conv:{file_uuid}:pdfalbum")
            ]
        ]
    elif file_type == "audio":
        keyboard = [
            [
                InlineKeyboardButton("🎵 To MP3", callback_data=f"conv:{file_uuid}:mp3"),
                InlineKeyboardButton("🎵 To WAV", callback_data=f"conv:{file_uuid}:wav"),
            ],
            [
                InlineKeyboardButton("🗣 To OGG", callback_data=f"conv:{file_uuid}:ogg"),
                InlineKeyboardButton("🏷 Edit Tags", callback_data=f"conv:{file_uuid}:tags")
            ],
            [
                InlineKeyboardButton("🎭 Voice Effects", callback_data=f"conv:{file_uuid}:vfx")
            ]
        ]
    elif file_type == "video":
        keyboard = [
            [
                InlineKeyboardButton("📉 Compress (فشرده‌سازی)", callback_data=f"conv:{file_uuid}:compress"),
                InlineKeyboardButton("🎵 Extract MP3 (استخراج صدا)", callback_data=f"conv:{file_uuid}:mp3")
            ],
            [
                InlineKeyboardButton("💬 Add Subtitle (زیرنویس)", callback_data=f"conv:{file_uuid}:sub"),
                InlineKeyboardButton("🖼 Convert to GIF", callback_data=f"conv:{file_uuid}:gif")
            ]
        ]
    elif file_type == "pdf":
        keyboard = [
            [
                InlineKeyboardButton("📝 PDF to Text", callback_data=f"conv:{file_uuid}:txt"),
                InlineKeyboardButton("🔒 Protect PDF", callback_data=f"conv:{file_uuid}:pdfprotect")
            ],
            [
                InlineKeyboardButton("🔓 Unlock PDF", callback_data=f"conv:{file_uuid}:pdfunlock")
            ]
        ]
    elif file_type == "docx":
        keyboard = [
            [InlineKeyboardButton("📄 Convert to PDF (Word -> PDF)", callback_data=f"conv:{file_uuid}:docx2pdf")]
        ]

    if not keyboard:
        await message.reply_text(
            f"📥 *File Received:* `{filename}`\n\n❌ Format conversions are not supported for this file type.",
            parse_mode="Markdown",
        )
        return

    keyboard.append([InlineKeyboardButton("❌ Cancel (لغو)", callback_data=f"conv:{file_uuid}:cancel")])

    await message.reply_text(
        f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
        f"┃  🔄  *FILE CONVERTER*     ┃\n"
        f"┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
        f"📁 `{filename}`\n"
        f"📦 Type: `{file_type.upper()}`\n\n"
        f"👇 _Select action:_",
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

    # Handle AI Engine Selection
    if action == "ai_engine":
        engine = data[1]
        if user_id not in USER_STATES:
            USER_STATES[user_id] = {}
        USER_STATES[user_id]['ai_engine'] = engine
        
        engine_names = {
            "gemini": "✨ Google Gemini",
            "aerolink": "🚀 Aerolink AI",
            "nvidia": "🟢 Nvidia GLM 5.2"
        }
        engine_name = engine_names.get(engine, engine)
        
        keyboard = [[InlineKeyboardButton("🛑 Stop AI", callback_data="ai_stop")]]
        await query.message.edit_text(
            f"✅ AI Engine set to: *{engine_name}*\n\n"
            f"Now send me any text message to chat! / برای شروع مکالمه یک متن ارسال کنید:\n\n"
            f"🛑 *برای غیرفعال کردن:* دکمه زیر را بزنید یا دوباره روی AI Chat کلیک کنید",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    if action == "ai_stop":
        if user_id in USER_STATES:
            USER_STATES[user_id].pop('ai_engine', None)
        keyboard = [
            [
                InlineKeyboardButton("✨ Gemini", callback_data="ai_engine:gemini"),
                InlineKeyboardButton("🚀 Aerolink AI", callback_data="ai_engine:aerolink")
            ],
            [
                InlineKeyboardButton("🟢 Nvidia GLM 5.2", callback_data="ai_engine:nvidia")
            ]
        ]
        await query.message.edit_text(
            "🛑 *AI Chat stopped / هوش مصنوعی غیرفعال شد*\n\n"
            "Pick a new engine or use the bot normally.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    if action == "ainoop":
        await query.answer("AI is already active!", show_alert=False)
        return

    if action == "tp_upload":
        unique_id = data[1]
        cached_info = UPLOAD_CACHE.get(unique_id)
        if not cached_info:
            await query.answer("❌ این فایل منقضی شده است (مهلت ۱۰ دقیقه به پایان رسیده).", show_alert=True)
            return

        filepath = cached_info['filepath']
        filename = cached_info['filename']

        if not os.path.exists(filepath):
            await query.answer("❌ فایل روی سرور یافت نشد.", show_alert=True)
            return

        await query.answer("در حال آپلود روی سایت...")
        status_msg = await query.message.reply_text("☁️ *در حال آپلود فایل روی پلتفرم TechPulse... / Uploading...*", parse_mode="Markdown")

        success, detail = await upload_to_techpulse(filepath, custom_filename=filename)
        if success:
            await status_msg.edit_text(f"✅ فایل `{filename}` با موفقیت روی سایت آپلود شد!")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
        else:
            await status_msg.edit_text(f"❌ آپلود فایل شکست خورد: `{detail}`")
        return

    # Handle GitHub & GitLab Interactive Explorer Navigation
    if action == "gitnav":
        url_id = data[1]
        sub_action = data[2]
        
        cached = URL_CACHE.get(url_id)
        if not cached or not cached.get('is_git'):
            await query.answer("❌ Session expired. Please send link again.", show_alert=True)
            return
            
        git_info = cached['git_info']
        platform = git_info['platform']
        owner = git_info['owner']
        repo = git_info['repo']
        branch = cached.get('branch')
        path = cached.get('path', "")
        page = cached.get('page', 0)
        
        gh_token, gl_token = db.get_tokens(user_id)
        token = gh_token if platform == "github" else gl_token
        
        if sub_action == "cancel":
            await query.delete_message()
            URL_CACHE.pop(url_id, None)
            return
            
        elif sub_action == "go":
            idx = int(data[3])
            items = cached.get('items', [])
            if idx >= len(items):
                await query.answer("❌ Item not found.")
                return
            item = items[idx]
            if item['type'] == 'dir':
                cached['path'] = item['path']
                cached['page'] = 0
                await query.answer(f"Navigating to {item['name']}...")
                await render_git_explorer(query.message, url_id, user_id, edit=True)
                return
            else:
                sub_action = "dlfile"
            
        elif sub_action == "up":
            if path:
                parts = path.strip("/").split("/")
                if len(parts) > 1:
                    cached['path'] = "/".join(parts[:-1])
                else:
                    cached['path'] = ""
                cached['page'] = 0
                await query.answer("Going up...")
                await render_git_explorer(query.message, url_id, user_id, edit=True)
            return
            
        elif sub_action == "page":
            direction = data[3]
            if direction == "prev" and page > 0:
                cached['page'] = page - 1
            elif direction == "next":
                cached['page'] = page + 1
            await render_git_explorer(query.message, url_id, user_id, edit=True)
            return
            
        elif sub_action == "branches":
            await query.answer("Fetching branches...")
            branches = await fetch_git_branches(platform, owner, repo, token)
            if not branches:
                await query.answer("❌ Failed to fetch branches.", show_alert=True)
                return
            cached['branches'] = branches
            
            keyboard = []
            for b_idx, b_name in enumerate(branches):
                keyboard.append([InlineKeyboardButton(f"🌿 {b_name}", callback_data=f"gitnav:{url_id}:setbranch:{b_idx}")])
            keyboard.append([InlineKeyboardButton("⬅️ Back to Explorer", callback_data=f"gitnav:{url_id}:back")])
            
            await query.edit_message_text(
                f"🌿 *BRANCHES / شاخه‌های مخزن* `{repo}`\nSelect a branch to explore:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return
            
        elif sub_action == "setbranch":
            b_idx = int(data[3])
            branches = cached.get('branches', [])
            if b_idx >= len(branches):
                await query.answer("❌ Branch not found.")
                return
            cached['branch'] = branches[b_idx]
            cached['page'] = 0
            cached['path'] = ""
            await query.answer(f"Switched branch to {branches[b_idx]}!")
            await render_git_explorer(query.message, url_id, user_id, edit=True)
            return
            
        elif sub_action == "back":
            await render_git_explorer(query.message, url_id, user_id, edit=True)
            return
            
        elif sub_action == "releases":
            # Delegate to existing releases callback
            query.data = f"gitdl:{url_id}:releases"
            data = query.data.split(":")
            action = "gitdl"
            choice = "releases"
            # Fall through to gitdl code
            
        elif sub_action == "dlzip":
            async def git_zip_download_task():
                status_msg = await query.message.reply_text("🚀 *Git download task started...*")
                try:
                    with tempfile.TemporaryDirectory() as temp_dir:
                        bot_obj = query.message.get_bot()
                        chat_id = query.message.chat_id
                        
                        if not path:
                            await status_msg.edit_text("📥 *Downloading Repository ZIP...*")
                            zip_file = os.path.join(temp_dir, f"{repo}.zip")
                            
                            if platform == "github":
                                dl_url = f"https://api.github.com/repos/{owner}/{repo}/zipball/{branch}"
                                headers = {"Authorization": f"token {token}"} if token else {}
                            else:
                                import urllib.parse
                                project_id = urllib.parse.quote_plus(f"{owner}/{repo}")
                                dl_url = f"https://gitlab.com/api/v4/projects/{project_id}/repository/archive.zip"
                                if branch:
                                    dl_url += f"?sha={branch}"
                                headers = {"PRIVATE-TOKEN": token} if token else {}
                                
                            proxy_url = get_proxy_url()
                            async with aiohttp.ClientSession(headers=headers) as session:
                                async with session.get(dl_url, allow_redirects=True, proxy=proxy_url) as resp:
                                    if resp.status != 200:
                                        await status_msg.edit_text(f"❌ Failed to download zip: HTTP {resp.status}")
                                        return
                                    with open(zip_file, "wb") as f:
                                        f.write(await resp.read())
                                        
                            await status_msg.edit_text("📤 *Uploading Repository ZIP to Telegram...*")
                            file_size = os.path.getsize(zip_file)
                            db.log_download(user_id, "git_repo", file_size)
                            
                            with open(zip_file, "rb") as f:
                                await bot_obj.send_document(chat_id=chat_id, document=f, filename=f"{repo}.zip")
                            await status_msg.delete()
                            
                            await register_file_for_upload(bot_obj, chat_id, zip_file, f"{repo}.zip")
                        else:
                            folder_name = os.path.basename(path)
                            await status_msg.edit_text(f"📥 *Downloading Folder contents:* `{folder_name}`...")
                            local_folder_dir = os.path.join(temp_dir, folder_name)
                            os.makedirs(local_folder_dir, exist_ok=True)
                            
                            async with aiohttp.ClientSession() as session:
                                if platform == "github":
                                    await download_github_folder_recursive(
                                        session, owner, repo, path, branch, local_folder_dir, token
                                    )
                                else:
                                    await download_gitlab_folder_recursive(
                                        session, owner, repo, path, branch, local_folder_dir, token
                                    )
                                    
                            import shutil

                            zip_output_path = os.path.join(temp_dir, folder_name)
                            await status_msg.edit_text("📦 *Compressing folder...*")
                            shutil.make_archive(zip_output_path, 'zip', local_folder_dir)
                            final_zip = zip_output_path + ".zip"
                            
                            await status_msg.edit_text("📤 *Uploading Folder ZIP to Telegram...*")
                            file_size = os.path.getsize(final_zip)
                            db.log_download(user_id, "git_folder", file_size)
                            
                            with open(final_zip, "rb") as f:
                                await bot_obj.send_document(chat_id=chat_id, document=f, filename=f"{folder_name}.zip")
                            await status_msg.delete()
                            
                            await register_file_for_upload(bot_obj, chat_id, final_zip, f"{folder_name}.zip")
                except Exception as e:
                    logger.error(f"Git ZIP download failed: {e}")
                    await query.message.reply_text(f"❌ *Git download failed:* `{str(e)[:150]}`")
            await download_queue.put(git_zip_download_task)
            await query.answer("Enqueued ZIP download!")
            return
            
        elif sub_action == "dlfile":
            idx = int(data[3])
            items = cached.get('items', [])
            if idx >= len(items):
                await query.answer("❌ File not found.")
                return
            item = items[idx]
            
            async def git_file_download_task():
                status_msg = await query.message.reply_text(f"📥 *Downloading File:* `{item['name']}`...", parse_mode="Markdown")
                try:
                    with tempfile.TemporaryDirectory() as temp_dir:
                        dest_file = os.path.join(temp_dir, item['name'])
                        proxy_url = get_proxy_url()
                        
                        if platform == "github":
                            dl_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{item['path']}"
                            headers = {"Accept": "application/vnd.github.v3.raw"}
                            if token:
                                headers["Authorization"] = f"token {token}"
                            params = {"ref": branch} if branch else {}
                        else:
                            import urllib.parse
                            project_id = urllib.parse.quote_plus(f"{owner}/{repo}")
                            enc_path = urllib.parse.quote_plus(item['path'])
                            dl_url = f"https://gitlab.com/api/v4/projects/{project_id}/repository/files/{enc_path}/raw"
                            headers = {"PRIVATE-TOKEN": token} if token else {}
                            params = {"ref": branch} if branch else {}
                            
                        async with aiohttp.ClientSession(headers=headers) as session:
                            async with session.get(dl_url, params=params, proxy=proxy_url) as resp:
                                if resp.status != 200:
                                    await status_msg.edit_text(f"❌ Failed to download file: HTTP {resp.status}")
                                    return
                                with open(dest_file, "wb") as f:
                                    f.write(await resp.read())
                                    
                        await status_msg.edit_text("📤 *Uploading File to Telegram...*")
                        file_size = os.path.getsize(dest_file)
                        db.log_download(user_id, "git_file", file_size)
                        
                        with open(dest_file, "rb") as f:
                            await query.message.get_bot().send_document(
                                chat_id=query.message.chat_id,
                                document=f,
                                filename=item['name']
                            )
                        await status_msg.delete()
                        
                        await register_file_for_upload(query.message.get_bot(), query.message.chat_id, dest_file, item['name'])
                except Exception as e:
                    logger.error(f"Git file download failed: {e}")
                    await query.message.reply_text(f"❌ *File download failed:* `{str(e)[:150]}`")
                    
            await download_queue.put(git_file_download_task)
            await query.answer("Enqueued file download!")
            return

    # Handle GitHub & GitLab Downloader Actions
    if action == "gitdl":
        url_id = data[1]
        choice = data[2]
        
        if choice == "cancel":
            await query.delete_message()
            URL_CACHE.pop(url_id, None)
            return
            
        cached = URL_CACHE.get(url_id)
        if not cached or not cached.get('is_git'):
            await query.edit_message_text("❌ Session expired. Please send link again.")
            return
            
        git_info = cached['git_info']
        platform = git_info['platform']
        owner = git_info['owner']
        repo = git_info['repo']
        branch = git_info['branch']
        path = git_info['path']
        
        gh_token, gl_token = db.get_tokens(user_id)
        token = gh_token if platform == "github" else gl_token
        
        if choice == "releases":
            await query.edit_message_text("🔍 *Fetching releases... / دریافت ریلیزها*", parse_mode="Markdown")
            
            async def release_task():
                try:
                    headers = {"Accept": "application/vnd.github.v3+json"}
                    if platform == "github":
                        if token:
                            headers["Authorization"] = f"token {token}"
                        url = f"https://api.github.com/repos/{owner}/{repo}/releases"
                    else:
                        if token:
                            headers["PRIVATE-TOKEN"] = token
                        import urllib.parse
                        project_id = urllib.parse.quote_plus(f"{owner}/{repo}")
                        url = f"https://gitlab.com/api/v4/projects/{project_id}/releases"
                        
                    proxy_url = get_proxy_url()
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, headers=headers, proxy=proxy_url) as resp:
                            if resp.status != 200:
                                await query.message.reply_text(f"❌ Failed to fetch releases: HTTP {resp.status}")
                                return
                            releases_data = await resp.json()
                            
                    if not releases_data:
                        await query.message.reply_text("❌ No releases found for this repository.")
                        return
                        
                    text = f"🏷 *RELEASES / ریلیزهای مخزن* `{repo}`\n{DIVIDER}\n"
                    keyboard = []
                    
                    if platform == "github":
                        latest = releases_data[0]
                        tag_name = latest.get("tag_name", "Latest")
                        text += f"Latest Release: *{tag_name}*\n\n"
                        
                        assets = latest.get("assets", [])
                        for asset in assets:
                            name = asset.get("name")
                            dl_url = asset.get("browser_download_url")
                            text += f" ├─ `{name}`\n"
                            
                            asset_url_id = uuid.uuid4().hex[:8]
                            URL_CACHE[asset_url_id] = {
                                'url': dl_url,
                                'title': name,
                            }
                            keyboard.append([InlineKeyboardButton(f"📥 {name[:30]}", callback_data=f"git_asset_dl:{asset_url_id}")])
                    else:
                        latest = releases_data[0]
                        tag_name = latest.get("tag_name", "Latest")
                        text += f"Latest Release: *{tag_name}*\n\n"
                        
                        links = latest.get("assets", {}).get("links", [])
                        for link in links:
                            name = link.get("name")
                            dl_url = link.get("url")
                            text += f" ├─ `{name}`\n"
                            
                            asset_url_id = uuid.uuid4().hex[:8]
                            URL_CACHE[asset_url_id] = {
                                'url': dl_url,
                                'title': name,
                            }
                            keyboard.append([InlineKeyboardButton(f"📥 {name[:30]}", callback_data=f"git_asset_dl:{asset_url_id}")])
                            
                    keyboard.append([InlineKeyboardButton("❌ Close", callback_data="gitdl_cancel")])
                    
                    await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"Failed to fetch releases: {e}")
                    await query.message.reply_text(f"❌ *Failed to list releases:* `{str(e)[:150]}`")
                    
            await download_queue.put(release_task)
            return
            
        await query.edit_message_text("⏳ *Processing Git download task... / در حال پردازش*", parse_mode="Markdown")
        
        async def git_dl_task():
            try:
                status_msg = await query.message.reply_text("🚀 *Git download task started...*")
                with tempfile.TemporaryDirectory() as temp_dir:
                    bot = query.message.get_bot()
                    chat_id = query.message.chat_id
                    
                    if choice == "repo":
                        await status_msg.edit_text("📥 *Downloading Repository ZIP...*")
                        zip_file = os.path.join(temp_dir, f"{repo}.zip")
                        
                        if platform == "github":
                            ref_branch = branch
                            if not ref_branch:
                                headers = {"Accept": "application/vnd.github.v3+json"}
                                if token:
                                    headers["Authorization"] = f"token {token}"
                                async with aiohttp.ClientSession() as session:
                                    async with session.get(f"https://api.github.com/repos/{owner}/{repo}", headers=headers) as r:
                                        if r.status == 200:
                                            repo_info = await r.json()
                                            ref_branch = repo_info.get("default_branch", "main")
                                        else:
                                            ref_branch = "main"
                            
                            dl_url = f"https://api.github.com/repos/{owner}/{repo}/zipball/{ref_branch}"
                            headers = {"Authorization": f"token {token}"} if token else {}
                        else:
                            import urllib.parse
                            project_id = urllib.parse.quote_plus(f"{owner}/{repo}")
                            dl_url = f"https://gitlab.com/api/v4/projects/{project_id}/repository/archive.zip"
                            if branch:
                                dl_url += f"?sha={branch}"
                            headers = {"PRIVATE-TOKEN": token} if token else {}
                            
                        proxy_url = get_proxy_url()
                        async with aiohttp.ClientSession(headers=headers) as session:
                            async with session.get(dl_url, allow_redirects=True, proxy=proxy_url) as resp:
                                if resp.status != 200:
                                    await status_msg.edit_text(f"❌ Failed to download zip: HTTP {resp.status}")
                                    return
                                with open(zip_file, "wb") as f:
                                    f.write(await resp.read())
                                    
                        await status_msg.edit_text("📤 *Uploading Repository ZIP...*")
                        file_size = os.path.getsize(zip_file)
                        db.log_download(user_id, "git_repo", file_size)
                        
                        with open(zip_file, "rb") as f:
                            await bot.send_document(chat_id=chat_id, document=f, filename=f"{repo}.zip")
                        await status_msg.delete()
                        
                    elif choice == "file":
                        filename_only = os.path.basename(path)
                        await status_msg.edit_text(f"📥 *Downloading File:* `{filename_only}`...")
                        dest_file = os.path.join(temp_dir, filename_only)
                        
                        if platform == "github":
                            ref_branch = branch or "main"
                            dl_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
                            headers = {"Accept": "application/vnd.github.v3.raw"}
                            if token:
                                headers["Authorization"] = f"token {token}"
                            params = {"ref": ref_branch}
                        else:
                            import urllib.parse
                            project_id = urllib.parse.quote_plus(f"{owner}/{repo}")
                            enc_path = urllib.parse.quote_plus(path)
                            dl_url = f"https://gitlab.com/api/v4/projects/{project_id}/repository/files/{enc_path}/raw"
                            headers = {"PRIVATE-TOKEN": token} if token else {}
                            params = {"ref": branch} if branch else {}
                            
                        proxy_url = get_proxy_url()
                        async with aiohttp.ClientSession(headers=headers) as session:
                            async with session.get(dl_url, params=params, proxy=proxy_url) as resp:
                                if resp.status != 200:
                                    await status_msg.edit_text(f"❌ Failed to download file: HTTP {resp.status}")
                                    return
                                with open(dest_file, "wb") as f:
                                    f.write(await resp.read())
                                    
                        await status_msg.edit_text("📤 *Uploading File...*")
                        file_size = os.path.getsize(dest_file)
                        db.log_download(user_id, "git_file", file_size)
                        
                        with open(dest_file, "rb") as f:
                            await bot.send_document(chat_id=chat_id, document=f, filename=filename_only)
                        await status_msg.delete()
                        
                    elif choice == "folder":
                        folder_name = os.path.basename(path)
                        await status_msg.edit_text(f"📥 *Downloading Folder contents:* `{folder_name}`...")
                        local_folder_dir = os.path.join(temp_dir, folder_name)
                        os.makedirs(local_folder_dir, exist_ok=True)
                        
                        async with aiohttp.ClientSession() as session:
                            if platform == "github":
                                await download_github_folder_recursive(
                                    session, owner, repo, path, branch, local_folder_dir, token
                                )
                            else:
                                await download_gitlab_folder_recursive(
                                    session, owner, repo, path, branch, local_folder_dir, token
                                )
                                
                        import shutil

                        zip_output_path = os.path.join(temp_dir, folder_name)
                        await status_msg.edit_text("📦 *Compressing folder...*")
                        shutil.make_archive(zip_output_path, 'zip', local_folder_dir)
                        final_zip = zip_output_path + ".zip"
                        
                        await status_msg.edit_text("📤 *Uploading Folder ZIP...*")
                        file_size = os.path.getsize(final_zip)
                        db.log_download(user_id, "git_folder", file_size)
                        
                        with open(final_zip, "rb") as f:
                            await bot.send_document(chat_id=chat_id, document=f, filename=f"{folder_name}.zip")
                        await status_msg.delete()
                        
            except Exception as e:
                logger.error(f"Git download failed: {e}")
                await query.message.reply_text(f"❌ *Git download failed:* `{str(e)[:150]}`")
                
        await download_queue.put(git_dl_task)
        return

    if action == "git_asset_dl":
        asset_url_id = data[1]
        cached = URL_CACHE.get(asset_url_id)
        if not cached:
            await query.answer("❌ Download expired.")
            return
            
        await query.answer("⏳ Downloading release asset...")
        
        async def asset_task():
            try:
                bot = query.message.get_bot()
                chat_id = query.message.chat_id
                status = await query.message.reply_text("📥 *Downloading release asset...*")
                
                with tempfile.TemporaryDirectory() as temp_dir:
                    filepath = os.path.join(temp_dir, cached['title'])
                    proxy_url = get_proxy_url()
                    async with aiohttp.ClientSession() as session:
                        async with session.get(cached['url'], proxy=proxy_url) as resp:
                            if resp.status == 200:
                                with open(filepath, "wb") as f:
                                    f.write(await resp.read())
                    
                    await status.edit_text("📤 *Uploading release asset...*")
                    with open(filepath, "rb") as f:
                        await bot.send_document(chat_id=chat_id, document=f, filename=cached['title'])
                    await status.delete()
            except Exception as e:
                logger.error(f"Release asset download failed: {e}")
                await bot.send_message(chat_id=chat_id, text=f"❌ Failed to download release asset: {e}")
                
        await download_queue.put(asset_task)
        return

    if action == "gitdl_cancel":
        await query.delete_message()
        return

    # Handle Converter Guides & Active Mode Actions
    if action == "conv_guide":
        guide_type = data[1]
        if guide_type == "cancel":
            await query.delete_message()
            return
        elif guide_type == "back":
            keyboard = [
                [
                    InlineKeyboardButton("📉 Video Compressor", callback_data="conv_guide:compress"),
                    InlineKeyboardButton("🎬 Subtitle Burner", callback_data="conv_guide:sub")
                ],
                [
                    InlineKeyboardButton("🎭 Voice Effects", callback_data="conv_guide:vfx"),
                    InlineKeyboardButton("🏷 Music Tag Editor", callback_data="conv_guide:tags")
                ],
                [
                    InlineKeyboardButton("🔒 PDF Security", callback_data="conv_guide:pdfsec"),
                    InlineKeyboardButton("📂 PDF Album Builder", callback_data="conv_guide:pdfalbum")
                ],
                [
                    InlineKeyboardButton("❌ Close", callback_data="conv_guide:cancel")
                ]
            ]
            await query.edit_message_text(
                f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                f"┃  🔄  *MEDIA ENGINE*       ┃\n"
                f"┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                f"Select a tool or upload any file directly.\n\n"
                f"👇 _Choose a tool:_",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return

        guide_texts = {
            'compress': (
                "📉 *Video Compressor / فشرده‌سازی ویدیو* 📉\n\n"
                "To compress a video file, please *upload/send the video file* directly to the bot now.\n\n"
                "I will automatically detect it and show you quality compression options."
            ),
            'sub': (
                "🎬 *Subtitle Burner / هاردکد کردن زیرنویس* 🎬\n\n"
                "To burn subtitles into a video:\n"
                "1. First, *upload/send the video file* directly to this bot.\n"
                "2. When the options keyboard appears, click *Add Subtitle (زیرنویس)*.\n"
                "3. You will then be prompted to send the `.srt` subtitle file."
            ),
            'vfx': (
                "🎭 *Voice & Audio Effects / افکت‌های صدا* 🎭\n\n"
                "To apply fun voice effects (Alien, Chipmunk, Robot, Echo):\n"
                "1. *Upload/send your audio or voice message* file directly to the bot.\n"
                "2. Click *Voice Effects* on the conversion options menu."
            ),
            'tags': (
                "🏷 *Music Tag Editor / ویرایشگر تگ* 🏷\n\n"
                "To edit MP3 album, artist, and title metadata tags:\n"
                "1. *Upload/send your MP3/audio file* to the bot.\n"
                "2. Click *Edit Tags* on the options keyboard.\n"
                "3. Enter the tags in the requested format."
            ),
            'pdfsec': (
                "🔒 *PDF Security / امنیت پی‌دی‌اف* 🔒\n\n"
                "To lock (encrypt) or unlock (decrypt) a PDF file:\n"
                "1. *Upload/send your PDF file* directly to the bot.\n"
                "2. Select *Protect PDF* or *Unlock PDF* on the options keyboard.\n"
                "3. Enter the desired password."
            )
        }

        if guide_type == "pdfalbum":
            USER_STATES[user_id] = {
                'state': 'AWAITING_PDF_ALBUM_IMAGES',
                'pdf_album': []
            }
            keyboard = [
                [InlineKeyboardButton("📄 Compile PDF Album", callback_data="pdfalbum_build_active")],
                [InlineKeyboardButton("❌ Cancel / Exit Mode", callback_data="pdfalbum_cancel")]
            ]
            await query.edit_message_text(
                "📂 *PDF Album Builder Mode / ساخت آلبوم پی‌دی‌اف* 📂\n\n"
                "Send your images now! They will be added to the album. When finished, click the button below.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return

        text = guide_texts.get(guide_type, "Invalid selection.")
        keyboard = [[InlineKeyboardButton("◀️ Back (بازگشت)", callback_data="conv_guide:back")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if action == "pdfalbum_build_active":
        album = USER_STATES.get(user_id, {}).get('pdf_album', [])
        if not album:
            USER_STATES.pop(user_id, None)  # Clear stale state
            await query.edit_message_text("❌ No images in PDF album. Please send some images first!")
            return

        await query.edit_message_text("⏳ *Building and compiling PDF album...*", parse_mode="Markdown")

        async def pdf_album_task():
            try:
                status_msg = await query.message.reply_text("🚀 *PDF Album compilation started...*")
                with tempfile.TemporaryDirectory() as temp_dir:
                    bot = query.message.get_bot()
                    image_paths = []
                    from PIL import Image

                    for idx, img_file_id in enumerate(album):
                        await status_msg.edit_text(f"📥 *Downloading image {idx+1}/{len(album)}...*")
                        tg_file = await bot.get_file(img_file_id)
                        img_path = os.path.join(temp_dir, f"img_{idx}.jpg")
                        await tg_file.download_to_drive(img_path)
                        image_paths.append(img_path)

                    await status_msg.edit_text("📄 *Generating PDF...*")
                    pdf_path = os.path.join(temp_dir, "album.pdf")
                    opened_images = [Image.open(img_p).convert("RGB") for img_p in image_paths]

                    if opened_images:
                        opened_images[0].save(pdf_path, save_all=True, append_images=opened_images[1:])

                        await status_msg.edit_text("📤 *Uploading PDF Album...*")
                        with open(pdf_path, "rb") as f:
                            await bot.send_document(
                                chat_id=query.message.chat_id,
                                document=f,
                                filename="album.pdf",
                                reply_to_message_id=query.message.reply_to_message.message_id if query.message.reply_to_message else None
                            )
                        await status_msg.delete()
                    else:
                        await status_msg.edit_text("❌ Failed to compile album: images could not be loaded.")
            except Exception as e:
                logger.error(f"PDF Album build failed: {e}")
                await query.message.reply_text(f"❌ *Failed to build PDF album:* `{str(e)[:150]}`")

            if user_id in USER_STATES and 'pdf_album' in USER_STATES[user_id]:
                USER_STATES[user_id].pop('pdf_album', None)
            USER_STATES.pop(user_id, None)

        await download_queue.put(pdf_album_task)
        return

    if action == "pdfalbum_cancel":
        USER_STATES.pop(user_id, None)
        await query.edit_message_text("❌ *PDF Album Builder mode canceled and exited.*", parse_mode="Markdown")
        return

    # Handle Search Pagination Page request
    if action == "src":
        page = int(data[1])
        query_uuid = data[2]
        cached_query = URL_CACHE.get(query_uuid)
        if not cached_query:
            await query.edit_message_text("❌ Search session expired. Please search again.")
            return
        
        query_text = cached_query["query"]
        await query.edit_message_text("🔍 *Loading search page... / در حال بارگذاری*", parse_mode="Markdown")
        await render_search_page(query.message, query_text, page, query_uuid, edit=True)
        return

    # Handle Pornhub Search Pagination request
    if action == "phsrc":
        page = int(data[1])
        query_uuid = data[2]
        cached_query = URL_CACHE.get(query_uuid)
        if not cached_query:
            await query.edit_message_text("❌ Search session expired. Please search again.")
            return
            
        query_text = cached_query["query"]
        await query.edit_message_text("🔍 *Loading Pornhub search page... / در حال بارگذاری*", parse_mode="Markdown")
        await render_phsearch_page(query.message, query_text, page, query_uuid, edit=True)
        return

    # Handle Pornhub video preview request
    if action == "phprev":
        url_id = data[1]
        cached = URL_CACHE.get(url_id)
        if not cached:
            await query.edit_message_text("❌ Session expired.")
            return
            
        preview_url = cached.get('preview_url')
        title = cached.get('title', 'Pornhub Video')
        
        if not preview_url:
            await query.edit_message_text("❌ No preview video found for this item.")
            return
            
        await query.edit_message_text("⏳ *Loading and sending video preview... / ارسال پیش‌نمایش*", parse_mode="Markdown")
        
        async def preview_task():
            bot = query.message.get_bot()
            chat_id = query.message.chat_id
            reply_id = query.message.message_id
            await send_ph_preview(bot, chat_id, preview_url, title, reply_id)
            
        await download_queue.put(preview_task)
        return

    # Handle Resolution Menu
    if action == "resopts":
        url_id = data[1]
        cached = URL_CACHE.get(url_id)
        if not cached:
            await query.edit_message_text("❌ Session expired. Please send link again.")
            return

        keyboard = [
            [
                InlineKeyboardButton("🎥 1080p (Full HD)", callback_data=f"dmethod:{url_id}:1080p"),
                InlineKeyboardButton("🎥 720p (HD)", callback_data=f"dmethod:{url_id}:720p")
            ],
            [
                InlineKeyboardButton("🎥 480p (Medium)", callback_data=f"dmethod:{url_id}:480p"),
                InlineKeyboardButton("🎥 360p (Low)", callback_data=f"dmethod:{url_id}:360p")
            ],
            [
                InlineKeyboardButton("◀️ Back (بازگشت)", callback_data=f"opt:{url_id}")
            ]
        ]
        await query.edit_message_text(
            f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
            f"┃  ⚙️  *RESOLUTION*         ┃\n"
            f"┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
            f"🎥 `{cached['title']}`\n\n"
            f"👇 _Choose your resolution:_",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    # Handle Resolution Specific Download
    if action == "resdl":
        url_id = data[1]
        resolution = data[2]
        cached = URL_CACHE.get(url_id)
        if not cached:
            await query.edit_message_text("❌ Session expired. Please send link again.")
            return

        url = cached['url']
        start_t = cached.get('start_time')
        end_t = cached.get('end_time')
        
        await query.edit_message_text(f"⏳ *Adding video ({resolution}) to download queue...*", parse_mode="Markdown")
        await add_to_queue(
            url=url,
            message_to_reply=query.message,
            format_opt=resolution,
            custom_name=None,
            start_time=start_t,
            end_time=end_t,
            as_gif=False,
            cached_title=cached['title'],
            cached_thumb=cached['thumbnail'],
            user_id=user_id
        )
        URL_CACHE.pop(url_id, None)
        return

    # Handle Search Options Choice
    if action == "opt":
        url_id = data[1]
        cached = URL_CACHE.get(url_id)
        if not cached:
            await query.edit_message_text("❌ Session expired. Please send link again.")
            return

        duration_str = format_seconds(cached['duration']) if cached.get('duration') else "?"
        title = cached.get('title', 'Video')
        title_short = title[:55] + ("…" if len(title) > 55 else "")

        keyboard = [
            [
                InlineKeyboardButton("🎥 Best Quality (HD)", callback_data=f"dl:{url_id}:video"),
                InlineKeyboardButton("🎤 MP3 Audio",         callback_data=f"dl:{url_id}:audio"),
            ],
            [
                InlineKeyboardButton("⚙️ 1080p", callback_data=f"dl:{url_id}:1080p"),
                InlineKeyboardButton("⚙️ 720p",  callback_data=f"dl:{url_id}:720p"),
                InlineKeyboardButton("⚙️ 480p",  callback_data=f"dl:{url_id}:480p"),
            ],
            [
                InlineKeyboardButton("⚙️ 360p",      callback_data=f"dl:{url_id}:360p"),
                InlineKeyboardButton("✂️ Trim / Cut", callback_data=f"dl:{url_id}:trim"),
            ],
            [InlineKeyboardButton("❌ Cancel / لغو", callback_data=f"dl:{url_id}:cancel")]
        ]
        if cached.get('duration') and cached['duration'] <= 15:
            keyboard[1].append(InlineKeyboardButton("🖼 GIF", callback_data=f"dl:{url_id}:gif"))

        await query.edit_message_text(
            f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
            f"┃  📊  *DOWNLOAD*           ┃\n"
            f"┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
            f"🎥 `{title_short}`\n"
            f"⏱ `{duration_str}`\n\n"
            f"👇 _کیفیت مورد نظر را انتخاب کنید:_",
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

    # Handle Download Method — routes directly to queue (legacy compat kept)
    if action == "dmethod":
        url_id = data[1]
        format_choice = data[2]
        cached = URL_CACHE.get(url_id)
        if not cached:
            await query.edit_message_text("❌ Session expired. Send link again.")
            return
        url = cached['url']
        start_t = cached.get('start_time')
        end_t   = cached.get('end_time')
        as_gif  = (format_choice == "gif")
        if format_choice == "audio":
            fmt_opt = "audio"
        elif format_choice in ("1080p", "720p", "480p", "360p"):
            fmt_opt = format_choice
        elif format_choice == "gif":
            fmt_opt = "video"
        else:
            fmt_opt = "video"
        await query.edit_message_text("⏳ *Queued for download...*", parse_mode="Markdown")
        await add_to_queue(
            url=url, message_to_reply=query.message,
            format_opt=fmt_opt, custom_name=None,
            start_time=start_t, end_time=end_t, as_gif=as_gif,
            cached_title=cached['title'], cached_thumb=cached['thumbnail'],
            user_id=user_id,
            audio_title=cached.get('audio_title'),
            audio_performer=cached.get('audio_performer')
        )
        URL_CACHE.pop(url_id, None)
        return

    # Handle Direct Downloads / Cancellations / Trims
    if action == "dl":
        url_id = data[1]
        choice = data[2]
        delivery = data[3] if len(data) > 3 else None
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
                f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                f"┃  ✂️  *TRIM VIDEO*         ┃\n"
                f"┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                f"Reply with start and end times:\n"
                f"`MM:SS - MM:SS` or `HH:MM:SS - HH:MM:SS`\n\n"
                f"📌 Example: `01:30 - 02:15`",
                parse_mode="Markdown"
            )
            return

        # Route download directly
        url    = cached['url']
        start_t = cached.get('start_time')
        end_t   = cached.get('end_time')
        as_gif  = (choice == "gif")

        if choice == "audio":
            fmt_opt = "audio"
        elif choice in ("1080p", "720p", "480p", "360p"):
            fmt_opt = choice
        elif choice in ("gif", "file"):
            fmt_opt = "video" if choice == "gif" else None
        else:
            fmt_opt = "video"

        await query.edit_message_text("⏳ *Queued! / اضافه شد به صف*", parse_mode="Markdown")
        await add_to_queue(
            url=url,
            message_to_reply=query.message,
            format_opt=fmt_opt,
            custom_name=None,
            start_time=start_t,
            end_time=end_t,
            as_gif=as_gif,
            cached_title=cached['title'],
            cached_thumb=cached['thumbnail'],
            user_id=user_id,
            audio_title=cached.get('audio_title'),
            audio_performer=cached.get('audio_performer')
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

        # Intercept interactive conversion actions:
        if target_format == "sub":
            USER_STATES[user_id] = {
                'state': 'AWAITING_SUBTITLE',
                'video_file_uuid': file_uuid
            }
            await query.edit_message_text(
                f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                f"┃  💬  *ADD SUBTITLE*       ┃\n"
                f"┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                f"Send the `.srt` subtitle file now.\n"
                f"I will burn it into the video.",
                parse_mode="Markdown"
            )
            return

        if target_format == "tags":
            USER_STATES[user_id] = {
                'state': 'AWAITING_TAG_EDIT',
                'audio_file_uuid': file_uuid
            }
            await query.edit_message_text(
                f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                f"┃  🏷  *EDIT TAGS*          ┃\n"
                f"┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                f"Send tags in this format:\n"
                f"`Artist - Title - Album`\n\n"
                f"📌 Example: `Coldplay - Yellow - Parachutes`",
                parse_mode="Markdown"
            )
            return

        if target_format == "vfx":
            keyboard = [
                [
                    InlineKeyboardButton("👽 Alien", callback_data=f"vfxdl:{file_uuid}:alien"),
                    InlineKeyboardButton("🐿 Chipmunk", callback_data=f"vfxdl:{file_uuid}:chipmunk")
                ],
                [
                    InlineKeyboardButton("🤖 Robot", callback_data=f"vfxdl:{file_uuid}:robot"),
                    InlineKeyboardButton("📻 Echo/Radio", callback_data=f"vfxdl:{file_uuid}:echo")
                ],
                [
                    InlineKeyboardButton("◀️ Back", callback_data=f"conv:{file_uuid}:cancel")
                ]
            ]
            await query.edit_message_text(
                f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                f"┃  🎭  *VOICE EFFECTS*      ┃\n"
                f"┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                f"Choose an effect for your audio:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return

        if target_format == "pdfalbum":
            if user_id not in USER_STATES:
                USER_STATES[user_id] = {}
            if 'pdf_album' not in USER_STATES[user_id]:
                USER_STATES[user_id]['pdf_album'] = []
            USER_STATES[user_id]['pdf_album'].append(file_id)
            album_size = len(USER_STATES[user_id]['pdf_album'])
            
            keyboard = [
                [
                    InlineKeyboardButton("📄 Generate PDF Album Now", callback_data=f"pdfalbum_build:{file_uuid}")
                ],
                [
                    InlineKeyboardButton("➕ Add More Images", callback_data=f"conv:{file_uuid}:cancel")
                ]
            ]
            await query.edit_message_text(
                f"📁 *Image Added to PDF Album!* 📁\n\n"
                f"Count of images currently: `{album_size}`\n\n"
                f"Send another image to add to the album, or click below to build the PDF album now.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return

        if target_format == "pdfprotect":
            USER_STATES[user_id] = {
                'state': 'AWAITING_PDF_PROTECT_PASS',
                'file_uuid': file_uuid
            }
            await query.edit_message_text(
                f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                f"┃  🔒  *PROTECT PDF*        ┃\n"
                f"┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                f"Reply with the password to set:",
                parse_mode="Markdown"
            )
            return

        if target_format == "pdfunlock":
            USER_STATES[user_id] = {
                'state': 'AWAITING_PDF_UNLOCK_PASS',
                'file_uuid': file_uuid
            }
            await query.edit_message_text(
                f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                f"┃  🔓  *UNLOCK PDF*         ┃\n"
                f"┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                f"Reply with the PDF password:",
                parse_mode="Markdown"
            )
            return

        await query.edit_message_text("⏳ *Adding conversion task to queue...*", parse_mode="Markdown")
        await add_conversion_to_queue(file_id, filename, target_format, query.message, user_id)
        URL_CACHE.pop(file_uuid, None)

    # Voice Effects Callback
    if action == "vfxdl":
        file_uuid = data[1]
        effect = data[2]
        cached = URL_CACHE.get(file_uuid)
        if not cached:
            await query.edit_message_text("❌ Session expired.")
            return
        
        file_id = cached["file_id"]
        filename = cached["filename"]
        target_format = f"vfx_{effect}"
        await query.edit_message_text(f"⏳ *Adding voice filter ({effect}) task to queue...*", parse_mode="Markdown")
        await add_conversion_to_queue(file_id, filename, target_format, query.message, user_id)
        URL_CACHE.pop(file_uuid, None)
        return

    # PDF Album Generator Callback
    if action == "pdfalbum_build":
        file_uuid = data[1]
        album = USER_STATES.get(user_id, {}).get('pdf_album', [])
        if not album:
            USER_STATES.pop(user_id, None)  # Clear stale state
            await query.edit_message_text("❌ No images in PDF album.")
            return
            
        await query.edit_message_text("⏳ *Building and compiling PDF album...*", parse_mode="Markdown")
        
        async def pdf_album_task():
            try:
                status_msg = await query.message.reply_text("🚀 *PDF Album compilation started...*")
                with tempfile.TemporaryDirectory() as temp_dir:
                    bot = query.message.get_bot()
                    image_paths = []
                    from PIL import Image
                    
                    for idx, img_file_id in enumerate(album):
                        await status_msg.edit_text(f"📥 *Downloading image {idx+1}/{len(album)}...*")
                        tg_file = await bot.get_file(img_file_id)
                        img_path = os.path.join(temp_dir, f"img_{idx}.jpg")
                        await tg_file.download_to_drive(img_path)
                        image_paths.append(img_path)
                    
                    await status_msg.edit_text("📄 *Generating PDF...*")
                    pdf_path = os.path.join(temp_dir, "album.pdf")
                    opened_images = [Image.open(img_p).convert("RGB") for img_p in image_paths]
                    
                    if opened_images:
                        opened_images[0].save(pdf_path, save_all=True, append_images=opened_images[1:])
                        
                        await status_msg.edit_text("📤 *Uploading PDF Album...*")
                        with open(pdf_path, "rb") as f:
                            await bot.send_document(
                                chat_id=query.message.chat_id,
                                document=f,
                                filename="album.pdf",
                                reply_to_message_id=query.message.reply_to_message.message_id if query.message.reply_to_message else None
                            )
                        await status_msg.delete()
                    else:
                        await status_msg.edit_text("❌ Failed to compile album: images could not be loaded.")
            except Exception as e:
                logger.error(f"PDF Album build failed: {e}")
                await query.message.reply_text(f"❌ *Failed to build PDF album:* `{str(e)[:150]}`")
                
            if user_id in USER_STATES and 'pdf_album' in USER_STATES[user_id]:
                USER_STATES[user_id].pop('pdf_album', None)
        
        await download_queue.put(pdf_album_task)
        URL_CACHE.pop(file_uuid, None)
        return

# =====================================================================
# Queue Execution System
# =====================================================================
async def add_to_queue(url, message_to_reply, format_opt, custom_name, start_time=None, end_time=None, as_gif=False, cached_title=None, cached_thumb=None, user_id=None, audio_title=None, audio_performer=None, upload_mode=None):
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
                    # Capture format_opt in local variable to avoid closure issues
                    _fmt = format_opt
                    _st = start_time
                    _et = end_time

                    filepath = None
                    # Cobalt public APIs now require JWT — skip for YouTube (android_vr works).
                    # Still try Cobalt first for adult sites that often 403 datacenter IPs.
                    if is_impersonate_site(url) and not is_youtube_url(url) and not _st and not _et:
                        try:
                            await status_msg.edit_text("📥 *Bypassing restrictions (Cobalt)...*", parse_mode="Markdown")
                            filepath = await _cobalt_download(url, temp_dir, audio_only=(_fmt == "audio"))
                            if filepath and os.path.exists(filepath):
                                logger.info(f"Successfully bypassed download blocks using Cobalt: {filepath}")
                                # Rename to custom name or cached title if available
                                ext = os.path.splitext(filepath)[1]
                                final_title = custom_name or cached_title or "media"
                                final_name = "".join([c for c in final_title if c.isalnum() or c in (' ', '.', '_', '-')]).strip()
                                if not final_name.lower().endswith(ext.lower()):
                                    final_name += ext
                                new_filepath = os.path.join(temp_dir, final_name)
                                os.rename(filepath, new_filepath)
                                filepath = new_filepath
                        except Exception as ce:
                            logger.warning(f"Cobalt download failed, falling back to yt-dlp: {ce}")

                    if not filepath:
                        # yt-dlp Video / Audio download (primary path for YouTube + music)
                        await status_msg.edit_text("📥 *Downloading media (yt-dlp)... / در حال دانلود*", parse_mode="Markdown")
                        bot_obj = status_msg.get_bot()
                        tracker = ProgressTracker(context_bot_wrapper(status_msg), chat_id, status_msg.message_id, loop)
                        filepath = await loop.run_in_executor(
                            None, lambda: download_yt(url, temp_dir, _fmt, _st, _et, tracker)
                        )
                else:
                    # Resilient Direct HTTP chunk downloader
                    await status_msg.edit_text("📥 *Downloading direct file...*", parse_mode="Markdown")
                    filepath = await download_direct_resilient(
                        url,
                        filepath=os.path.join(temp_dir, "file"),
                        progress_message=status_msg,
                        bot=status_msg.get_bot(),
                        custom_name=custom_name
                    )

                if filepath and os.path.exists(filepath):
                    if upload_mode == "web":
                        await status_msg.edit_text("🌐 *Uploading file to Uplod.ir... / در حال آپلود به سرور وب*", parse_mode="Markdown")
                        web_link = await upload_to_uplod_ir(filepath)
                        if web_link:
                             await status_msg.edit_text(f"✅ *Upload Successful! / آپلود موفق*\n\n📥 *Download Link:* {web_link}", parse_mode="Markdown", disable_web_page_preview=True)
                        else:
                            await status_msg.edit_text("❌ *Failed to upload file to Uplod.ir.*")
                    else:
                        # Send media to Telegram
                        await send_file_to_telegram(
                            bot=status_msg.get_bot(),
                            chat_id=chat_id,
                            filepath=filepath,
                            reply_to_message_id=message_to_reply.message_id,
                            thumbnail_path=thumbnail_path,
                            as_gif=as_gif,
                            user_id=user_id,
                            audio_title=audio_title,
                            audio_performer=audio_performer
                        )
                        await status_msg.delete()
                else:
                    await status_msg.edit_text("❌ *Download completed but file was not found locally.*")
        except Exception as e:
            logger.error(f"Task failed: {e}", exc_info=True)
            err_msg = str(e)
            try:
                logger.error(f"Download error detail: {err_msg[:500]}")
                if (isinstance(e, AssertionError) or "assertion" in err_msg.lower()) and "cookie" in err_msg.lower():
                    await status_msg.edit_text(
                        f"❌ *Cookie Format Error / خطای فرمت کوکی*\n\n"
                        f"The uploaded `cookies.txt` file contains formatting errors. "
                        f"Please re-export using *Get cookies.txt LOCALLY* extension and send again.",
                        parse_mode="Markdown"
                    )
                elif any(x in err_msg.lower() for x in ["video unavailable", "this video is not available", "has been removed", "private video", "members-only"]):
                    await status_msg.edit_text(
                        f"❌ *Video Unavailable / ویدیو در دسترس نیست*\n\n"
                        f"این ویدیو حذف شده، خصوصی است، یا در منطقه شما پخش نمی‌شود.\n"
                        f"The video is deleted, private, or geo-restricted.",
                        parse_mode="Markdown"
                    )
                elif any(x in err_msg.lower() for x in ["confirm you", "not a bot", "forbidden", "403", "429", "sign in"]):
                    await status_msg.edit_text(
                        f"❌ *Download Blocked / دانلود بلاک شد*\n\n"
                        f"`{err_msg[:250]}`\n\n"
                        f"⚠️ لینک دیگری امتحان کنید یا چند دقیقه صبر کنید.",
                        parse_mode="Markdown"
                    )
                elif "format is not available" in err_msg.lower():
                    await status_msg.edit_text(
                        f"❌ *Format not available / فرمت موجود نیست*\n\n"
                        f"Try a different resolution or format.",
                        parse_mode="Markdown"
                    )
                else:
                    display_msg = err_msg if err_msg.strip() else f"Unknown error ({type(e).__name__})"
                    await status_msg.edit_text(f"❌ *Failed:* `{display_msg[:250]}`", parse_mode="Markdown")
            except Exception:
                pass

    pos = download_queue.qsize() + 1
    if pos > 1:
        await status_msg.edit_text(f"⏳ *Queue Position:* #{pos}\nPlease wait for other tasks to complete...")
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
                ydl_opts = {
                    'quiet': True,
                    'no_warnings': True,
                    'extract_flat': True,
                    'extractor_args': get_youtube_extractor_args(),
                    'js_runtimes': {'node': {}},
                    **get_ydl_cookie_opts(url),
                    **get_site_specific_opts(url),
                }
                proxy_url = get_proxy_url()
                if proxy_url:
                    ydl_opts["proxy"] = proxy_url
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
                        await status_msg.edit_text(f"📥 *[ZIP Build] Downloading {idx + 1}/{len(entries)}:* `{entry.get('title', '')[:30]}...`", parse_mode="Markdown")
                        try:
                            tracker = ProgressTracker(context_bot_wrapper(status_msg), chat_id, status_msg.message_id, loop)
                            _eurl = entry_url
                            # Sync wrapper download
                            fn = await loop.run_in_executor(
                                None, lambda: download_yt(_eurl, temp_dir, "video", None, None, tracker)
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
                    await register_file_for_upload(bot, chat_id, output_filepath, os.path.basename(output_filepath))
                else:
                    await status_msg.edit_text("❌ *Conversion failed.* Could not build output file.")
        except Exception as e:
            logger.error(f"Conversion failed: {e}", exc_info=True)
            await status_msg.edit_text(f"❌ *Conversion failed:* `{str(e)[:150]}`", parse_mode="Markdown")

    pos = download_queue.qsize() + 1
    if pos > 1:
        await status_msg.edit_text(f"⏳ *Queue Position:* #{pos}\nWaiting for other tasks to complete...")
    await download_queue.put(conversion_task)

# context_bot_wrapper is defined earlier in the file (before ProgressTracker)

_download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

async def queue_worker(bot):
    """Processes downloads with controlled concurrency from queue."""
    while True:
        task = await download_queue.get()
        async def _run_task(t):
            async with _download_semaphore:
                try:
                    await t()
                except Exception as e:
                    logger.error(f"Queue task error: {e}", exc_info=True)
        asyncio.create_task(_run_task(task))
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

async def cookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to manage cookies.txt for bot-detection bypass."""
    user_id = update.effective_user.id
    
    is_admin = False
    if ADMIN_USER_ID and ADMIN_USER_ID.strip() and "YOUR_ADMIN_USER_ID" not in ADMIN_USER_ID:
        if str(user_id) == str(ADMIN_USER_ID):
            is_admin = True
    else:
        is_admin = True  # Allow if no admin configured yet

    if not is_admin:
        await update.message.reply_text(
            f"❌ This command is only available to admins.\n"
            f"Your Telegram User ID is: `{user_id}`\n"
            f"Please add this ID to your `.env` file to become the admin:\n"
            f"`ADMIN_USER_ID={user_id}`",
            parse_mode="Markdown"
        )
        return

    if os.path.exists(COOKIES_FILE):
        size = os.path.getsize(COOKIES_FILE)
        mod_time = time.ctime(os.path.getmtime(COOKIES_FILE))
        await update.message.reply_text(
            f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
            f"┃  🍪  *COOKIES STATUS*     ┃\n"
            f"┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
            f"✅ `cookies.txt` is *active*\n"
            f"📦 Size: `{size} bytes`\n"
            f"🕐 Updated: `{mod_time}`\n\n"
            f"💡 *Render.com Note:* If you redeploy or restart the bot, this file will be deleted. "
            f"To keep cookies permanently on Render, copy the file contents and save it in the Render dashboard environment variables as `YOUTUBE_COOKIES_CONTENT`.\n\n"
            f"_To update: send a new `cookies.txt` file._",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
            f"┃  🍪  *COOKIES SETUP*      ┃\n"
            f"┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
            f"❌ No `cookies.txt` found.\n"
            f"Bot detection is *active* on YouTube.\n\n"
            f"*To fix:*\n"
            f"1️⃣ Install *Get cookies.txt LOCALLY*\n"
            f"2️⃣ Visit youtube.com while *logged in*\n"
            f"3️⃣ Export cookies as `cookies.txt`\n"
            f"4️⃣ Send the file *directly to this bot*\n\n"
            f"💡 *Render.com Persistent Cookies:*\n"
            f"Since Render containers have ephemeral disks, uploaded files disappear on restart/redeploy. "
            f"To make cookies permanent, copy the contents of `cookies.txt` and add it as an Environment Variable named "
            f"`YOUTUBE_COOKIES_CONTENT` in your Render dashboard settings.\n\n"
            f"📁 Saved to: `{COOKIES_FILE}`",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )

async def direct_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Force download a URL directly using HTTP Range download bypass."""
    message = update.message
    user_id = update.effective_user.id
    
    args = context.args
    if not args:
        await message.reply_text(
            "❌ Please specify the URL:\n"
            "`/direct <url> [--name custom_filename.ext]`\n\n"
            "Example:\n"
            "`/direct https://example.com/data --name database.zip`",
            parse_mode="Markdown"
        )
        return
        
    text = " ".join(args)
    
    # Parse Custom Rename Command
    custom_name = None
    name_match = re.search(r'--name\s+(\S+)', text)
    if name_match:
        custom_name = name_match.group(1)
        text = re.sub(r'--name\s+\S+', '', text).strip()
        
    url_match = re.search(r"https?://\S+", text)
    if not url_match:
        await message.reply_text("❌ Please send a valid link starting with http or https.")
        return
        
    url = url_match.group(0)
    
    # Bypass redirects
    status_msg = await message.reply_text("🔍 *Resolving link redirects... / در حال بررسی لینک*", parse_mode="Markdown")
    resolved_url = await bypass_url(url)
    await status_msg.delete()
    
    # Enqueue as direct download task (format_opt=None forces direct download)
    await add_to_queue(
        url=resolved_url,
        message_to_reply=message,
        format_opt=None,
        custom_name=custom_name,
        user_id=user_id
    )

async def main_async(application):
    # Clear any lingering webhook so /start and polling work reliably on Render
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook cleared — polling mode active.")
    except Exception as e:
        logger.warning(f"Could not clear webhook: {e}")

    await start_web_server()
    task = asyncio.create_task(queue_worker(application.bot))
    task.add_done_callback(lambda t: logger.error(f"Queue worker crashed: {t.exception()}") if not t.cancelled() and t.exception() else None)

    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    logger.info("Bot is polling in the background...")
    while True:
        await asyncio.sleep(3600)

async def local_main_async(application):
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    task = asyncio.create_task(queue_worker(application.bot))
    task.add_done_callback(lambda t: logger.error(f"Queue worker crashed: {t.exception()}") if not t.cancelled() and t.exception() else None)
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    logger.info("Bot started. Listening for messages...")
    while True:
        await asyncio.sleep(3600)

def main():
    if not BOT_TOKEN or "YOUR_TELEGRAM_BOT_TOKEN_HERE" in BOT_TOKEN:
        print("❌ Error: TELEGRAM_BOT_TOKEN is not set in .env file!")
        print(f"Please edit the .env file in {BASE_DIR} and set TELEGRAM_BOT_TOKEN.")
        return

    logger.info("Starting Telegram Bot...")
    proxy_url = get_proxy_url()
    if proxy_url:
        logger.info(f"Routing python-telegram-bot traffic through proxy: {proxy_url}")
        application = Application.builder().token(BOT_TOKEN).proxy(proxy_url).get_updates_proxy(proxy_url).build()
    else:
        application = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("phsearch", phsearch_command))
    application.add_handler(CommandHandler("github_token", github_token_command))
    application.add_handler(CommandHandler("gitlab_token", gitlab_token_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("cookies", cookies_command))
    application.add_handler(CommandHandler("direct", direct_command))
    application.add_handler(CallbackQueryHandler(handle_callback))
    
    # Catch textual requests (including URLs)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Catch incoming file attachments (images, PDFs, documents, audio streams, videos)
    application.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO | filters.AUDIO | filters.VOICE | filters.VIDEO, handle_incoming_file))

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
