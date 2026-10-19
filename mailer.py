"""Odeslání přehledu nových inzerátů e-mailem přes smtplib."""

from __future__ import annotations

import logging
import os
import smtplib
from collections import OrderedDict
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from html import escape
from typing import Dict, List, Optional, Sequence

log = logging.getLogger(__name__)

REQUIRED_ENV = ("SMTP_SERVER", "SMTP_USERNAME", "SMTP_PASSWORD", "EMAIL_TO")

SITE_LABELS = {"bazos": "Bazoš", "cyklobazar": "Cyklobazar"}


class MailerConfigError(RuntimeError):
    """Chybí nebo je chybná SMTP konfigurace."""


@dataclass
class SmtpConfig:
    server: str
    port: int
    username: str
    password: str
    recipients: List[str]
    sender: str
    use_ssl: bool

    @classmethod
    def from_env(cls) -> "SmtpConfig":
        missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
        if missing:
            raise MailerConfigError(
                "Chybí environment proměnné: " + ", ".join(missing)
            )

        raw_port = os.environ.get("SMTP_PORT", "587").strip()
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise MailerConfigError(
                "SMTP_PORT musí být číslo, dostal jsem {!r}".format(raw_port)
            ) from exc

        recipients = [
            addr.strip()
            for addr in os.environ["EMAIL_TO"].replace(";", ",").split(",")
            if addr.strip()
        ]
        if not recipients:
            raise MailerConfigError("EMAIL_TO neobsahuje žádnou adresu")

        username = os.environ["SMTP_USERNAME"].strip()
        return cls(
            server=os.environ["SMTP_SERVER"].strip(),
            port=port,
            username=username,
            password=os.environ["SMTP_PASSWORD"],
            recipients=recipients,
            sender=os.environ.get("EMAIL_FROM", "").strip() or username,
            use_ssl=port == 465,
        )


def _plural(count: int, one: str, few: str, many: str) -> str:
    """České skloňování: 1 nabídka, 2-4 nabídky, 5+ nabídek."""
    if count == 1:
        return one
    if 2 <= count <= 4:
        return few
    return many


def _money(value: Optional[int]) -> str:
    if value is None:
        return "-"
    return "{:,} Kč".format(value).replace(",", " ")


def group_by_category(listings: Sequence, categories: Sequence) -> "OrderedDict":
    """Seskupí inzeráty podle kategorie v pořadí z configu."""
    groups = OrderedDict()
    for category in categories:
        groups[category.key] = (category.label, [])
    for listing in listings:
        if listing.category_key not in groups:
            groups[listing.category_key] = (
                listing.category_label or listing.category_key, []
            )
        groups[listing.category_key][1].append(listing)
    # Prázdné kategorie v e-mailu nechceme.
    return OrderedDict(
        (key, value) for key, value in groups.items() if value[1]
    )


def build_subject(listings: Sequence, categories: Sequence) -> str:
    """Předmět se štítky kategorií, např. '[Kazeta] [Kliky] 3 nové nabídky'."""
    groups = group_by_category(listings, categories)
    labels = "".join("[{}] ".format(label) for label, _ in groups.values())
    if len(listings) == 1:
        single = listings[0]
        return "{}{} - {}".format(labels, single.price_label, single.title[:60])
    return "{}{} {}".format(
        labels, len(listings),
        _plural(len(listings), "nová nabídka", "nové nabídky", "nových nabídek"),
    )


def render_text(listings: Sequence, categories: Sequence, total_budget: Optional[int]) -> str:
    groups = group_by_category(listings, categories)
    lines = ["Nové inzeráty ({}):".format(len(listings)), ""]
    for label, items in groups.values():
        limit = items[0].category_max_price
        lines.append("=== {} (limit {}) ===".format(label, _money(limit)))
        for listing in items:
            site = SITE_LABELS.get(listing.site, listing.site)
            lines.append("  {} | {}".format(listing.price_label, listing.title))
            lines.append("    {} | dotaz: {}".format(site, listing.query or "-"))
            lines.append("    {}".format(listing.url))
        lines.append("")

    lines.append("--- Souhrn limitů ---")
    for category in categories:
        lines.append("  {:<16} do {}".format(category.label, _money(category.max_price)))
    if total_budget is not None:
        lines.append("")
        lines.append("Cíl: poskládat celou sadu do {}.".format(_money(total_budget)))
    return "\n".join(lines)


