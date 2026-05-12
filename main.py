"""
main.py
--------
Entry point — BFS crawler + full-content scraper.

v9 — FULL CONTENT EXTRACTOR
  - ALL AI summary / content_summary / HuggingFace code REMOVED
  - Per-page TXT files saved to output/<slug>.txt
  - combined_output.txt with all URLs + full content
  - No truncation of content anywhere
  - safe_get() with driver recovery
  - readyState wait before every extraction

v10 — CHALLENGE PAGE RESILIENCE
  - safe_get() uses page_load_strategy=eager; waits for readyState 'interactive'
    OR 'complete' (whichever arrives first within 20 s) — challenge pages
    intentionally stall 'complete' so waiting only for 'complete' hangs
  - After safe_get() succeeds, the Scraper's _wait_for_real_content() handles
    the remaining challenge wait before any extraction happens
  - Progress callback updated to include current page URL for the live UI
"""

import os
import re
import json
import time
import argparse
from urllib.parse import urlparse

from selenium.common.exceptions import (
    InvalidSessionIdException,
    WebDriverException,
    TimeoutException,
)

import config
from driver import create_driver
from crawler import Crawler
from scraper import Scraper
from utils import setup_logger, get_timestamp


# ── Slug helper ───────────────────────────────────────────────────────────────

def _url_to_slug(url: str) -> str:
    """
    Convert a URL to a safe filename slug.
    https://example.com/about  ->  about
    https://example.com/       ->  home
    """
    parsed = urlparse(url)
    path   = parsed.path.strip("/")
    if not path:
        return "home"
    slug = re.sub(r"[^a-zA-Z0-9_\-]", "_", path)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug[:80] or "page"


# ── Per-page TXT saver ────────────────────────────────────────────────────────

def save_page_txt(data: dict, output_dir: str) -> str:
    """
    Save one page's full content to output/<slug>.txt.
    Returns the file path written.
    """
    os.makedirs(output_dir, exist_ok=True)
    slug     = _url_to_slug(data.get("url", "page"))
    filepath = os.path.join(output_dir, f"{slug}.txt")

    counter = 1
    base    = filepath
    while os.path.exists(filepath):
        name     = os.path.splitext(base)[0]
        filepath = f"{name}_{counter}.txt"
        counter += 1

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"URL:   {data.get('url', '')}\n")
        f.write(f"TITLE: {data.get('title', '')}\n")
        meta = data.get("meta_description", "")
        if meta:
            f.write(f"META:  {meta}\n")
        f.write("\n")
        f.write("FULL CONTENT:\n")
        f.write("=" * 60 + "\n")
        content = data.get("full_content", "").strip()
        f.write(content if content else "[No readable content extracted]")
        f.write("\n")

    return filepath


# ── Combined TXT saver ────────────────────────────────────────────────────────

def save_combined_txt(results: list, output_dir: str) -> str:
    """Save all pages into a single combined_output.txt."""
    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, "combined_output.txt")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("COMBINED WEB SCRAPE OUTPUT\n")
        f.write(f"Total pages: {len(results)}\n")
        f.write("=" * 60 + "\n\n")

        for i, data in enumerate(results, 1):
            f.write(f"\n{'=' * 60}\n")
            f.write(f"PAGE {i}\n")
            f.write(f"{'=' * 60}\n\n")
            f.write(f"URL:   {data.get('url', '')}\n")
            f.write(f"TITLE: {data.get('title', '')}\n")
            meta = data.get("meta_description", "")
            if meta:
                f.write(f"META:  {meta}\n")
            f.write("\nFULL CONTENT:\n")
            f.write("-" * 40 + "\n")
            content = data.get("full_content", "").strip()
            f.write(content if content else "[No readable content extracted]")
            f.write("\n\n")

    return filepath


# ── JSON export ───────────────────────────────────────────────────────────────

