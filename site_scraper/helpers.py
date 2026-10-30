"""Shared imports, constants and small helper functions."""

import argparse
import asyncio
import csv
import gzip
import hashlib
import io
import ipaddress
import json
import re
import signal
import socket
import sqlite3
import sys
import threading
import time
import zlib
from collections import Counter, defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from .amon_welcome import welcome, step, info, warn, fail, pick_one, summary_box
try:
    from .amon_welcome import SUNRISE, _gradient
except ImportError:  # older/embedded copy without the palette helpers
    SUNRISE = [(255, 228, 120), (255, 176, 80), (255, 112, 140), (200, 112, 235), (120, 165, 255)]

    def _gradient(stops, t):
        t = min(max(t, 0.0), 1.0) * (len(stops) - 1)
        i = min(int(t), len(stops) - 2)
        a, b, f = stops[i], stops[i + 1], t - i
        return tuple(int(round(a[k] + (b[k] - a[k]) * f)) for k in range(3))

# Live terminal dashboard (optional; plain progress lines without it)
try:
    from rich import box
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:
    Live = None

try:
    import truststore
    truststore.inject_into_ssl()  # use the Windows certificate store (works behind the corporate proxy)
except ImportError:
    pass

# Optional libraries: each one improves a part; without it the script falls back to a simpler method.
try:
    import trafilatura
except ImportError:
    trafilatura = None
try:
    import extruct
except ImportError:
    extruct = None
try:
    import phonenumbers
except ImportError:
    phonenumbers = None
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None
try:
    import pandas as pd
except ImportError:
    pd = None
try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/140.0.0.0 Safari/537.36")
DOC_EXT = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".csv", ".zip", ".txt", ".rtf", ".odt"}
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".avif"}
SKIP_EXT = {".css", ".js", ".mjs", ".map", ".woff", ".woff2", ".ttf", ".eot", ".otf", ".ico", ".mp4", ".webm",
            ".mov", ".mp3", ".wav", ".m4a", ".json", ".xml", ".rss", ".atom", ".exe", ".dmg", ".apk"}
TRACKING = re.compile(r"^(utm_.*|gclid|fbclid|msclkid|dclid|yclid|mc_cid|mc_eid|_ga|_gl|ref|ref_src|source|"
                      r"trk|igshid|si|hsa_.*|_hs.*|sessionid|session_id|sid|phpsessid|jsessionid|cfid|cftoken)$", re.I)
SOCIAL = {"linkedin.com": "LinkedIn", "twitter.com": "X/Twitter", "x.com": "X/Twitter", "facebook.com": "Facebook",
          "instagram.com": "Instagram", "youtube.com": "YouTube", "github.com": "GitHub", "tiktok.com": "TikTok",
          "pinterest.com": "Pinterest", "threads.net": "Threads", "wa.me": "WhatsApp", "medium.com": "Medium",
          "xing.com": "Xing", "t.me": "Telegram"}
CHALLENGE_RE = re.compile(r"<title>\s*(just a moment|attention required|access denied|are you a robot|"
                          r"verify you are human)|cf-chl|challenge-platform|captcha-delivery|/cdn-cgi/challenge", re.I)
NOT_PROFILE_RE =re.compile(r"status|posts?|watch|issues|pull|blob|tree|search|hashtag|share|sharer|intent|home|"
                            r"dialog|login|signup|explore|sharing|p|reel|embed|releases|discussions", re.I)
