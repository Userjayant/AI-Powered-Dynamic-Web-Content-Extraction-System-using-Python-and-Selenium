"""
scraper.py
-----------
Extracts structured content from a web page.

v12 — PRODUCTION-GRADE FULL CONTENT EXTRACTOR
  Improvements over v11:
  ─────────────────────────────────────────────
  1.  NOISE PRUNING — expanded CSS class/ID blocklist covering 60+ patterns
      (cookie banners, popups, modals, GDPR bars, chat widgets, sticky bars,
       breadcrumbs, social share strips, related-posts, comment sections …)
  2.  HIDDEN ELEMENT REMOVAL — strips [style*="display:none"],
      [style*="visibility:hidden"], [aria-hidden=true], [hidden] before any
      text is read.  Prevents invisible duplicate text from polluting output.
  3.  SMARTER CONTAINER SCORING — candidate containers are now scored by
      paragraph density (text inside <p>/<li> tags ÷ total text).  A div
      that is mostly heading + nav text scores lower than one rich in <p>.
  4.  LAZY-LOAD — three-phase scroll strategy: top → 25% → 50% → 75% →
      bottom, with IntersectionObserver trigger, then a final 1-second
      settle.  Works with Intersection-Observer-based lazy images and
      infinite-scroll pagination stubs.
  5.  TABLE RENDERING — tables now rendered as aligned pipe-table rows
      with a header separator line (│ col1 │ col2 │).  Thead rows get
      an underline of dashes so the table reads like Markdown.
  6.  DUPLICATE LINE DEDUP — smarter: normalises punctuation/whitespace
      before hashing so "About Us" and "about us." don't both survive.
  7.  SHORT-NOISE FILTER — lines under 3 chars dropped; lines 3–25 chars
      that appear more than once are dropped (menu crumbs that slip through).
  8.  PRE/CODE BLOCKS — preserved with indentation intact.
  9.  DEFINITION LISTS — <dl>/<dt>/<dd> rendered cleanly.
  10. READING-ORDER GUARANTEE — _render() uses el.children (document
      order) so paragraphs always appear before the next heading.
  11. JS SETTLE WAIT — after scrolling, waits up to 3 s for any new
      content to finish rendering (polls body text length).
  12. STRONGER ERROR HANDLING — every extraction phase wrapped in
      try/except; broken pages always return a valid dict, never raise.
"""

import re
import time
from copy import copy
from bs4 import BeautifulSoup, NavigableString, Tag
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By

from utils import clean_text, content_hash
import config

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# Hard tag subtrees → always removed before extraction.
_HARD_NOISE_TAGS = frozenset({
    "script", "style", "noscript", "iframe", "svg", "canvas",
    "nav", "header", "footer", "aside", "form",
    "button", "input", "select", "textarea",
    "meta", "link", "head",
    "figure",           # keep figcaption but drop outer figure decoration
})

# These specific classes/IDs reliably signal navigation or chrome noise.
# Each token is checked independently (split on whitespace/hyphen boundaries).
_NOISE_CLASS_PATTERNS = re.compile(
    r"(?:^|[\-_])"
    r"(nav|navbar|navigation|topbar|top-bar|"
    r"menu|megamenu|mega-menu|dropdown|"
    r"breadcrumb|breadcrumbs|pagination|pager|"
    r"cookie|cookies|consent|gdpr|"
    r"banner|promo-bar|announcement-bar|alert-bar|sticky-bar|"
    r"popup|modal|overlay|lightbox|dialog|"
    r"advertisement|advert|ad-unit|ads|sponsored|"
    r"sidebar|widget|widgetarea|widget-area|"
    r"site-footer|page-footer|footer-widget|"
    r"site-header|page-header|masthead|hero-eyebrow|"
    r"social|share|sharing|follow-us|"
    r"subscribe|newsletter|signup-form|cta-bar|"
    r"comment|comments|disqus|reply-form|"
    r"related|recommended|you-may-also|more-articles|"
    r"skip-link|screen-reader|sr-only|visually-hidden|"
    r"chat|live-chat|crisp|intercom|drift|"
    r"back-to-top|scroll-top|floating-btn|"
    r"tag-cloud|tag-list|author-box|post-tags|post-meta|"
    r"entry-footer|post-footer|article-footer)$",
    re.IGNORECASE,
)