def save_json(data: list, filepath: str):
    """Save results to pretty-printed JSON."""
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── Driver recovery wrapper ───────────────────────────────────────────────────

def safe_get(driver, url: str, logger, headless: bool):
    """
    Fault-tolerant page navigation.

    v10 changes:
    - Waits for readyState 'interactive' OR 'complete' (whichever comes first,
      up to 20 s).  Challenge pages hold readyState at 'loading' then jump to
      'interactive' when the JS challenge initialises — waiting only for
      'complete' would hang indefinitely.
    - After this returns success=True, scraper.Scraper._wait_for_real_content()
      handles the remaining challenge polling before any extraction.

    Returns (driver, success: bool).
    """
    def _wait_interactive(drv):
        """Wait until readyState is at least 'interactive' (max 20 s)."""
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                state = drv.execute_script("return document.readyState")
                if state in ("interactive", "complete"):
                    return
            except Exception:
                pass
            time.sleep(0.5)
        # Timed out — proceed anyway; scraper will handle it
        time.sleep(1)

    try:
        driver.get(url)
        _wait_interactive(driver)
        return driver, True

    except TimeoutException:
        logger.warning(f"Page load timed out — skipping: {url}")
        return driver, False

    except (InvalidSessionIdException, WebDriverException) as exc:
        logger.error(f"Driver session lost ({type(exc).__name__}): {exc}")
        try:
            driver.quit()
        except Exception:
            pass
        try:
            driver = create_driver(headless=headless)
            logger.info("Driver restarted — Session recovered")
        except Exception as spawn_exc:
            logger.error(f"Failed to restart driver: {spawn_exc}")
            return driver, False
        try:
            driver.get(url)
            _wait_interactive(driver)
            logger.info(f"Retry after recovery succeeded: {url}")
            return driver, True
        except Exception as retry_exc:
            logger.error(f"Retry also failed — skipping: {url}  ({retry_exc})")
            return driver, False


# ── Core run loop ─────────────────────────────────────────────────────────────

