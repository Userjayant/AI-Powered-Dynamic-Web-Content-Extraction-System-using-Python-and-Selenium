"""
utils.py
---------
Helper functions used across the project.

v3 — PRODUCTION UTILITIES
  - setup_logger: writes to logs/scraper.log + console
  - get_timestamp(): compact timestamp for filenames
  - content_hash(): MD5 dedup fingerprint
  - clean_text(): improved whitespace normalisation
  - limit_words(): soft truncation with ellipsis
  - normalize_url(): absolute URL resolution + fragment strip
  - is_same_domain(): domain comparison ignoring www.
  - is_valid_url(): rejects assets, anchors, mailto, tel, js links
"""

import re
import os
import hashlib
import logging
from datetime import datetime
from urllib.parse import urlparse, urljoin

import config


# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logger(name: str = "WebCrawler") -> logging.Logger:
    """
    Returns a logger that writes to BOTH the console and logs/scraper.log.
    Log format: 10:30:01 | INFO | your message here
    """
    logger = logging.getLogger(name)

    # Avoid adding duplicate handlers if called more than once
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )

    # Console handler (INFO and above)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler (DEBUG and above → full detail in log file)
    os.makedirs(config.LOGS_DIR, exist_ok=True)
    file_handler = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


# ── Timestamp / filenames ─────────────────────────────────────────────────────

def get_timestamp() -> str:
    """Returns a compact timestamp string, e.g. '20240512_103045'."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


# ── URL helpers ───────────────────────────────────────────────────────────────

def get_domain(url: str) -> str:
    """Returns the bare domain of a URL.  https://www.abc.com/page → abc.com"""
    return urlparse(url).netloc.replace("www.", "")


def is_same_domain(base_url: str, link: str) -> bool:
    """True if *link* lives on the same domain as *base_url*."""
    return get_domain(base_url) == get_domain(link)


def is_valid_url(url: str) -> bool:
    """
    True when the URL is a crawlable HTTP(S) page.
    Rejects static assets, anchors, mailto/tel/javascript links.
    """
    if not url:
        return False

    skip_extensions = (
        ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp",
        ".zip", ".rar", ".tar", ".gz",
        ".mp4", ".mp3", ".avi", ".mov", ".webm",
        ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".exe", ".dmg", ".apk",
        ".css", ".js", ".json", ".xml",
        ".ico", ".ttf", ".woff", ".woff2", ".eot",
    )
    lower = url.lower()
    if any(lower.endswith(ext) for ext in skip_extensions):
        return False
    if url.startswith(("mailto:", "tel:", "javascript:", "#", "data:", "blob:")):
        return False

    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def normalize_url(base: str, link: str) -> str:
    """Converts a relative URL to absolute and strips the fragment (#…)."""
    try:
        absolute = urljoin(base, link)
        # Strip fragment
        absolute = absolute.split("#")[0]
        # Remove trailing slash for consistency (except root)
        parsed = urlparse(absolute)
        if parsed.path not in ("", "/"):
            absolute = absolute.rstrip("/")
        return absolute
    except Exception:
        return link


# ── Text helpers ──────────────────────────────────────────────────────────────

def clean_text(text: str) -> str:
    """
    Normalise whitespace and strip leading/trailing spaces.
    Also removes non-printable control characters (except newline/tab).
    """
    if not text:
        return ""
    # Remove control characters except tab and newline
    text = re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]", "", text)
    # Collapse all whitespace (including \t, \r, \n) to single space
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def limit_words(text: str, max_words: int = 400) -> str:
    """Keeps only the first *max_words* words; appends '…' when truncated."""
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]) + "…"


# ── Duplicate detection ───────────────────────────────────────────────────────

def content_hash(text: str) -> str:
    """
    Returns a short MD5 hex digest of normalised text.
    Used to detect duplicate/near-duplicate pages before saving.
    Normalisation: lowercase, collapse whitespace, strip punctuation.
    """
    normalised = re.sub(r"[^\w\s]", "", text.lower())
    normalised = re.sub(r"\s+", " ", normalised).strip()
    return hashlib.md5(normalised.encode("utf-8")).hexdigest()