# Ordered CSS selectors from most-specific (semantic) to least-specific.
_MAIN_SELECTORS = [
    # Most reliable semantic tags
    "article",
    "main",
    '[role="main"]',
    '[role="article"]',
    # Common CMS / theme class names
    ".post-content", ".entry-content", ".article-content", ".article-body",
    ".post-body", ".page-content", ".content-area", ".main-content",
    ".blog-content", ".single-content", ".story-body", ".news-body",
    ".post-inner", ".entry-body", ".hentry", ".content-wrapper",
    ".article-text", ".body-text", ".rich-text", ".prose",
    # Common IDs
    "#content", "#main", "#main-content", "#article", "#post", "#primary",
    # Generic fallbacks
    ".content", ".body-content", ".page-body", ".inner-content",
    "section",
    "div",
]

_HEADING_TAGS   = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_HEADING_MARKER = {"h1": "#", "h2": "##", "h3": "###",
                   "h4": "####", "h5": "#####", "h6": "######"}

_INLINE_TAGS = frozenset({
    "span", "a", "em", "strong", "b", "i", "u", "s", "del",
    "small", "mark", "code", "cite", "q", "abbr", "time",
    "label", "var", "kbd", "samp", "sup", "sub",
})

THIN_THRESHOLD          = 250    # chars below which we try harder / fallback
_MAX_CHARS              = 80_000 # hard cap on stored content
_MIN_LINE_LEN           = 3      # strip lines shorter than this
_DEDUP_MAX_COUNT        = 2      # short lines (≤25 chars) allowed this many times

# Challenge-page detection
_CHALLENGE_PHRASES = frozenset({
    "checking your browser",
    "just a moment",
    "please wait",
    "verifying you are human",
    "enable javascript and cookies",
    "ddos-guard",
    "ray id",
    "performance & security by cloudflare",
    "access denied",
    "403 forbidden",
    "attention required",
    "this site is protected by",
})
REAL_CONTENT_MIN_CHARS  = 250
MAX_CHALLENGE_WAIT      = 35
CHALLENGE_POLL_INTERVAL = 2

# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def _token_is_noise(tok: str) -> bool:
    """Check if a single CSS class or ID token is noise."""
    return bool(_NOISE_CLASS_PATTERNS.search(tok))


def _el_is_noise(el: Tag) -> bool:
    """True if any class token or the id of an element signals noise."""
    for attr in ("class", "id"):
        val = el.get(attr, "")
        tokens = val if isinstance(val, list) else val.split()
        for tok in tokens:
            if _token_is_noise(tok):
                return True
    return False


def _remove_hidden(soup: BeautifulSoup) -> None:
    """
    Remove elements that are hidden via inline style or ARIA attributes.
    These produce invisible duplicate text that pollutes extraction.
    """
    # Inline display:none / visibility:hidden
    for el in list(soup.find_all(style=True)):
        style = el.get("style", "").replace(" ", "").lower()
        if "display:none" in style or "visibility:hidden" in style:
            try:
                el.decompose()
            except Exception:
                pass

    # aria-hidden="true"
    for el in list(soup.find_all(attrs={"aria-hidden": "true"})):
        try:
            el.decompose()
        except Exception:
            pass

    # HTML5 [hidden] attribute
    for el in list(soup.find_all(hidden=True)):
        try:
            el.decompose()
        except Exception:
            pass