MEDIA_HOSTS =("youtube.com", "youtu.be", "vimeo.com", "loom.com", "wistia", "vidyard")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}")
AT_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-]+)\s*[\[\(\{]\s*at\s*[\]\)\}]\s*([A-Za-z0-9-]+(?:\s*[\[\(\{]\s*dot\s*"
                         r"[\]\)\}]\s*[A-Za-z0-9-]+)+)", re.I)
NOT_FOUND_RE = re.compile(r"\b(404|page not found|not found|page (?:does not|doesn.t) exist|nicht gefunden)\b", re.I)
PLACEHOLDER_RE = re.compile(r"lorem ipsum|this is some text inside of a div block|heading goes here|"
                            r"your text here|placeholder text", re.I)
LOAD_MORE_RE =re.compile(r"^\s*(load|show|view|see) more|mehr (laden|anzeigen)|more results", re.I)
TLD_REGION = {"in": "IN", "de": "DE", "uk": "GB", "fr": "FR", "es": "ES", "it": "IT", "nl": "NL", "au": "AU",
              "ca": "CA", "ch": "CH", "at": "AT", "se": "SE", "sg": "SG", "ae": "AE", "us": "US", "ie": "IE"}
EXCEL_MAX_ROWS = 1_000_000
EXCEL_MAX_CELL = 32_000


# ─── Small helpers ──────────────────────────────────────────────────────────────

def clean(text):
    return " ".join((text or "").split())


def safe_join(base, href):
    """urljoin that returns '' for malformed links (e.g. 'http://[broken') instead of raising."""
    try:
        joined = urljoin(base, (href or "").strip())
        urlsplit(joined)
        return joined
    except ValueError:
        return ""


def ext_of(url):
    path = urlsplit(url).path.lower()
    dot = path.rfind(".")
    return path[dot:] if dot > path.rfind("/") else ""


def reg_domain(host):
    """example.co.uk / www.example.com -> example.co.uk / example.com (good enough without a suffix list)."""
    parts = host.lower().split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "ac", "gov", "edu"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def slugify(text, limit=80):
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")
    return (s or "index")[:limit]


def fit_name(folder, stem, ext, max_path=250):
    """File name in `folder` that keeps the full path under Windows' 260-character limit.
    Long stems are cut and given a short hash so they stay unique."""
    room = max_path - len(str(folder)) - 1 - len(ext)
    if len(stem) <= room:
        return stem + ext
    tag = hashlib.sha1(stem.encode()).hexdigest()[:8]
    return stem[:max(1, room - 9)].rstrip("-._") + "_" + tag + ext


def safe_cell(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        value = ", ".join(str(v) for v in value)
    elif isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        value = ILLEGAL_CHARACTERS_RE.sub("", value)[:EXCEL_MAX_CELL]
    return value


FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value):
    """Scraped text that starts like a formula gets a leading ' so Excel shows it instead of running it
    (CSV/formula injection, e.g. a page titled '=HYPERLINK(...)')."""
    if isinstance(value, str) and value.startswith(FORMULA_START):
        return "'" + value
    return value


def csv_row(values):
    return [csv_safe(v) for v in values]


_host_cache = {}


def is_public_host(host):
    """False for hosts that resolve to loopback/private/link-local/reserved addresses (the user's own network).
    Stops a scraped page from making this machine call e.g. http://192.168.1.1/ or cloud metadata endpoints."""
    host = (host or "").split("@")[-1].rsplit(":", 1)[0].strip("[]").lower()
    if host in _host_cache:
        return _host_cache[host]
    try:
        addrs = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except (socket.gaierror, UnicodeError, ValueError):
        addrs = set()
    ok = bool(addrs)
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast \
                or ip.is_unspecified:
            ok = False
    _host_cache[host] = ok
    return ok


def read_capped(response, cap):
    """Body of a streamed response, decompressed, but never more than `cap` bytes (guards against huge/bomb files)."""
    try:
        return response.raw.read(cap, decode_content=True) or b""
    finally:
        response.close()


def gunzip_capped(data, cap=200 * 1024 * 1024):
    return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data, cap)


def parse_duration(text):
    if not text:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhd]?)", text.strip().lower())
    if not m:
        raise SystemExit(f"--max-time: cannot read '{text}' (use e.g. 90m, 2h, 3600)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def to_markdown(node):
    """Fallback HTML -> Markdown for when trafilatura is missing or finds nothing."""
    lines = []
    for el in node.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "pre", "td"]):
        if el.find_parent(["li", "blockquote", "td"]) and el.name in ("p", "li"):
            continue
        text = clean(el.get_text(" "))
        if not text:
            continue
        if el.name[0] == "h" and el.name[1:].isdigit():
            lines.append("#" * int(el.name[1]) + " " + text)
        elif el.name == "li":
            lines.append("- " + text)
        elif el.name == "blockquote":
            lines.append("> " + text)
        elif el.name == "pre":
            lines.append("```\n" + el.get_text() + "\n```")
        else:
            lines.append(text)
    return "\n\n".join(dict.fromkeys(lines))
