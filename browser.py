"""Headless prohlížeč (Playwright) pro weby, které renderují obsah přes JS.

Cyklobazar.cz běží za Cloudflare a na prosté ``requests`` vrací HTTP 403 -
stránka potřebuje skutečný prohlížeč, který spustí JS a vyřeší výzvu.
Bazoš takhle tahat nemusíme, tam stačí rychlý ``requests``.

Playwright se importuje až při prvním použití, takže když chybí,
degraduje se jen tento jeden web a zbytek monitoru běží dál.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Optional

log = logging.getLogger(__name__)

# Titulky/texty, které signalizují Cloudflare interstitial (CZ i EN varianta).
CHALLENGE_TITLES = ("okamzik", "okamžik", "just a moment", "moment...", "attention required")

# Interaktivní Turnstile ("Potvrďte, že jste člověk") se sám nevyřeší - čeká
# na kliknutí živého člověka. Nemá smysl na něj čekat ani ho obcházet.
INTERACTIVE_CHALLENGE_SELECTORS = (
    "#cf-chl-widget",
    "[id^='cf-chl-widget']",
    "iframe[src*='challenges.cloudflare.com']",
    "input[type='checkbox'][name='cf-turnstile-response']",
)

# Cookie lišty, které překrývají obsah.
CONSENT_SELECTORS = (
    "button.fc-cta-consent",
    ".fc-button.fc-cta-consent",
    "button#didomi-notice-agree-button",
    "button[aria-label='Souhlasím']",
)

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)


class BrowserUnavailable(RuntimeError):
    """Playwright není nainstalovaný nebo se nepodařilo spustit prohlížeč."""


class PlaywrightFetcher:
    """Líně spuštěný headless Chromium se sdíleným kontextem.

    Prohlížeč se spustí při prvním ``get()`` a drží se otevřený pro všechny
    další dotazy - jednak je to rychlejší, jednak si kontext udrží cookie
    od Cloudflare a výzva se tak řeší jen jednou.
    """

    name = "playwright"

    def __init__(
        self,
        headless: bool = True,
        page_timeout: float = 45.0,
        challenge_timeout: float = 40.0,
        min_delay: float = 1.5,
        max_delay: float = 3.5,
        locale: str = "cs-CZ",
        timezone_id: str = "Europe/Prague",
    ) -> None:
        self.headless = headless
        self.page_timeout = page_timeout
        self.challenge_timeout = challenge_timeout
        self.min_delay = min_delay
        self.max_delay = max(min_delay, max_delay)
        self.locale = locale
        self.timezone_id = timezone_id

        self._stealth_cm = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._failed = False
        self._blocked = False
        self._first_request = True

    @property
    def blocked(self) -> bool:
        """True, když web vyžaduje ruční potvrzení a dál nás nepustí."""
        return self._blocked

    # --- životní cyklus --------------------------------------------------

    def start(self) -> None:
        if self._page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserUnavailable(
                "Chybí balík playwright. Nainstaluj: pip install playwright "
                "&& python -m playwright install --with-deps chromium"
            ) from exc

        stealth = self._load_stealth()
        try:
            if stealth is not None:
                # playwright-stealth 2.x: obalí celý sync_playwright kontext
                self._stealth_cm = stealth.use_sync(sync_playwright())
                self._playwright = self._stealth_cm.__enter__()
            else:
                self._stealth_cm = sync_playwright()
                self._playwright = self._stealth_cm.__enter__()

            self._browser = self._playwright.chromium.launch(
                headless=self.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            self._context = self._browser.new_context(
                user_agent=DEFAULT_UA,
                locale=self.locale,
                timezone_id=self.timezone_id,
                viewport={"width": 1366, "height": 900},
                extra_http_headers={"Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8"},
            )
            self._context.set_default_timeout(self.page_timeout * 1000)
            self._page = self._context.new_page()
            log.info("Headless Chromium spuštěn.")
        except Exception as exc:
            self.close()
            raise BrowserUnavailable("Nepodařilo se spustit prohlížeč: {}".format(exc)) from exc

    @staticmethod
    def _load_stealth():
        """playwright-stealth 2.x má třídu Stealth, 1.x jen stealth_sync()."""
        try:
            from playwright_stealth import Stealth
        except ImportError:
            log.info("playwright-stealth není k dispozici, jedu bez něj.")
            return None
        return Stealth()

    def close(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    closer.close()
                except Exception:
                    pass
        if self._stealth_cm is not None:
            try:
                self._stealth_cm.__exit__(None, None, None)
            except Exception:
                pass
        self._stealth_cm = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def __enter__(self) -> "PlaywrightFetcher":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # --- stahování -------------------------------------------------------

    def _sleep(self) -> None:
        if self._first_request:
            self._first_request = False
            return
        time.sleep(random.uniform(self.min_delay, self.max_delay))

    def get(self, url: str, wait_for: Optional[str] = None) -> Optional[str]:
        """Načte stránku a vrátí HTML až po vyřešení JS výzvy."""
        if self._failed or self._blocked:
            return None
        try:
            self.start()
        except BrowserUnavailable as exc:
            log.error("%s", exc)
            self._failed = True
            return None

        self._sleep()
        try:
            response = self._page.goto(
                url, wait_until="domcontentloaded", timeout=self.page_timeout * 1000
            )
        except Exception as exc:
            log.warning("Načtení %s selhalo: %s", url, exc)
            return None

        status = response.status if response is not None else None
        self._dismiss_consent()

        if not self._wait_for_challenge(url):
            return None
        if wait_for and not self._wait_for_content(wait_for):
            log.warning("Obsah (%s) se na %s neobjevil", wait_for, url)

        try:
            html = self._page.content()
        except Exception as exc:
            log.warning("Nelze přečíst obsah %s: %s", url, exc)
            return None

        log.debug("Staženo %s (HTTP %s, %s znaků)", url, status, len(html))
        return html

    def _wait_for_challenge(self, url: str) -> bool:
        """Počká, než Cloudflare pustí dál. Vrací False při timeoutu."""
        deadline = time.time() + self.challenge_timeout
        warned = False
        while time.time() < deadline:
            try:
                title = (self._page.title() or "").lower()
            except Exception:
                return False
            if not any(marker in title for marker in CHALLENGE_TITLES):
                return True
            if self._is_interactive_challenge():
                log.warning(
                    "Web vyžaduje ruční potvrzení 'nejsem robot' (Cloudflare "
                    "Turnstile). Automaticky to řešit nebudeme - web se pro "
                    "tento běh přeskakuje: %s", url,
                )
                self._blocked = True
                return False
            if not warned:
                log.info("Čekám na vyřešení JS výzvy: %s", url)
                warned = True
            self._page.wait_for_timeout(1500)
        log.warning("JS výzva se nevyřešila do %ss: %s", self.challenge_timeout, url)
        return False

    def _is_interactive_challenge(self) -> bool:
        for selector in INTERACTIVE_CHALLENGE_SELECTORS:
            try:
                if self._page.query_selector(selector) is not None:
                    return True
            except Exception:
                continue
        return False

    def _wait_for_content(self, selector: str) -> bool:
        try:
            self._page.wait_for_selector(selector, timeout=15000, state="attached")
            return True
        except Exception:
            return False

    def _dismiss_consent(self) -> None:
        """Odklikne cookie lištu, která by překrývala obsah."""
        for selector in CONSENT_SELECTORS:
            try:
                element = self._page.query_selector(selector)
                if element is not None and element.is_visible():
                    element.click(timeout=3000)
                    self._page.wait_for_timeout(300)
                    return
            except Exception:
                continue
        try:
            self._page.evaluate(
                "document.querySelectorAll('.fc-consent-root').forEach(e => e.remove())"
            )
        except Exception:
            pass
