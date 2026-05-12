"""
config.py
----------
Centralized configuration for the web crawler & scraper.
Change settings here instead of hunting through files.
"""

# ── Browser ──────────────────────────────────────────────────────────────────
HEADLESS_MODE = True          # True = no browser window; False = visible window
TIMEOUT = 15                  # Seconds to wait for a page element to appear
PAGE_LOAD_TIMEOUT = 30        # Seconds before Selenium gives up loading a page
SCROLL_PAUSE = 1.0            # Seconds to pause between scroll steps
SCROLL_STEPS = 3              # How many times to scroll down per page

# ── Crawl behaviour ──────────────────────────────────────────────────────────
MAX_PAGES = 20                # Default maximum pages to crawl (overridden by CLI)
RETRY_LIMIT = 2               # How many times to retry a failing page
RETRY_DELAY = 2               # Seconds to wait before each retry
REQUEST_DELAY = 1.0           # Polite delay (seconds) between page visits

# ── Content extraction ───────────────────────────────────────────────────────
MAX_CONTENT_WORDS = 400       # Word cap on saved content_summary
MAX_HEADINGS = 10             # Maximum headings to include per page
MIN_PARAGRAPH_WORDS = 5       # Ignore paragraphs shorter than this

# ── URL filtering ────────────────────────────────────────────────────────────
# Add strings that a URL *must* contain (empty list = accept all)
URL_INCLUDE_KEYWORDS: list = []
# Add strings that will cause a URL to be *rejected* (empty list = reject none)
URL_EXCLUDE_KEYWORDS: list = ["logout", "login", "signup", "cart", "checkout"]

# ── Output ───────────────────────────────────────────────────────────────────
OUTPUT_FORMAT = "both"        # "csv" | "json" | "both"
OUTPUT_DIR = "output"
LOGS_DIR = "logs"
LOG_FILE = "logs/scraper.log"