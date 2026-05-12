"""
scraper_service.py
-------------------
Service layer that sits between the FastAPI backend and the core scraping
engine (main.run).

Responsibilities
----------------
- Validate and normalise the incoming URL before any browser is launched.
- Call main.run() with save_files=False so no disk I/O happens during an
  API request.
- Build a clean, serialisable ScrapeResponse object for the API to return.
- Translate every possible scraper exception into a user-friendly error
  message — the API layer never sees a raw Selenium traceback.

Nothing in this file touches Selenium directly; all scraping logic stays in
the existing modules (crawler.py, scraper.py, driver.py).
"""

import io
import csv
import time
import logging
from urllib.parse import urlparse

import config
from main import run          # ← core engine, untouched
from utils import setup_logger


logger = setup_logger("scraper_service")


# ── Input / Output models (plain dataclasses — no Pydantic needed here) ──────

class ScrapeRequest:
    """Validated parameters for a single scrape job."""

    MAX_PAGES_LIMIT = 50     # hard ceiling regardless of what the caller sends

    def __init__(self, url: str, max_pages: int = 10, keyword: str = None):
        self.url       = self._validate_url(url)
        self.max_pages = max(1, min(int(max_pages), self.MAX_PAGES_LIMIT))
        self.keyword   = keyword.strip() if keyword else None

    @staticmethod
    def _validate_url(raw: str) -> str:
        raw = raw.strip()
        if not raw:
            raise ValueError("URL must not be empty.")
        if not raw.startswith(("http://", "https://")):
            raw = "https://" + raw
        parsed = urlparse(raw)
        if not parsed.netloc:
            raise ValueError(f"Could not parse a valid domain from: {raw!r}")
        return raw


class ScrapeResponse:
    """
    Serialisable result object returned to the API.

    Attributes
    ----------
    success       bool    — False means the scrape failed entirely.
    data          list    — List of page dicts (url, title, …).
    pages_crawled int
    pages_saved   int
    pages_skipped int
    base_url      str
    duration_sec  float   — Wall-clock time of the scrape job.
    error         str     — Human-friendly error message (empty on success).
    """

    def __init__(self, *, success: bool, data: list = None,
                 pages_crawled: int = 0, pages_saved: int = 0,
                 pages_skipped: int = 0, base_url: str = "",
                 duration_sec: float = 0.0, error: str = ""):
        self.success       = success
        self.data          = data or []
        self.pages_crawled = pages_crawled
        self.pages_saved   = pages_saved
        self.pages_skipped = pages_skipped
        self.base_url      = base_url
        self.duration_sec  = round(duration_sec, 2)
        self.error         = error

    def to_dict(self) -> dict:
        return {
            "success":       self.success,
            "data":          self.data,
            "pages_crawled": self.pages_crawled,
            "pages_saved":   self.pages_saved,
            "pages_skipped": self.pages_skipped,
            "base_url":      self.base_url,
            "duration_sec":  self.duration_sec,
            "error":         self.error,
        }


# ── CSV export helper ─────────────────────────────────────────────────────────

def build_csv_string(data: list) -> str:
    """
    Converts a list of page dicts into a UTF-8 CSV string.
    Used by the /download endpoint so the client can save the file.
    """
    if not data:
        return ""
    fieldnames = ["url", "title", "meta_description", "headings", "content_summary", "ai_summary"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(data)
    return buf.getvalue()


# ── Core service function ─────────────────────────────────────────────────────

def scrape(url: str,
           max_pages: int = config.MAX_PAGES,
           keyword: str = None) -> ScrapeResponse:
    """
    Public entry point for the FastAPI backend.

    Parameters
    ----------
    url        : Target website (with or without scheme).
    max_pages  : How many pages to crawl (capped at 50).
    keyword    : Optional keyword filter forwarded to the scraper.

    Returns
    -------
    ScrapeResponse — always returns, never raises.
    """
    # ── 1. Validate input ─────────────────────────────────────────────────────
    try:
        req = ScrapeRequest(url=url, max_pages=max_pages, keyword=keyword)
    except ValueError as ve:
        logger.warning(f"Invalid scrape request: {ve}")
        return ScrapeResponse(success=False, error=str(ve))

    logger.info(
        f"Scrape job started | url={req.url} | "
        f"max_pages={req.max_pages} | keyword={req.keyword!r}"
    )

    t_start = time.monotonic()

    # ── 2. Run the scraper ────────────────────────────────────────────────────
    try:
        raw = run(
            base_url=req.url,
            max_pages=req.max_pages,
            keyword=req.keyword,
            headless=True,       # always headless when called from the API
            save_files=False,    # API doesn't write to disk
        )
    except Exception as exc:
        duration = time.monotonic() - t_start
        error_msg = _friendly_error(exc)
        logger.error(f"Scrape job failed after {duration:.1f}s: {exc}")
        return ScrapeResponse(
            success=False,
            base_url=req.url,
            duration_sec=duration,
            error=error_msg,
        )

    duration = time.monotonic() - t_start
    logger.info(
        f"Scrape job finished | pages_saved={raw['pages_saved']} | "
        f"duration={duration:.1f}s"
    )

    return ScrapeResponse(
        success=True,
        data=raw["results"],
        pages_crawled=raw["pages_crawled"],
        pages_saved=raw["pages_saved"],
        pages_skipped=raw["pages_skipped"],
        base_url=raw["base_url"],
        duration_sec=duration,
    )


# ── Error translation ─────────────────────────────────────────────────────────

def _friendly_error(exc: Exception) -> str:
    """Maps technical exceptions to user-readable messages."""
    msg = str(exc).lower()

    if "err_name_not_resolved" in msg or "net::err" in msg:
        return "Could not reach the website. Please check the URL and try again."
    if "timeout" in msg:
        return "The website took too long to respond. Try again or reduce page count."
    if "invalid session" in msg or "session" in msg:
        return "Browser session crashed unexpectedly. Please try again."
    if "chrome" in msg or "webdriver" in msg or "chromedriver" in msg:
        return "Browser failed to start. Ensure Chrome and ChromeDriver are installed."
    if "connection refused" in msg:
        return "Connection refused. The site may be blocking automated requests."
    if "certificate" in msg or "ssl" in msg:
        return "SSL/TLS certificate error on the target site."

    # Fallback: truncated raw message so it's still somewhat useful
    return f"Scraping failed: {str(exc)[:120]}"