def render_html(listings: Sequence, categories: Sequence, total_budget: Optional[int]) -> str:
    groups = group_by_category(listings, categories)

    sections = []
    for label, items in groups.values():
        limit = items[0].category_max_price
        rows = []
        for listing in items:
            site = SITE_LABELS.get(listing.site, listing.site)
            meta = escape(site)
            if listing.query:
                meta += " &middot; " + escape(listing.query)
            rows.append(
                "<tr>"
                '<td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;">'
                '<a href="{url}" style="color:#1d4ed8;font-weight:600;'
                'text-decoration:none;font-size:15px;">{title}</a>'
                '<div style="color:#6b7280;font-size:12px;margin-top:4px;">'
                "{meta}</div></td>"
                '<td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;'
                'white-space:nowrap;text-align:right;font-weight:700;'
                'color:#047857;font-size:15px;">{price}</td>'
                "</tr>".format(
                    url=escape(listing.url, quote=True),
                    title=escape(listing.title),
                    meta=meta,
                    price=escape(listing.price_label),
                )
            )
        sections.append(
            '<div style="padding:14px 20px 0;">'
            '<div style="display:inline-block;background:#1f2937;color:#ffffff;'
            'border-radius:999px;padding:3px 12px;font-size:12px;'
            'font-weight:700;">{label}</div>'
            '<span style="color:#6b7280;font-size:12px;margin-left:8px;">'
            "limit {limit}</span>"
            '<table style="width:100%;border-collapse:collapse;margin-top:8px;">'
            "{rows}</table></div>".format(
                label=escape(label), limit=escape(_money(limit)),
                rows="".join(rows),
            )
        )

    limit_rows = "".join(
        "<tr>"
        '<td style="padding:3px 10px 3px 0;color:#374151;">{label}</td>'
        '<td style="padding:3px 0;text-align:right;color:#111827;'
        'font-weight:600;">do {limit}</td></tr>'.format(
            label=escape(category.label), limit=escape(_money(category.max_price))
        )
        for category in categories
    )
    budget_note = ""
    if total_budget is not None:
        budget_note = (
            '<p style="margin:10px 0 0;font-size:13px;color:#111827;">'
            "<strong>Cíl: poskládat celou sadu do {}.</strong></p>".format(
                escape(_money(total_budget))
            )
        )

    return (
        '<html><body style="margin:0;padding:20px;background:#f3f4f6;'
        'font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;">'
        '<div style="max-width:680px;margin:0 auto;background:#ffffff;'
        'border-radius:10px;overflow:hidden;border:1px solid #e5e7eb;">'
        '<div style="padding:16px 20px;background:#111827;color:#ffffff;">'
        '<h2 style="margin:0;font-size:17px;">Nové inzeráty &ndash; '
        "Shimano 105 / GRX</h2>"
        '<p style="margin:4px 0 0;font-size:13px;color:#9ca3af;">'
        "{count} {noun} v {groups} {group_noun}</p></div>"
        "{sections}"
        '<div style="margin:18px 20px 20px;padding:14px;background:#f9fafb;'
        'border:1px solid #e5e7eb;border-radius:8px;">'
        '<div style="font-size:13px;font-weight:700;color:#111827;'
        'margin-bottom:6px;">Souhrn limitů</div>'
        '<table style="width:100%;border-collapse:collapse;font-size:13px;">'
        "{limit_rows}</table>{budget_note}</div>"
        '<div style="padding:0 20px 16px;color:#9ca3af;font-size:11px;">'
        "Automatický monitor Bazoš.cz &amp; Cyklobazar.cz</div>"
        "</div></body></html>".format(
            count=len(listings),
            noun=_plural(len(listings), "nová nabídka", "nové nabídky",
                         "nových nabídek"),
            groups=len(groups),
            group_noun=_plural(len(groups), "kategorii", "kategoriích",
                               "kategoriích"),
            sections="".join(sections),
            limit_rows=limit_rows,
            budget_note=budget_note,
        )
    )


def build_message(
    listings: Sequence,
    config: SmtpConfig,
    categories: Sequence,
    total_budget: Optional[int] = None,
) -> MIMEMultipart:
    message = MIMEMultipart("alternative")
    message["Subject"] = build_subject(listings, categories)
    message["From"] = config.sender
    message["To"] = ", ".join(config.recipients)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain="bazar-monitor")
    message.attach(
        MIMEText(render_text(listings, categories, total_budget), "plain", "utf-8")
    )
    message.attach(
        MIMEText(render_html(listings, categories, total_budget), "html", "utf-8")
    )
    return message


def send_listings(
    listings: Sequence,
    config: SmtpConfig,
    categories: Sequence,
    total_budget: Optional[int] = None,
) -> bool:
    """Odešle e-mail. Vrací False, pokud nebylo co odeslat."""
    if not listings:
        log.info("Žádné nové inzeráty, e-mail se neposílá.")
        return False

    message = build_message(listings, config, categories, total_budget)
    log.info("Odesílám %s nových inzerátů na %s",
             len(listings), ", ".join(config.recipients))

    if config.use_ssl:
        smtp = smtplib.SMTP_SSL(config.server, config.port, timeout=30)
    else:
        smtp = smtplib.SMTP(config.server, config.port, timeout=30)
    try:
        smtp.ehlo()
        if not config.use_ssl:
            smtp.starttls()
            smtp.ehlo()
        smtp.login(config.username, config.password)
        smtp.sendmail(config.sender, config.recipients, message.as_string())
    finally:
        try:
            smtp.quit()
        except smtplib.SMTPException:
            smtp.close()
    log.info("E-mail odeslán.")
    return True
