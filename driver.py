"""
driver.py
----------
Sets up a Selenium Chrome browser.

UPGRADES (v2):
  - Reads HEADLESS_MODE, TIMEOUT, PAGE_LOAD_TIMEOUT from config.py
  - Added extra stealth arguments to reduce bot-detection

UPGRADES (v3 — fault-tolerance):
  - Added --disable-extensions, crash-dump / error-reporting suppression flags
  - CDP stealth call wrapped in try-except so a CDP failure never kills startup
  - create_driver() raises a clear RuntimeError on total failure

UPGRADES (v10 — challenge-page resilience):
  - IMAGE/FONT BLOCKING REMOVED — breaks JS challenge pages silently
  - Expanded CDP stealth script: navigator.webdriver, plugins, languages,
    permissions query, chrome runtime, userAgent
  - page_load_strategy = "eager" — don't block on slow resources
  - --disable-features=IsolateOrigins for cross-origin JS compatibility
  - Bumped user-agent to Chrome 124
"""

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

import config

_STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
);
window.chrome = { runtime: { onConnect: null, onMessage: null } };
Object.defineProperty(navigator, 'userAgent', {
    get: () => 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
});
"""


def create_driver(headless: bool = None) -> webdriver.Chrome:
    """
    Creates and returns a Selenium Chrome WebDriver.

    Args:
        headless: Override config.HEADLESS_MODE for this session.

    Returns:
        A configured webdriver.Chrome instance.
    """
    use_headless = headless if headless is not None else config.HEADLESS_MODE

    options = Options()
    options.page_load_strategy = "eager"

    if use_headless:
        options.add_argument("--headless=new")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-crash-reporter")
    options.add_argument("--disable-in-process-stack-traces")
    options.add_argument("--disable-logging")
    options.add_argument("--disable-dev-tools")
    options.add_argument("--log-level=3")
    options.add_argument("--output=/dev/null")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--allow-running-insecure-content")

    # Required for JS challenge pages — IsolateOrigins blocks cross-origin JS
    options.add_argument("--disable-features=IsolateOrigins,site-per-process")
    options.add_argument("--disable-web-security")

    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    # NOTE: Image/font blocking intentionally REMOVED (v10).
    # It silently breaks JS-based challenge pages (Turnstile, hCaptcha, etc.)

    service = Service(ChromeDriverManager().install())

    try:
        driver = webdriver.Chrome(service=service, options=options)
    except Exception as exc:
        raise RuntimeError(f"[driver] Chrome failed to start: {exc}") from exc

    driver.set_page_load_timeout(config.PAGE_LOAD_TIMEOUT)

    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": _STEALTH_SCRIPT},
        )
    except Exception:
        pass

    return driver