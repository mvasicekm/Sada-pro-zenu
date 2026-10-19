#!/usr/bin/env python3
"""Monitor inzerátů Shimano 105 / GRX na Bazos.cz a Cyklobazar.cz.

Hledá komponenty po kategoriích (páky, přehazovačka, kazeta, ...), aby
šlo poskládat celou sadu do zadaného rozpočtu. Každý inzerát se zařadí
podle titulku k příslušné kategorii a porovná s jejím cenovým stropem.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from typing import Dict, List, Optional, Sequence

from dotenv import load_dotenv

import catalog
import categories as categories_module
import mailer
import scraper

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(BASE_DIR, "config.json")
DEFAULT_SEEN = os.path.join(BASE_DIR, "seen_ids.json")
DEFAULT_DATA = os.path.join(BASE_DIR, "docs", "data.json")

log = logging.getLogger("monitor")


def load_config(path: str) -> Dict:
    with open(path, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    if not config.get("categories"):
        raise ValueError("config.json neobsahuje žádné kategorie v 'categories'")
    return config


def load_seen(path: str) -> Dict[str, str]:
    """Načte mapu ``uid -> ISO datum prvního nálezu``."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("seen_ids.json nelze přečíst (%s), začínám s prázdnou pamětí", exc)
        return {}

    if isinstance(data, dict):
        ids = data.get("ids", data)
        if isinstance(ids, dict):
            return {str(k): str(v) for k, v in ids.items()}
        if isinstance(ids, list):
            return {str(uid): "" for uid in ids}
    if isinstance(data, list):
        return {str(uid): "" for uid in data}
    log.warning("Neznámý formát seen_ids.json, začínám s prázdnou pamětí")
    return {}


def save_seen(path: str, seen: Dict[str, str], max_entries: int = 5000) -> None:
    """Uloží paměť a ořeže ji na posledních ``max_entries`` záznamů."""
    items = sorted(seen.items(), key=lambda item: item[1], reverse=True)
    if max_entries and len(items) > max_entries:
        items = items[:max_entries]
    payload = {
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "ids": dict(sorted(items)),
    }
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp_path, path)


def select_new(
    listings: Sequence[scraper.Listing],
    seen: Dict[str, str],
    allow_missing_price: bool,
) -> List[scraper.Listing]:
    """Vyfiltruje inzeráty, které jsou nové a vejdou se do limitu kategorie."""
    fresh: List[scraper.Listing] = []
    for listing in listings:
        if listing.uid in seen:
            continue
        if listing.price is None:
            if not allow_missing_price:
                log.debug("Přeskakuji (cena neuvedena): %s", listing.url)
                continue
        elif (
            listing.category_max_price is not None
            and listing.price > listing.category_max_price
        ):
            continue
        fresh.append(listing)

    fresh.sort(key=lambda item: (item.category_key, item.price is None, item.price or 0))
    return fresh


def normalize_sites(raw: Dict) -> Dict[str, Dict]:
    """Povolí zápis ``"bazos": true`` i ``"bazos": {"enabled": true, ...}``."""
    sites: Dict[str, Dict] = {}
    for name in scraper.SCRAPER_CLASSES:
        value = raw.get(name, True)
        if isinstance(value, bool):
            sites[name] = {"enabled": value}
        elif isinstance(value, dict):
            sites[name] = dict(value)
            sites[name].setdefault("enabled", True)
        else:
            sites[name] = {"enabled": bool(value)}
    return sites