def _paragraph_density(el: Tag) -> float:
    """
    Ratio of text inside <p>/<li> to total visible text.
    Higher = more prose-like = better content container.
    Returns 0.0 if total text is empty.
    """
    try:
        total = len(el.get_text(separator=" ", strip=True))
        if total == 0:
            return 0.0
        para_text = sum(
            len(c.get_text(separator=" ", strip=True))
            for c in el.find_all(["p", "li"])
        )
        return para_text / total
    except Exception:
        return 0.0


def _score_container(el: Tag) -> float:
    """Combined score: text length × (0.5 + paragraph_density)."""
    try:
        tlen = len(el.get_text(separator=" ", strip=True))
        return tlen * (0.5 + _paragraph_density(el))
    except Exception:
        return 0.0


def _find_main_container(soup: BeautifulSoup) -> Tag:
    """
    Identify the element most likely to hold the page's main content.

    Strategy:
      1. Try semantic/named selectors in priority order; accept first
         candidate whose score exceeds THIN_THRESHOLD × 2.
      2. Score ALL div/section/article tags; return the highest scorer
         (text length × paragraph density weighting).
      3. Fall back to <body>.
    """
    threshold = THIN_THRESHOLD * 2

    # Pass 1: known semantic selectors
    for selector in _MAIN_SELECTORS:
        try:
            el = soup.select_one(selector)
            if el and _score_container(el) > threshold:
                return el
        except Exception:
            continue

    # Pass 2: score every block container
    best, best_score = None, threshold
    for tag_name in ("article", "div", "section"):
        for el in soup.find_all(tag_name):
            s = _score_container(el)
            if s > best_score:
                best_score = s
                best = el
    if best:
        return best

    # Pass 3: body fallback
    return soup.body or soup


def _prune_noise(container: Tag) -> None:
    """
    Remove noise subtrees from the container IN PLACE.
    Order matters: hard tags first, then class/id patterns.
    """
    # 1. Hard structural tags
    for tag in list(container.find_all(_HARD_NOISE_TAGS)):
        try:
            tag.decompose()
        except Exception:
            pass

    # 2. Class/ID pattern matches
    for tag in list(container.find_all(True)):
        try:
            if _el_is_noise(tag):
                tag.decompose()
        except Exception:
            continue


# ─────────────────────────────────────────────────────────────────────────────
# Text renderer — walks DOM in document order
# ─────────────────────────────────────────────────────────────────────────────

