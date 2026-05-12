"""
crawler.py
-----------
BFS crawler that finds and queues internal links.

UPGRADES (v2):
  - URL include/exclude keyword filtering (from config.py)
  - Retry mechanism: retries a failing URL up to config.RETRY_LIMIT times
  - Polite request delay between page visits (config.REQUEST_DELAY)
  - Uses professional logger from utils.setup_logger()

UPGRADES (v3 — fault-tolerance):
  - should_retry() now uses exponential backoff (2 s, 4 s, …)
  - Permanently-failed URLs recorded in self.skipped for reporting
  - Default URL_EXCLUDE_KEYWORDS extended with common problem paths
  - extract_links() wrapped in a broad try-except so a dead session
    never raises out of the crawler
"""

import time
from collections import deque

from bs4 import BeautifulSoup
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By

from utils import is_same_domain, is_valid_url, normalize_url
import config


class Crawler:
    """BFS crawler that finds internal links from a website."""

    def __init__(self, base_url: str, max_pages: int, logger,
                 url_include: list = None, url_exclude: list = None):
        """
        Args:
            base_url:    The seed URL (homepage of the site).
            max_pages:   Stop after visiting this many pages.
            logger:      A Python logging.Logger instance.
            url_include: Only enqueue URLs containing at least one of these strings.
                         Defaults to config.URL_INCLUDE_KEYWORDS (empty = accept all).
            url_exclude: Reject URLs containing any of these strings.
                         Defaults to config.URL_EXCLUDE_KEYWORDS.
        """
        self.base_url = base_url.rstrip("/")
        self.max_pages = max_pages
        self.logger = logger

        # URL filtering lists (lower-cased for case-insensitive matching)
        self.url_include = [k.lower() for k in (url_include or config.URL_INCLUDE_KEYWORDS)]
        self.url_exclude = [k.lower() for k in (url_exclude or config.URL_EXCLUDE_KEYWORDS)]

        self.visited: set = set()
        self.queue: deque = deque([self.base_url])

        # retry tracking:  url -> attempt count
        self._retry_counts: dict = {}

        # permanently skipped URLs (exhausted retries)
        self.skipped: set = set()

        # Hard-coded problem paths merged with config exclusions (v3)
        _extra_exclude = [
            "login", "logout", "signup", "sign-up", "register",
            "account", "terms", "privacy", "contact",
            "password", "reset", "verify", "unsubscribe",
        ]
        for kw in _extra_exclude:
            if kw not in self.url_exclude:
                self.url_exclude.append(kw)

    # ── URL filtering ─────────────────────────────────────────────────────────

    def _passes_filters(self, url: str) -> bool:
        """
        Returns True only when a URL passes the include/exclude rules.

        Include rule: if url_include is non-empty, the URL must contain
                      at least one of the include keywords.
        Exclude rule: the URL must NOT contain any of the exclude keywords.
        """
        lower = url.lower()

        if self.url_include and not any(kw in lower for kw in self.url_include):
            self.logger.debug(f"URL filtered out (include rule): {url}")
            return False

        if any(kw in lower for kw in self.url_exclude):
            self.logger.debug(f"URL filtered out (exclude rule): {url}")
            return False

        return True

    # ── Link extraction ───────────────────────────────────────────────────────

    def extract_links(self, driver, current_url: str) -> list:
        """Extracts all valid, same-domain links from the current page.
        Returns an empty list (instead of raising) if the driver session is dead.
        """
        try:
            WebDriverWait(driver, config.TIMEOUT).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except Exception:
            self.logger.warning(f"Timeout waiting for body on: {current_url}")

        try:
            soup = BeautifulSoup(driver.page_source, "lxml")
        except Exception as exc:
            self.logger.warning(f"Could not read page source for {current_url}: {exc}")
            return []

        links = []

        for a in soup.find_all("a", href=True):
            raw = a["href"].strip()
            full = normalize_url(current_url, raw)

            if (
                is_valid_url(full)
                and is_same_domain(self.base_url, full)
                and full not in self.visited
                and self._passes_filters(full)
            ):
                links.append(full)

        unique = list(set(links))
        self.logger.debug(f"Found {len(unique)} new links on {current_url}")
        return unique

    # ── Queue management ──────────────────────────────────────────────────────

    def has_next(self) -> bool:
        """True while there are URLs to visit and we haven't hit the page cap."""
        return bool(self.queue) and len(self.visited) < self.max_pages

    def next_url(self):
        """Pop the next unvisited URL from the queue (BFS order)."""
        while self.queue:
            url = self.queue.popleft()
            if url not in self.visited:
                return url
        return None

    def add_links(self, links: list):
        """Append new URLs to the queue (skip already-visited or already-queued)."""
        for link in links:
            if link not in self.visited and link not in self.queue:
                self.queue.append(link)

    def mark_visited(self, url: str):
        """Record a URL as visited."""
        self.visited.add(url)

    # ── Retry mechanism ───────────────────────────────────────────────────────

    def should_retry(self, url: str) -> bool:
        """
        Returns True if *url* should be retried (attempts < RETRY_LIMIT).

        Backoff schedule (v3 — exponential):
          attempt 1 → wait 2 s
          attempt 2 → wait 4 s
          attempt N → wait 2^N s   (capped at 30 s)

        When all attempts are exhausted the URL is added to self.skipped
        and False is returned so the caller can move on.
        """
        attempts = self._retry_counts.get(url, 0) + 1
        self._retry_counts[url] = attempts

        if attempts <= config.RETRY_LIMIT:
            wait = min(2 ** attempts, 30)          # exponential, max 30 s
            self.logger.warning(
                f"Retry {attempts}/{config.RETRY_LIMIT} for {url}  "
                f"(backoff {wait}s)"
            )
            self.queue.appendleft(url)             # retry next
            time.sleep(wait)
            return True

        self.logger.error(
            f"Skipping URL permanently after {config.RETRY_LIMIT} retries: {url}"
        )
        self.skipped.add(url)
        return False

    # ── Polite delay ──────────────────────────────────────────────────────────

    def polite_delay(self):
        """Sleep briefly between requests to avoid hammering the server."""
        if config.REQUEST_DELAY > 0:
            time.sleep(config.REQUEST_DELAY)