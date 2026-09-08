"""Deterministic organization-to-logo-domain registry for browser rendering."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from vdai.models import SourceConfig

_BRAND_KEY = re.compile(r"[^a-z0-9]+")
_HOSTNAME = re.compile(r"^[a-z0-9.-]+$")
_CURATED_ENTITY_DOMAINS = {
    "atlas": "atlas.mitre.org",
    "dealer services network": "dsn.net",
    "j s held": "jsheld.com",
    "js held": "jsheld.com",
    "kbra": "kbra.com",
    "kroll bond rating agency": "kbra.com",
    "mitre atlas": "atlas.mitre.org",
}


def normalize_brand_key(value: Any) -> str:
    """Normalize a label without guessing corporate suffixes or ownership."""

    return _BRAND_KEY.sub(" ", str(value or "").casefold()).strip()


def normalize_logo_domain(value: Any) -> str:
    """Return a safe bare hostname, accepting either a domain or an HTTPS URL."""

    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("https://"):
        try:
            raw = urlsplit(raw).hostname or ""
        except ValueError:
            return ""
    raw = raw.removeprefix("www.").strip(".")
    if not raw or "/" in raw or "@" in raw or "." not in raw or not _HOSTNAME.fullmatch(raw):
        return ""
    return raw


def build_brand_domains(
    sources: Iterable[SourceConfig],
    company_watchlist: Iterable[Mapping[str, Any]],
) -> dict[str, str]:
    """Build lookup keys for source IDs, organizations, companies, and aliases."""

    domains: dict[str, str] = {}
    for source in sources:
        domain = normalize_logo_domain(source.logo_domain) or normalize_logo_domain(source.url)
        if not domain:
            continue
        domains[f"source:{source.source_id.casefold()}"] = domain
        for label in (source.organization, source.source_name):
            if key := normalize_brand_key(label):
                domains[key] = domain

    for row in company_watchlist:
        domain = normalize_logo_domain(row.get("logo_domain")) or normalize_logo_domain(
            row.get("official_url")
        )
        if not domain:
            continue
        company_id = str(row.get("company_id") or "").strip().casefold()
        if company_id:
            domains[f"company:{company_id}"] = domain
        labels: list[Any] = [
            row.get("canonical_name"),
            row.get("company"),
            row.get("name"),
            row.get("parent_owner"),
        ]
        aliases = row.get("aliases")
        if isinstance(aliases, str):
            labels.extend(re.split(r"[|;,]", aliases))
        elif isinstance(aliases, Iterable):
            labels.extend(aliases)
        for label in labels:
            if key := normalize_brand_key(label):
                domains[key] = domain
    # Vetted entity domains take precedence over a discovery publisher or
    # repository host so a story never labels a publisher favicon as the
    # featured company's logo.
    domains.update(_CURATED_ENTITY_DOMAINS)
    return domains


__all__ = ["build_brand_domains", "normalize_brand_key", "normalize_logo_domain"]