def run(base_url: str,
        max_pages: int = config.MAX_PAGES,
        keyword: str = None,
        headless: bool = config.HEADLESS_MODE,
        output_format: str = "txt",
        save_files: bool = True,
        progress_callback=None) -> dict:
    """
    Main orchestration function.

    Args:
        base_url:          Seed URL.
        max_pages:         Max pages to crawl.
        keyword:           Optional filter keyword.
        headless:          Run Chrome headless.
        output_format:     "txt", "json", or "both" (txt always written).
        save_files:        Write output files to disk.
        progress_callback: Optional callable(pages_done, pages_total, url).

    Returns:
        {results, pages_crawled, pages_saved, pages_skipped,
         txt_paths, combined_path, json_path, base_url}
    """
    logger = setup_logger()
    logger.info(f"Starting crawl -> {base_url}  |  max_pages={max_pages}")

    if not base_url.startswith(("http://", "https://")):
        base_url = "https://" + base_url

    driver  = create_driver(headless=headless)
    crawler = Crawler(base_url, max_pages, logger)
    scraper = Scraper(logger, keyword_filter=keyword)

    output_dir = getattr(config, "OUTPUT_DIR", "output")
    results    = []
    txt_paths  = []

    try:
        while crawler.has_next():
            url = crawler.next_url()
            if not url:
                break

            pages_done = len(crawler.visited) + 1
            logger.info(f"Visiting [{pages_done}/{max_pages}]: {url}")

            if progress_callback:
                try:
                    progress_callback(pages_done, max_pages, url)
                except Exception:
                    pass

            try:
                driver, success = safe_get(driver, url, logger, headless)
                if not success:
                    logger.warning(f"Skipping URL after failed load: {url}")
                    crawler.mark_visited(url)
                    continue

                crawler.mark_visited(url)

                # scraper.extract() now internally handles challenge detection
                # and waits via _wait_for_real_content() before extracting
                data = scraper.extract(driver, url)

                if data is None:
                    # Keyword filtered or duplicate — build minimal record
                    try:
                        page_title = driver.title or ""
                    except Exception:
                        page_title = ""
                    data = {
                        "url":              url,
                        "title":            page_title,
                        "meta_description": "",
                        "headings":         "",
                        "full_content":     "",
                    }

                results.append(data)

                if save_files and data.get("full_content"):
                    try:
                        txt_path = save_page_txt(data, output_dir)
                        txt_paths.append(txt_path)
                        logger.info(f"Saved TXT -> {txt_path}")
                        print(f"  [OK] Saved: {txt_path}  ({len(data['full_content']):,} chars)")
                    except Exception as te:
                        logger.error(f"TXT save failed for {url}: {te}")

                new_links = crawler.extract_links(driver, url)
                crawler.add_links(new_links)

                logger.info(
                    f"Crawled {len(crawler.visited)}/{max_pages} | "
                    f"Queue: {len(crawler.queue)} | Saved: {len(results)}"
                )

                crawler.polite_delay()

            except TimeoutException:
                logger.warning(f"Timeout -- skipping: {url}")
                crawler.mark_visited(url)
                continue

            except Exception as e:
                logger.error(f"Unexpected error on {url}: {e}")
                if not crawler.should_retry(url):
                    crawler.mark_visited(url)

    finally:
        try:
            driver.quit()
        except Exception as qe:
            logger.debug(f"Driver quit raised (safe): {qe}")

    # ── Save combined output ──────────────────────────────────────────────────
    combined_path = None
    json_path     = None

    if save_files:
        os.makedirs(output_dir, exist_ok=True)

        try:
            combined_path = save_combined_txt(results, output_dir)
            logger.info(f"Saved combined TXT -> {combined_path}")
            print(f"[OK] Combined TXT -> {combined_path}")
        except Exception as ce:
            logger.error(f"Combined TXT save failed: {ce}")

        if output_format.lower() in ("json", "both"):
            domain    = urlparse(base_url).netloc.replace("www.", "").replace(".", "_")
            timestamp = get_timestamp()
            json_path = os.path.join(output_dir, f"{domain}_{timestamp}.json")
            try:
                save_json(results, json_path)
                logger.info(f"Saved JSON -> {json_path}")
                print(f"[OK] JSON -> {json_path}")
            except Exception as je:
                logger.error(f"JSON save failed: {je}")

    logger.info(
        f"Done! Pages extracted: {len(results)} | "
        f"Skipped permanently: {len(crawler.skipped)}"
    )

    return {
        "results":       results,
        "pages_crawled": len(crawler.visited),
        "pages_saved":   len(results),
        "pages_skipped": len(crawler.skipped),
        "txt_paths":     txt_paths,
        "combined_path": combined_path,
        "json_path":     json_path,
        "base_url":      base_url,
    }


# ── CLI entry point ───────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  Dynamic Web Crawler & Scraper -- v10")
    print("  Full content extraction | Challenge-page detection")
    print("  Per-page TXT | Combined TXT")
    print("=" * 60)

    parser = argparse.ArgumentParser(description="Full-content web scraper")
    parser.add_argument("--url",         type=str, default=None)
    parser.add_argument("--pages",       type=int, default=config.MAX_PAGES)
    parser.add_argument("--keyword",     type=str, default=None)
    parser.add_argument("--format",      type=str, default="txt",
                        choices=["txt", "json", "both"])
    parser.add_argument("--no-headless", action="store_true", default=False)

    args  = parser.parse_args()
    url   = args.url or input("Enter website URL: ").strip()
    pages = max(1, min(args.pages, 50))

    run(
        base_url=url,
        max_pages=pages,
        keyword=args.keyword,
        headless=not args.no_headless,
        output_format=args.format,
        save_files=True,
    )


if __name__ == "__main__":
    main()