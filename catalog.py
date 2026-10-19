"""Trvalý katalog nálezů pro webovou stránku (docs/data.json).

Na rozdíl od ``seen_ids.json``, který drží jen ID odeslaných inzerátů, tady
zůstávají celé záznamy, aby je stránka mohla vypsat. Katalog se při každém
běhu slučuje s aktuálním výsledkem hledání:

* nový inzerát se přidá s ``first_seen``,
* známý inzerát dostane nový ``last_seen`` a aktuální cenu,
* inzerát, který už se delší dobu neobjevil (nejspíš prodaný), se odklidí.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from typing import Dict, List, Optional, Sequence

import specs

log = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 14


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value: str) -> Optional[dt.datetime]:
    try:
        parsed = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def load(path: str) -> Dict[str, Dict]:
    """Načte mapu ``uid -> záznam``. Poškozený soubor katalog nezabije."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("%s nelze přečíst (%s), zakládám nový katalog", path, exc)
        return {}

    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        log.warning("Neznámý formát %s, zakládám nový katalog", path)
        return {}
    return {
        str(item["uid"]): item
        for item in items
        if isinstance(item, dict) and item.get("uid")
    }


def merge(
    stored: Dict[str, Dict],
    listings: Sequence,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    specs_cfg: Optional[Dict] = None,
) -> List[Dict]:
    """Sloučí nálezy se starým katalogem a vrátí seřazené záznamy."""
    now = _now()
    merged = dict(stored)

    for listing in listings:
        previous = merged.get(listing.uid, {})
        merged[listing.uid] = {
            "uid": listing.uid,
            "site": listing.site,
            "title": listing.title,
            "url": listing.url,
            "price": listing.price,
            "category_key": listing.category_key,
            "category_label": listing.category_label,
            "category_max_price": listing.category_max_price,
            "first_seen": previous.get("first_seen") or now,
            "last_seen": now,
        }

    # Parametry se počítají u všech záznamů, ne jen u dnes nalezených, aby se
    # oprava tabulky kódů v config.json projevila i na starších inzerátech.
    for item in merged.values():
        item["specs"] = specs.extract(item.get("title", ""), specs_cfg)

    if retention_days > 0:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retention_days)
        kept = {}
        for uid, item in merged.items():
            last_seen = _parse_iso(item.get("last_seen", ""))
            if last_seen is not None and last_seen < cutoff:
                continue
            kept[uid] = item
        dropped = len(merged) - len(kept)
        if dropped:
            log.info("Katalog: odklizeno %s inzerátů starších než %s dní",
                     dropped, retention_days)
        merged = kept

    return sorted(
        merged.values(),
        key=lambda item: (
            item.get("category_key") or "",
            item.get("price") is None,
            item.get("price") or 0,
        ),
    )


def save(
    path: str,
    items: Sequence[Dict],
    categories: Sequence,
    total_budget: Optional[int] = None,
    bike: Optional[Dict] = None,
) -> None:
    """Zapíše katalog atomicky, včetně kategorií a profilu kola."""
    payload = {
        "generated_at": _now(),
        "total_budget": total_budget,
        "bike": bike or {},
        "categories": [
            {"key": c.key, "label": c.label, "max_price": c.max_price}
            for c in categories
        ],
        "items": list(items),
    }
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp_path, path)


def update(
    path: str,
    listings: Sequence,
    categories: Sequence,
    total_budget: Optional[int] = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    specs_cfg: Optional[Dict] = None,
    bike: Optional[Dict] = None,
) -> int:
    """Načti, sluč, zapiš. Vrací počet záznamů v katalogu."""
    items = merge(load(path), listings, retention_days, specs_cfg)
    save(path, items, categories, total_budget, bike)
    return len(items)