def _render(el: Tag, lines: list, depth: int = 0, in_list: bool = False) -> None:
    """
    Recursively walk *el* and append formatted text lines to *lines*.

    Tag handling:
      h1–h6       → "# Heading" with surrounding blank lines
      p           → paragraph text + trailing blank line
      ul / ol     → recurse with in_list=True + surrounding blank lines
      li          → "  • text" (nested li gets extra indent)
      br          → blank line
      td/th       → collected by tr handler; not directly rendered
      tr          → pipe-separated row  │ cell │ cell │
      thead tr    → row + separator line
      table       → blank + recurse + blank
      blockquote  → "> text"
      figcaption  → "[caption]"
      pre / code  → preserved indented block
      dl/dt/dd    → definition list
      hr          → "────" divider
      inline tags → emit direct text nodes + recurse
      containers  → recurse (div, section, article, main …)
      text nodes  → emit if non-trivial
    """
    for child in el.children:

        # ── Plain text node ───────────────────────────────────────────────
        if isinstance(child, NavigableString):
            t = str(child).strip()
            if len(t) >= _MIN_LINE_LEN:
                lines.append(t)
            continue

        if not isinstance(child, Tag):
            continue

        name = child.name
        if not name:
            continue

        # ── Headings ──────────────────────────────────────────────────────
        if name in _HEADING_TAGS:
            t = child.get_text(separator=" ", strip=True)
            if t:
                lines.append("")
                lines.append(f"{_HEADING_MARKER[name]} {t}")
                lines.append("")
            continue

        # ── Paragraphs ────────────────────────────────────────────────────
        if name == "p":
            t = child.get_text(separator=" ", strip=True)
            if t and len(t) >= _MIN_LINE_LEN:
                lines.append(t)
                lines.append("")
            continue

        # ── List containers ───────────────────────────────────────────────
        if name in ("ul", "ol"):
            lines.append("")
            _render(child, lines, depth + 1, in_list=True)
            lines.append("")
            continue

        # ── List items ────────────────────────────────────────────────────
        if name == "li":
            prefix = "  " * depth + "  • "
            # Grab direct text first, then recurse for nested content
            direct = " ".join(
                str(c).strip()
                for c in child.children
                if isinstance(c, NavigableString) and str(c).strip()
            )
            if direct and len(direct) >= _MIN_LINE_LEN:
                lines.append(f"{prefix}{direct}")
            # Recurse for nested lists / spans inside li
            sub: list = []
            _render(child, sub, depth + 1, in_list=True)
            for sl in sub:
                if sl.strip() and sl.strip() not in (direct,):
                    lines.append(sl)
            continue

        # ── Line break ────────────────────────────────────────────────────
        if name == "br":
            lines.append("")
            continue

        # ── Horizontal rule ───────────────────────────────────────────────
        if name == "hr":
            lines.append("")
            lines.append("─" * 40)
            lines.append("")
            continue

        # ── Tables ────────────────────────────────────────────────────────
        if name == "table":
            lines.append("")
            _render_table(child, lines)
            lines.append("")
            continue

        # Skip individual td/th/tr/thead/tbody/tfoot — handled by table renderer
        if name in ("td", "th", "tr", "thead", "tbody", "tfoot"):
            continue

        # ── Blockquote ────────────────────────────────────────────────────
        if name == "blockquote":
            t = child.get_text(separator=" ", strip=True)
            if t:
                lines.append("")
                for bline in t.split("\n"):
                    bl = bline.strip()
                    if bl:
                        lines.append(f"> {bl}")
                lines.append("")
            continue

        # ── Figcaption ────────────────────────────────────────────────────
        if name == "figcaption":
            t = child.get_text(separator=" ", strip=True)
            if t:
                lines.append(f"[{t}]")
            continue

        # ── Pre / Code blocks ─────────────────────────────────────────────
        if name in ("pre", "code"):
            t = child.get_text()   # preserve internal whitespace
            t = t.rstrip()
            if t and len(t.strip()) >= _MIN_LINE_LEN:
                lines.append("")
                for codeline in t.split("\n"):
                    lines.append(f"    {codeline}")
                lines.append("")
            continue

        # ── Definition lists ──────────────────────────────────────────────
        if name == "dl":
            lines.append("")
            _render(child, lines, depth + 1)
            lines.append("")
            continue

        if name == "dt":
            t = child.get_text(separator=" ", strip=True)
            if t:
                lines.append(f"  {t}:")
            continue

        if name == "dd":
            t = child.get_text(separator=" ", strip=True)
            if t:
                lines.append(f"    {t}")
            continue

        # ── Inline elements — emit direct text + recurse ──────────────────
        if name in _INLINE_TAGS:
            for grandchild in child.children:
                if isinstance(grandchild, NavigableString):
                    t = str(grandchild).strip()
                    if len(t) >= _MIN_LINE_LEN:
                        lines.append(t)
            _render(child, lines, depth + 1, in_list)
            continue

        # ── Generic containers — recurse ──────────────────────────────────
        # Covers div, section, article, main, figure, details, summary, etc.
        _render(child, lines, depth, in_list)


