"""Scrapery pro inzertní servery Bazos.cz a Cyklobazar.cz.

Parsování stojí na seznamech kandidátních CSS selektorů s obecným
fallbackem přes odkazy na detail inzerátu. Když se web mírně změní
(přejmenovaná class, jiná struktura obalů), scraper se opře o fallback
místo toho, aby vrátil nula výsledků.

Bazoš se tahá přes rychlý ``requests``, Cyklobazar přes headless
Chromium (Playwright), protože běží za Cloudflare a potřebuje JS.
"""

from __future__ import annotations

import hashlib
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from categories import Category, CategoryMatcher

log = logging.getLogger(__name__)

USER_AGENTS: Tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0",
)

# Texty, které znamenají "cena není uvedena".
NO_PRICE_MARKERS: Tuple[str, ...] = (
    "dohodou",
    "v textu",
    "v inzeratu",
    "v inzerátu",
    "nabidnete",
    "nabídněte",
    "neuvedena",
    "neuvedeno",
    "poptat",
    "na dotaz",
)

FREE_MARKERS: Tuple[str, ...] = ("zdarma", "darem", "gratis")

# Mezery včetně nedělitelných variant, které Bazos/Cyklobazar používají v cenách.
_SPACES = "\u00a0\u202f\u2009\u2007\u200b"

_PRICE_RE = re.compile(r"\d[\d" + _SPACES + r" .,]*")

CHALLENGE_MARKERS: Tuple[str, ...] = (
    "just a moment",
    "cf-browser-verification",
    "cf_chl_opt",
    "enable javascript and cookies to continue",
    "attention required! | cloudflare",
)


def normalize_space(text: Optional[str]) -> str:
    """Sjednotí všechny druhy mezer a zkolabuje bílé znaky."""
    if not text:
        return ""
    for ch in _SPACES:
        text = text.replace(ch, " ")
    return " ".join(text.split())


def parse_price(text: Optional[str]) -> Optional[int]:
    """Zparsuje českou cenu na celé Kč.

    Vrací ``None``, pokud cena není uvedena ("Dohodou", "V textu", ...).
    Zvládá "12 000 Kč", "12.000,- Kč", "1 234,50 Kč", "Zdarma".
    """
    plain = normalize_space(text)
    if not plain:
        return None

    low = plain.lower()
    if any(marker in low for marker in FREE_MARKERS):
        return 0

    match = _PRICE_RE.search(plain)
    if not match:
        return None

    raw = normalize_space(match.group(0)).strip(" .,")
    # Odstraní desetinnou část ("1 234,50" -> "1 234", ale "12.000" zůstane).
    raw = re.sub(r"[.,]\d{1,2}$", "", raw)
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None

    value = int(digits)
    if value <= 0 or value > 10_000_000:
        # Nesmyslná hodnota = raději "cena neznámá" než falešný zásah.
        return None
    if any(marker in low for marker in NO_PRICE_MARKERS):
        return None
    return value


def _is_bare_number(text: str) -> bool:
    """True pro text, který je sám o sobě jen číslo (cena bez jednotky)."""
    plain = normalize_space(text)
    return bool(plain) and len(plain) <= 15 and re.fullmatch(r"[\d .,]+", plain) is not None


def looks_like_bot_challenge(html: str) -> bool:
    """Detekuje antibot interstitial (Cloudflare "Just a moment...")."""
    head = html[:4000].lower()
    return any(marker in head for marker in CHALLENGE_MARKERS)


@dataclass(frozen=True)
class Listing:
    """Jeden nalezený inzerát."""

    uid: str
    site: str
    title: str
    url: str
    price: Optional[int]
    query: str = ""
    category_key: str = ""
    category_label: str = ""
    category_max_price: Optional[int] = None

    @property
    def price_label(self) -> str:
        if self.price is None:
            return "Dohodou / v textu"
        if self.price == 0:
            return "Zdarma"
        return "{:,} Kč".format(self.price).replace(",", " ")

    def with_category(self, category: Category) -> "Listing":
        return Listing(
            uid=self.uid,
            site=self.site,
            title=self.title,
            url=self.url,
            price=self.price,
            query=self.query,
            category_key=category.key,
            category_label=category.label,
            category_max_price=category.max_price,
        )