def build_browser_fetcher(config: Dict, sites: Dict[str, Dict]):
    """Spustí headless prohlížeč, jen když ho některý zapnutý web potřebuje."""
    needs_browser = any(
        sites.get(site, {}).get("enabled", True) for site in scraper.BROWSER_SITES
    )
    if not needs_browser:
        return None

    import browser as browser_module

    browser_cfg = config.get("browser", {})
    return browser_module.PlaywrightFetcher(
        headless=bool(browser_cfg.get("headless", True)),
        page_timeout=float(browser_cfg.get("page_timeout_seconds", 45)),
        challenge_timeout=float(browser_cfg.get("challenge_timeout_seconds", 40)),
        min_delay=float(browser_cfg.get("min_delay_seconds", 1.5)),
        max_delay=float(browser_cfg.get("max_delay_seconds", 3.5)),
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help="cesta ke config.json")
    parser.add_argument("--seen", default=DEFAULT_SEEN,
                        help="cesta k seen_ids.json")
    parser.add_argument("--data", default=DEFAULT_DATA,
                        help="cesta k docs/data.json pro webovou stránku")
    parser.add_argument("--dry-run", action="store_true",
                        help="nic neposílat a neukládat, jen vypsat nálezy")
    parser.add_argument("--no-mail", action="store_true",
                        help="neposílat e-mail, ale uložit seen_ids.json")
    parser.add_argument("--only", metavar="KLIC", action="append",
                        help="omezit běh na kategorii (lze opakovat)")
    parser.add_argument("--site", metavar="NAZEV", action="append",
                        help="omezit běh na web: bazos / cyklobazar")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="podrobné logování")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    load_dotenv()

    config = load_config(args.config)
    request_cfg = config.get("request", {})
    total_budget = config.get("total_budget")
    send_mail = not (args.dry_run or args.no_mail)

    all_categories = categories_module.load_categories(config)
    matcher = categories_module.CategoryMatcher(
        all_categories, config.get("matching", {})
    )

    active_categories = all_categories
    if args.only:
        wanted = {key.lower() for key in args.only}
        active_categories = [c for c in all_categories if c.key.lower() in wanted]
        if not active_categories:
            log.error("Žádná kategorie neodpovídá %s", args.only)
            return 2

    sites = normalize_sites(config.get("sites", {}))
    if args.site:
        wanted_sites = {name.lower() for name in args.site}
        for name, site_cfg in sites.items():
            site_cfg["enabled"] = name.lower() in wanted_sites

    # SMTP konfiguraci validuj dřív, než se něco stáhne.
    smtp_config = None
    if send_mail:
        try:
            smtp_config = mailer.SmtpConfig.from_env()
        except mailer.MailerConfigError as exc:
            log.error("SMTP konfigurace: %s", exc)
            return 2

    requests_fetcher = scraper.RequestsFetcher(
        timeout=float(request_cfg.get("timeout_seconds", 20)),
        min_delay=float(request_cfg.get("min_delay_seconds", 1.0)),
        max_delay=float(request_cfg.get("max_delay_seconds", 3.0)),
        max_retries=int(request_cfg.get("max_retries", 2)),
    )
    browser_fetcher = build_browser_fetcher(config, sites)

    try:
        scrapers = scraper.build_scrapers(sites, requests_fetcher, browser_fetcher)
        if not scrapers:
            log.error("Není zapnutý žádný web v 'sites'")
            return 2

        default_pages = int(request_cfg.get("max_pages_per_query", 1))
        broad_queries = config.get("broad_queries", [])
        plans = []
        for instance in scrapers:
            tasks = scraper.build_tasks(
                sites.get(instance.name, {}), active_categories,
                broad_queries, default_pages,
            )
            log.info("[%s] naplánováno %s dotazů (režim %s)",
                     instance.name, len(tasks),
                     sites.get(instance.name, {}).get("query_mode", "all"))
            plans.append((instance, tasks))

        listings = scraper.collect(plans, matcher)
    finally:
        requests_fetcher.close()
        if browser_fetcher is not None:
            browser_fetcher.close()

    log.info("Celkem %s relevantních inzerátů", len(listings))
    if not listings:
        log.warning("Žádné inzeráty - weby mohly změnit HTML nebo blokují requesty")
        return 0

    # Katalog pro stránku se plní jen z úplného běhu. Při --only / --site
    # chybí nálezy ostatních kategorií a webů, takže by se katalog zúžil.
    if not args.dry_run and not args.only and not args.site:
        count = catalog.update(
            args.data, listings, all_categories, total_budget,
            retention_days=int(config.get("catalog_retention_days",
                                          catalog.DEFAULT_RETENTION_DAYS)),
        )
        log.info("docs/data.json zaktualizován (%s inzerátů v katalogu)", count)

    seen = load_seen(args.seen)
    fresh = select_new(
        listings, seen,
        allow_missing_price=bool(config.get("allow_missing_price", False)),
    )
    log.info("Nových inzerátů v limitu kategorie: %s", len(fresh))
    for listing in fresh:
        log.info("  [%s] %s | %s | %s | %s",
                 listing.category_label, listing.price_label, listing.site,
                 listing.title, listing.url)

    if not fresh:
        return 0

    if args.dry_run:
        log.info("--dry-run: e-mail se neposílá a seen_ids.json se neukládá")
        return 0

    if send_mail:
        try:
            mailer.send_listings(fresh, smtp_config, all_categories, total_budget)
        except Exception as exc:
            # Bez odeslaného e-mailu nesmíme označit inzeráty za viděné,
            # jinak by se příští běh o nich už nedozvěděl.
            log.error("Odeslání e-mailu selhalo, seen_ids.json neukládám: %s", exc)
            return 1

    timestamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for listing in fresh:
        seen[listing.uid] = timestamp
    save_seen(args.seen, seen, int(config.get("max_seen_ids", 5000)))
    log.info("seen_ids.json zaktualizován (%s záznamů)", len(seen))
    return 0


if __name__ == "__main__":
    sys.exit(main())