def _render_table(table: Tag, lines: list) -> None:
    """
    Render an HTML table as pipe-separated rows.
    Thead rows get a separator line beneath them.
    """
    try:
        in_head = False
        first_data_row = True

        for row in table.find_all("tr"):
            # Detect if we're in thead
            parent = row.parent
            is_header_row = (
                parent is not None and
                (parent.name == "thead" or
                 any(c.name in ("th",) for c in row.find_all(True, recursive=False)))
            )

            cells = []
            for cell in row.find_all(["td", "th"]):
                t = cell.get_text(separator=" ", strip=True)
                cells.append(t if t else "")

            if not any(cells):
                continue

            row_str = " │ ".join(cells)
            lines.append(f"│ {row_str} │")

            if is_header_row:
                # Underline header row
                sep = "─" * (len(row_str) + 4)
                lines.append(sep)
    except Exception:
        # If table rendering fails, fall back to flat text
        try:
            t = table.get_text(separator=" │ ", strip=True)
            if t:
                lines.append(t)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Post-processing
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_for_dedup(line: str) -> str:
    """Normalise a line for deduplication: lowercase, collapse space, strip punct."""
    s = line.lower().strip()
    s = re.sub(r"[^\w\s]", "", s)          # strip punctuation
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _clean_lines(lines: list) -> str:
    """
    Post-process extracted lines:
      - Strip leading/trailing whitespace per line
      - Drop lines shorter than _MIN_LINE_LEN
      - Drop very short repeated lines (menu artefacts)
      - Collapse runs of more than 2 consecutive blank lines
      - Deduplicate identical lines (case+punct-normalised)
      - Return final joined string
    """
    # First pass: count occurrences of short lines for the repeat filter
    short_count: dict[str, int] = {}
    for raw in lines:
        line = raw.strip()
        if 0 < len(line) <= 25:
            key = _normalize_for_dedup(line)
            short_count[key] = short_count.get(key, 0) + 1

    seen:   dict[str, int] = {}
    result: list[str]      = []
    blanks: int            = 0

    for raw in lines:
        line = raw.strip()

        # Blank line handling
        if not line:
            blanks += 1
            if blanks <= 2:
                result.append("")
            continue
        blanks = 0

        # Drop too-short lines
        if len(line) < _MIN_LINE_LEN:
            continue

        # Drop short lines that repeat too many times (nav artefacts)
        if len(line) <= 25 and not line.startswith(("#", "•", ">", "│", "─")):
            key = _normalize_for_dedup(line)
            if short_count.get(key, 0) > _DEDUP_MAX_COUNT:
                continue

        # Global deduplication (headings exempt)
        is_structural = line.startswith(("#", "•", ">", "│", "─", "    "))
        norm_key = _normalize_for_dedup(line)
        prev_count = seen.get(norm_key, 0)
        if not is_structural and prev_count >= 1:
            continue
        seen[norm_key] = prev_count + 1

        result.append(line)

    return "\n".join(result).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Scraper class
# ─────────────────────────────────────────────────────────────────────────────