class RequestsFetcher:
    """requests session s browser hlavičkami, prodlevami a retry."""

    name = "requests"

    def __init__(
        self,
        timeout: float = 20.0,
        min_delay: float = 1.0,
        max_delay: float = 3.0,
        max_retries: int = 2,
    ) -> None:
        self.timeout = timeout
        self.min_delay = min_delay
        self.max_delay = max(min_delay, max_delay)
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            }
        )
        self._first_request = True

    def _sleep(self) -> None:
        if self._first_request:
            self._first_request = False
            return
        time.sleep(random.uniform(self.min_delay, self.max_delay))

    def get(self, url: str, wait_for: Optional[str] = None) -> Optional[str]:
        """Vrátí HTML, nebo ``None`` když se stránku nepodaří stáhnout."""
        for attempt in range(self.max_retries + 1):
            self._sleep()
            try:
                response = self.session.get(
                    url,
                    timeout=self.timeout,
                    headers={"User-Agent": random.choice(USER_AGENTS)},
                )
            except requests.RequestException as exc:
                log.warning("Požadavek na %s selhal (%s/%s): %s",
                            url, attempt + 1, self.max_retries + 1, exc)
                continue

            if response.status_code == 200:
                response.encoding = response.apparent_encoding or response.encoding
                return response.text

            if response.status_code in (403, 503):
                log.warning("%s vrátil HTTP %s (antibot ochrana?)",
                            url, response.status_code)
                return None

            log.warning("%s vrátil HTTP %s", url, response.status_code)
        return None

    def close(self) -> None:
        self.session.close()


# Zpětně kompatibilní alias - dřívější název třídy.
HttpClient = RequestsFetcher


class BaseScraper:
    """Obecná logika pro extrakci inzerátů ze stránky s výsledky.

    Potomek definuje jen URL vyhledávání a sadu kandidátních selektorů.
    """

    name: str = "base"
    base_url: str = ""
    item_selectors: Sequence[str] = ()
    link_selectors: Sequence[str] = ()
    title_selectors: Sequence[str] = ()
    price_selectors: Sequence[str] = ()
    detail_url_re: re.Pattern = re.compile(r"/inzerat/")
    id_re: Optional[re.Pattern] = None
    # Selektor, na který se čeká v headless prohlížeči (None = nečekat).
    content_selector: Optional[str] = None

    def __init__(self, fetcher) -> None:
        self.fetcher = fetcher

    # --- k přepsání v potomkovi ------------------------------------------

    def search_url(self, query: str, page: int) -> str:
        raise NotImplementedError

    # --- veřejné API -----------------------------------------------------

    def search(self, query: str, max_pages: int = 1) -> List[Listing]:
        results: Dict[str, Listing] = {}
        for page in range(max_pages):
            url = self.search_url(query, page)
            html = self.fetcher.get(url, wait_for=self.content_selector)
            if not html:
                log.warning("[%s] stránku %s se nepodařilo stáhnout: %s",
                            self.name, page + 1, url)
                break
            if looks_like_bot_challenge(html):
                log.warning("[%s] server vrátil antibot stránku, přeskakuji: %s",
                            self.name, url)
                break

            page_listings = self.parse(html, query)
            if not page_listings:
                log.debug("[%s] dotaz %r, stránka %s: žádné inzeráty",
                          self.name, query, page + 1)
                break
            for listing in page_listings:
                results.setdefault(listing.uid, listing)
        return list(results.values())

    def parse(self, html: str, query: str = "") -> List[Listing]:
        soup = BeautifulSoup(html, "html.parser")
        listings = self._parse_structured(soup, query)
        if not listings:
            log.info("[%s] známé selektory nic nenašly, zkouším fallback",
                     self.name)
            listings = self._parse_fallback(soup, query)
        return listings

    # --- interní ---------------------------------------------------------

    def _parse_structured(self, soup: BeautifulSoup, query: str) -> List[Listing]:
        listings: Dict[str, Listing] = {}
        for selector in self.item_selectors:
            for block in soup.select(selector):
                listing = self._listing_from_block(block, query)
                if listing:
                    listings.setdefault(listing.uid, listing)
            if listings:
                break
        return list(listings.values())

    def _parse_fallback(self, soup: BeautifulSoup, query: str) -> List[Listing]:
        """Projde všechny odkazy na detail inzerátu a cenu hledá v okolí."""
        listings: Dict[str, Listing] = {}
        for anchor in soup.find_all("a", href=True):
            if not self.detail_url_re.search(anchor["href"]):
                continue
            title = self._title_from(anchor) or normalize_space(anchor.get("title"))
            if not title:
                continue
            url = urljoin(self.base_url, anchor["href"])
            price = self._find_price(anchor)
            if price is None:
                price = self._price_from_ancestors(anchor)
            uid = self._make_uid(url)
            listings.setdefault(
                uid,
                Listing(uid=uid, site=self.name, title=title, url=url,
                        price=price, query=query),
            )
        return list(listings.values())

    def _listing_from_block(self, block, query: str) -> Optional[Listing]:
        anchor = self._find_anchor(block)
        if anchor is None:
            return None
        url = urljoin(self.base_url, anchor["href"])
        title = self._find_title(block, anchor)
        if not title:
            return None
        price = self._find_price(block)
        uid = self._make_uid(url)
        return Listing(uid=uid, site=self.name, title=title, url=url,
                       price=price, query=query)

    def _find_anchor(self, block):
        """Odkaz na detail. Blok sám může být ``<a>`` (Cyklobazar)."""
        candidates = []
        if block.name == "a" and block.get("href"):
            candidates.append(block)
        for selector in self.link_selectors:
            candidates.extend(
                a for a in block.select(selector) if a.get("href")
            )
        candidates.extend(block.find_all("a", href=True))

        for anchor in candidates:
            if self.detail_url_re.search(anchor["href"]):
                return anchor
        # Web mohl změnit tvar URL detailu - vezmi první interní odkaz.
        # Externí odkazy přeskakujeme, jsou to inzertní bannery.
        for anchor in candidates:
            if self._is_internal(anchor["href"]):
                return anchor
        return None

    def _is_internal(self, href: str) -> bool:
        href = (href or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            return False
        host = urlparse(urljoin(self.base_url, href)).netloc
        return self._domain(host) == self._domain(urlparse(self.base_url).netloc)

    @staticmethod
    def _domain(netloc: str) -> str:
        """Registrovaná domena - Bazoš má inzeráty na subdoménách (sport.bazos.cz)."""
        host = netloc.lower().split(":")[0]
        return ".".join(host.split(".")[-2:])

    def _find_title(self, block, anchor) -> str:
        for selector in self.title_selectors:
            element = block.select_one(selector)
            if element is not None:
                title = normalize_space(element.get_text(" "))
                if title:
                    return title
        return self._title_from(anchor)

    @staticmethod
    def _title_from(anchor) -> str:
        """Titulek z odkazu: text, jinak title/aria-label, jinak alt obrázku."""
        title = normalize_space(anchor.get_text(" "))
        if title:
            return title
        for attr in ("title", "aria-label"):
            value = normalize_space(anchor.get(attr))
            if value:
                return value
        image = anchor.find("img")
        if image is not None:
            return normalize_space(image.get("alt"))
        return ""

    def _find_price(self, block) -> Optional[int]:
        for selector in self.price_selectors:
            for element in block.select(selector):
                text = normalize_space(element.get_text(" "))
                if not text:
                    continue
                low = text.lower()
                if any(marker in low for marker in NO_PRICE_MARKERS):
                    # Explicitně "Dohodou" / "V textu" - cena neexistuje.
                    return None
                price = self._price_from_text(text)
                if price is None and _is_bare_number(text):
                    price = parse_price(text)
                if price is not None:
                    return price
        # Poslední možnost: hledej "... Kč" kdekoli v bloku.
        return self._price_from_text(block.get_text(" "))

    @staticmethod
    def _price_from_text(text: str) -> Optional[int]:
        plain = normalize_space(text)
        match = re.search(
            r"(\d[\d .,]{0,12})\s*(?:Kč|kč|CZK|,-)", plain
        )
        if match:
            return parse_price(match.group(1))
        return None

    def _price_from_ancestors(self, node) -> Optional[int]:
        current = node
        for _ in range(4):
            current = current.parent
            if current is None:
                break
            price = self._find_price(current)
            if price is not None:
                return price
        return None

    def _make_uid(self, url: str) -> str:
        """Stabilní ID inzerátu: ideálně z ID v URL, jinak hash URL."""
        if self.id_re:
            match = self.id_re.search(url)
            if match:
                return "{}:{}".format(self.name, match.group(1))
        clean = url.split("?")[0].split("#")[0].rstrip("/")
        digest = hashlib.sha1(clean.encode("utf-8")).hexdigest()[:16]
        return "{}:{}".format(self.name, digest)


class BazosScraper(BaseScraper):
    name = "bazos"
    base_url = "https://www.bazos.cz/"
    item_selectors = ("div.inzeraty", "div.inzeratyflex", "div[class*='inzerat']")
    link_selectors = ("h2.nadpis a", "h2 a", ".inzeratynadpis a")
    title_selectors = ("h2.nadpis a", "h2 a")
    price_selectors = ("div.inzeratycena", ".inzeratycena", "[class*='cena']")
    detail_url_re = re.compile(r"/inzerat/\d+")
    id_re = re.compile(r"/inzerat/(\d+)")

    def search_url(self, query: str, page: int) -> str:
        return (
            "https://www.bazos.cz/search.php"
            "?hledat={q}&rubriky=www&hlokalita=&humkreis=25"
            "&cenaod=&cenado=&order=&crz={offset}".format(
                q=quote_plus(query), offset=page * 20
            )
        )


class CyklobazarScraper(BaseScraper):
    """Cyklobazar.cz - vyžaduje headless prohlížeč (Cloudflare + JS)."""

    name = "cyklobazar"
    base_url = "https://www.cyklobazar.cz/"
    item_selectors = (
        ".cb-offer-list__item",
        "a.cb-offer",
        "[class*='cb-offer-list__item']",
        "[class*='cb-offer']",
    )
    link_selectors = ("a.cb-offer", "a[href*='/inzerat/']")
    title_selectors = (
        ".cb-offer__header h4",
        ".cb-offer__header h3",
        ".cb-offer__header h2",
        ".cb-offer__header",
        "h4",
        "h3",
    )
    price_selectors = (".cb-offer__price", "[class*='price']", "[class*='cena']")
    detail_url_re = re.compile(r"/inzerat/[A-Za-z0-9]{5,}")
    id_re = re.compile(r"/inzerat/([A-Za-z0-9]{5,})")
    content_selector = ".cb-offer-list, a.cb-offer"

    def search_url(self, query: str, page: int) -> str:
        url = "https://www.cyklobazar.cz/vsechny-kategorie?q={q}".format(
            q=quote_plus(query)
        )
        if page:
            url += "&vp-page={}".format(page + 1)
        return url


SCRAPER_CLASSES: Dict[str, type] = {
    BazosScraper.name: BazosScraper,
    CyklobazarScraper.name: CyklobazarScraper,
}

# Weby, které se neobejdou bez headless prohlížeče.
BROWSER_SITES = frozenset({CyklobazarScraper.name})


def build_scrapers(
    sites_cfg: Dict[str, Dict],
    requests_fetcher,
    browser_fetcher=None,
) -> List[BaseScraper]:
    """Poskládá scrapery a přiřadí jim vhodný způsob stahování."""
    scrapers: List[BaseScraper] = []
    for name, cls in SCRAPER_CLASSES.items():
        if not sites_cfg.get(name, {}).get("enabled", True):
            continue
        if name in BROWSER_SITES:
            if browser_fetcher is None:
                log.warning("[%s] vyžaduje headless prohlížeč, ale ten není "
                            "k dispozici - web se přeskočí", name)
                continue
            scrapers.append(cls(browser_fetcher))
        else:
            scrapers.append(cls(requests_fetcher))
    return scrapers


@dataclass(frozen=True)
class SearchTask:
    """Jeden dotaz k provedení na jednom webu."""

    query: str
    category: Optional[Category]
    max_pages: int


def build_tasks(
    site_cfg: Dict,
    categories: Sequence[Category],
    broad_queries: Sequence[str],
    default_pages: int,
) -> List[SearchTask]:
    """Sestaví plán dotazů podle režimu webu.

    ``all``   - projede všechna klíčová slova každé kategorie (hodí se pro
                rychlý Bazoš přes requests).
    ``broad`` - pár širokých dotazů, kategorie se pozná z titulku. Výrazně
                méně požadavků, vhodné pro weby tažené přes prohlížeč.
    """
    mode = str(site_cfg.get("query_mode", "all")).lower()
    max_pages = int(site_cfg.get("max_pages_per_query", default_pages))

    tasks: List[SearchTask] = []
    seen = set()

    def add(query: str, category: Optional[Category]) -> None:
        query = query.strip()
        key = (query.lower(), category.key if category else "")
        if query and key not in seen:
            seen.add(key)
            tasks.append(SearchTask(query, category, max_pages))

    if mode == "broad":
        queries = list(broad_queries)
        if queries and site_cfg.get("rotate_queries", True):
            # Cyklobazar nás po pár požadavcích pošle na ruční ověření, takže
            # projde zpravidla jen první dotaz. Rotací pořadí podle času se
            # napříč běhy postupně dostane na řadu každý z nich.
            offset = int(time.time() // 900) % len(queries)
            queries = queries[offset:] + queries[:offset]
        for query in queries:
            add(query, None)
    else:
        for category in categories:
            for query in category.queries:
                add(query, category)
    return tasks


def collect(
    plans: Iterable[Tuple[BaseScraper, Sequence[SearchTask]]],
    matcher: CategoryMatcher,
) -> List[Listing]:
    """Provede naplánované dotazy a vrátí zařazené inzeráty.

    Každý inzerát se zařadí podle *titulku*, ne podle dotazu, který ho
    našel - fulltext bazarů vrací i jiné díly, než na co se ptáme.
    """
    found: Dict[str, Listing] = {}

    for scraper, tasks in plans:
        for task in tasks:
            try:
                listings = scraper.search(task.query, max_pages=task.max_pages)
            except Exception as exc:  # jeden rozbitý web nesmí zabít běh
                log.error("[%s] dotaz %r selhal: %s",
                          scraper.name, task.query, exc)
                continue

            kept = 0
            for listing in listings:
                resolved = matcher.classify(listing.title, task.category)
                if resolved is None:
                    continue
                kept += 1
                enriched = listing.with_category(resolved)
                existing = found.get(listing.uid)
                if existing is None or (
                    existing.price is None and enriched.price is not None
                ):
                    found[listing.uid] = enriched

            label = task.category.key if task.category else "broad"
            log.info("[%s/%s] %r: %s nalezeno, %s relevantních",
                     scraper.name, label, task.query, len(listings), kept)

            if getattr(scraper.fetcher, "blocked", False):
                log.warning("[%s] web nás dál nepouští, zbytek dotazů "
                            "přeskakuji", scraper.name)
                break
    return list(found.values())