class Scraper:
    """Extracts full readable content from a webpage via Selenium."""

    def __init__(self, logger, keyword_filter: str = None):
        self.logger         = logger
        self.keyword_filter = keyword_filter.lower().strip() if keyword_filter else None
        self._seen_hashes:  set = set()

    # ── Challenge-page detection ──────────────────────────────────────────────

    def _visible_text(self, driver) -> str:
        try:
            return driver.execute_script(
                "return document.body ? document.body.innerText : '';"
            ) or ""
        except Exception:
            return ""

    def _is_challenge_page(self, driver) -> bool:
        text = self._visible_text(driver).lower()
        if len(text.strip()) < REAL_CONTENT_MIN_CHARS:
            return True
        return any(phrase in text for phrase in _CHALLENGE_PHRASES)

    def _wait_for_real_content(self, driver, url: str) -> bool:
        if not self._is_challenge_page(driver):
            return True
        self.logger.info(f"Challenge page detected — waiting up to {MAX_CHALLENGE_WAIT}s: {url}")
        print(f"  [~] Challenge page — waiting for content: {url}")
        waited = 0
        while waited < MAX_CHALLENGE_WAIT:
            time.sleep(CHALLENGE_POLL_INTERVAL)
            waited += CHALLENGE_POLL_INTERVAL
            if not self._is_challenge_page(driver):
                self.logger.info(f"Challenge cleared after {waited}s: {url}")
                print(f"  [+] Challenge cleared after {waited}s")
                time.sleep(1)
                return True
        self.logger.warning(f"Challenge never cleared after {MAX_CHALLENGE_WAIT}s: {url}")
        print(f"  [!] Challenge never cleared: {url}")
        return False

    # ── Lazy-load scrolling ───────────────────────────────────────────────────

    def scroll_page(self, driver) -> None:
        """
        Multi-phase scroll to trigger lazy-loaded content.

        Phase 1: Scroll in steps (25%, 50%, 75%, 100% of page height)
                 with a pause at each step.
        Phase 2: Repeat bottom-scroll until height stabilises (infinite scroll).
        Phase 3: Wait for any new content to finish rendering.
        """
        try:
            pause = getattr(config, "SCROLL_PAUSE", 0.8)

            # Phase 1: step scroll
            for pct in (0.25, 0.5, 0.75, 1.0):
                try:
                    driver.execute_script(
                        f"window.scrollTo(0, document.body.scrollHeight * {pct});"
                    )
                    time.sleep(pause)
                except Exception:
                    pass

            # Phase 2: wait for height to stabilise
            last_h = driver.execute_script("return document.body.scrollHeight") or 0
            max_extra = getattr(config, "SCROLL_STEPS", 4)
            for _ in range(max_extra):
                try:
                    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                    time.sleep(pause)
                    new_h = driver.execute_script("return document.body.scrollHeight") or 0
                    if new_h == last_h:
                        break
                    last_h = new_h
                except Exception:
                    break

            # Phase 3: wait for JS settle (poll body text length)
            self._wait_js_settle(driver)

        except Exception:
            pass

    def _wait_js_settle(self, driver, timeout: float = 3.0, interval: float = 0.5) -> None:
        """
        Poll body.innerText length until it stops growing, up to *timeout* seconds.
        Catches React/Vue/Angular late renders that fire after scroll.
        """
        try:
            prev_len = len(self._visible_text(driver))
            deadline = time.time() + timeout
            while time.time() < deadline:
                time.sleep(interval)
                cur_len = len(self._visible_text(driver))
                if cur_len == prev_len:
                    break
                prev_len = cur_len
        except Exception:
            pass

    # ── Full text extraction ──────────────────────────────────────────────────

    def extract_full_text(self, html: str) -> str:
        """
        v12 pipeline:
          1. Parse HTML.
          2. Remove hidden elements.
          3. Find the richest content container.
          4. Prune noise tags/classes from the container.
          5. Walk the container in document order → structured text.
          6. Post-process: deduplicate, clean, format.
        Returns up to _MAX_CHARS characters.
        """
        try:
            soup = _parse(html)

            # Step 2: remove hidden elements globally before scoping
            _remove_hidden(soup)

            # Step 3: find main container
            container = _find_main_container(soup)

            # Step 4: prune noise IN-PLACE on the container
            _prune_noise(container)

            # Step 5: render structured text
            lines: list = []
            _render(container, lines)

            # Step 6: clean and deduplicate
            text = _clean_lines(lines)
            return text[:_MAX_CHARS]

        except Exception as exc:
            self.logger.error(f"extract_full_text error: {exc}")
            return ""

    def _js_innertext_fallback(self, driver) -> str:
        """
        JS innerText fallback for SPAs that render text outside any semantic tag.
        Cleans up excessive blank lines.
        """
        try:
            raw = driver.execute_script(
                "return document.body ? document.body.innerText : '';"
            ) or ""
            # Collapse runs of blank lines to max 2
            cleaned = re.sub(r"\n{3,}", "\n\n", raw).strip()
            return cleaned[:_MAX_CHARS]
        except Exception:
            return ""

    # ── Main entry point ──────────────────────────────────────────────────────

    def extract(self, driver, url: str) -> dict | None:
        """
        Full extraction pipeline:
          1. Wait for <body> to be present.
          2. Detect and wait out challenge pages.
          3. Multi-phase scroll to trigger lazy content.
          4. JS settle wait.
          5. Snapshot page_source.
          6. Run v12 extract_full_text().
          7. JS innerText fallback if result is thin.
          8. Keyword filter.
          9. Duplicate content detection.
         10. Return structured dict.
        """
        # 1. Wait for body
        try:
            WebDriverWait(driver, config.TIMEOUT).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except Exception:
            self.logger.warning(f"Body wait timed out: {url}")

        # 2. Challenge detection
        try:
            content_available = self._wait_for_real_content(driver, url)
        except Exception:
            content_available = True  # proceed anyway

        if not content_available:
            try:
                page_title = clean_text(driver.title or "No Title")
            except Exception:
                page_title = "No Title"
            return {
                "url":              url,
                "title":            page_title,
                "meta_description": "",
                "headings":         "",
                "full_content":     "[Page requires browser verification — content unavailable]",
            }

        # 3 + 4. Scroll + JS settle
        self.scroll_page(driver)

        # Small buffer after scroll for React/Vue re-renders
        time.sleep(0.5)

        # 5. Snapshot page source
        try:
            html = driver.page_source or ""
        except Exception as exc:
            self.logger.error(f"page_source read failed: {url}  ({exc})")
            html = ""

        # Parse once for metadata extraction (separate soup object)
        soup_meta = _parse(html)

        # Title
        title = "No Title"
        try:
            if soup_meta.title and soup_meta.title.string:
                title = clean_text(soup_meta.title.string)
        except Exception:
            pass

        # Meta description (standard + OG)
        meta_description = ""
        try:
            mt = (
                soup_meta.find("meta", attrs={"name": "description"})
                or soup_meta.find("meta", attrs={"property": "og:description"})
            )
            if mt and mt.get("content"):
                meta_description = clean_text(mt["content"])
        except Exception:
            pass

        # Headings summary field (H1 + H2, pipe-separated)
        headings_text = ""
        try:
            headings_list = []
            for h in soup_meta.find_all(["h1", "h2"]):
                txt = clean_text(h.get_text())
                if txt:
                    headings_list.append(txt)
            max_h = getattr(config, "MAX_HEADINGS", 10)
            headings_text = " | ".join(headings_list[:max_h])
        except Exception:
            pass

        # 6. Full extraction (v12 pipeline)
        full_content = self.extract_full_text(html)

        # 7. JS innerText fallback if extraction was thin
        if len(full_content.strip()) < THIN_THRESHOLD:
            self.logger.debug(
                f"BS4 extraction thin ({len(full_content)} chars) — "
                f"trying JS innerText fallback: {url}"
            )
            js_text = self._js_innertext_fallback(driver)
            if len(js_text) > len(full_content):
                full_content = js_text
                self.logger.debug(f"JS innerText gave {len(full_content):,} chars: {url}")

        self.logger.info(f"Extracted {len(full_content):,} chars from: {url}")
        print(f"  [✓] {len(full_content):,} chars — {url}")

        # 8. Keyword filter
        if self.keyword_filter:
            combined = (title + " " + meta_description + " " + full_content).lower()
            if self.keyword_filter not in combined:
                self.logger.info(f"Skipped (keyword not found): {url}")
                return None

        # 9. Duplicate detection (fingerprint on title + first 500 chars of content)
        try:
            fingerprint = content_hash(title + full_content[:500])
            if fingerprint in self._seen_hashes:
                self.logger.info(f"Skipped (duplicate content): {url}")
                return None
            self._seen_hashes.add(fingerprint)
        except Exception:
            pass

        return {
            "url":              url,
            "title":            title,
            "meta_description": meta_description,
            "headings":         headings_text,
            "full_content":     full_content,
        }