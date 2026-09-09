"""Deterministic, email-safe terminal rendering for digest editions."""

# The email markup is intentionally inline and self-contained for client compatibility.
# ruff: noqa: E501

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from html import escape
from typing import Any
from urllib.parse import quote, urlsplit

from vdai.branding import normalize_brand_key, normalize_logo_domain
from vdai.dashboard import render_dashboard
from vdai.models import Edition


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    """The two email bodies, subject, and browser-only companion view."""

    subject: str
    html: str
    text: str
    browser_html: str = ""


_SECTION_LABELS = {
    "lead": "TOP STORY",
    "deal_tape": "VENTURE DEBT",
    "competitive_field": "BANKS + DEPOSITS",
    "ai_radar": "AI INTELLIGENCE",
    "runway_watch": "VENTURE CAPITAL",
}
_SECTION_CODES = {
    "lead": "TOP",
    "deal_tape": "DEBT",
    "competitive_field": "BANK",
    "ai_radar": "AI",
    "runway_watch": "VC",
}
_SECTION_ORDER = tuple(_SECTION_LABELS)


def render_digest(
    digest: dict[str, Any] | Edition,
    *,
    test_mode: bool = False,
    brand_domains: Mapping[str, str] | None = None,
    subject_prefix: str = "",
) -> RenderedEmail:
    """Render the validated edition without network access or model calls."""

    if isinstance(digest, Edition):
        digest = {
            "edition_date": digest.edition_date.isoformat(),
            "edition": {
                "kicker": "VENTURE DEBT + AI",
                "headline": digest.subject,
                "deck": "",
            },
            "validated_stories": [story.model_dump(mode="json") for story in digest.stories],
        }
    edition = dict(digest.get("edition") or {})
    headline = _text(edition.get("headline")) or "Venture Debt + AI Daily"
    edition_date = _text(digest.get("edition_date"))
    subject = ("[TEST] " if test_mode else "") + subject_prefix + headline
    grouped = _group_stories(digest)
    stories = [story for section in _SECTION_ORDER for story in grouped[section]]
    registry = _brand_registry(digest, brand_domains)
    story_count = len(stories)
    count_label = f"{story_count} {'STORY' if story_count == 1 else 'STORIES'}"
    tape_html = _render_tape(stories, registry)
    glance_text = _render_plain_at_a_glance(stories, count_label)

    section_rows: list[str] = []
    story_index = 0
    for section in _SECTION_ORDER:
        section_stories = grouped[section]
        if not section_stories:
            continue
        section_rows.append(
            _render_section(section, section_stories, registry, start_index=story_index + 1)
        )
        story_index += len(section_stories)
    sections_html = "".join(section_rows)
    plain_sections = "\n\n".join(
        _render_plain_section(section, grouped[section])
        for section in _SECTION_ORDER
        if grouped[section]
    )
    if not sections_html:
        sections_html = _render_quiet_state()
        plain_sections = "No material stories today."
    test_banner = (
        '<tr><td bgcolor="#ff9f1c" style="padding:8px 18px;background:#ff9f1c;'
        'background-image:linear-gradient(#ff9f1c,#ff9f1c);color:#100d08!important;'
        '-webkit-text-fill-color:#100d08!important;'
        'font-family:Consolas,\'Courier New\',monospace;font-size:10px;line-height:15px;'
        'font-weight:800;letter-spacing:1px;text-align:center;">'
        "TEST RENDER · EMAIL DELIVERY DISABLED</td></tr>"
        if test_mode
        else ""
    )
    html = (
        "<!doctype html><html><head>"
        '<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="dark only">'
        '<meta name="supported-color-schemes" content="dark">'
        "<style>:root{color-scheme:dark only;supported-color-schemes:dark}"
        "body,.vd-root{background-color:#050706;background-image:linear-gradient(#050706,#050706)}"
        ".vd-shell{background-color:#090d0b;background-image:linear-gradient(#090d0b,#090d0b)}"
        "u + .vd-root .gmail-blend-screen{background:#000;mix-blend-mode:screen}"
        "u + .vd-root .gmail-blend-difference{background:#000;mix-blend-mode:difference}"
        "@media only screen and (max-width:720px){"
        ".vd-shell{width:100%!important;max-width:100%!important}"
        ".vd-tape-row,.vd-story-head,.vd-story-fields{width:100%!important;max-width:100%!important;table-layout:auto!important}"
        ".vd-logo-group{width:auto!important;max-width:none!important;table-layout:auto!important}"
        ".vd-logo-cell{width:auto!important;max-width:none!important;padding-right:8px!important}"
        ".vd-shell td,.vd-shell div,.vd-shell a{overflow-wrap:anywhere!important;word-break:break-word!important}"
        ".vd-pad{padding-left:14px!important;padding-right:14px!important}"
        ".vd-hide-mobile{display:none!important}.vd-title{font-size:26px!important;line-height:33px!important}"
        ".vd-date{float:none!important;display:block!important;margin-top:2px!important}"
        ".vd-label{width:100%!important;display:block!important;padding-bottom:4px!important}"
        ".vd-copy{box-sizing:border-box!important;width:100%!important;display:block!important;border-left:0!important;padding-left:0!important}"
        "}</style></head>"
        '<body class="vd-root" bgcolor="#050706" text="#e7eee8" style="margin:0;padding:0;'
        'background:#050706;background-image:linear-gradient(#050706,#050706);'
        'color:#e7eee8!important;-webkit-text-fill-color:#e7eee8!important;">'
        '<table class="vd-root" role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'bgcolor="#050706" style="width:100%;background:#050706;'
        'background-image:linear-gradient(#050706,#050706);border-collapse:collapse;">'
        '<tr><td align="center" bgcolor="#050706" style="padding:12px 8px;background:#050706;'
        'background-image:linear-gradient(#050706,#050706);'
        'text-align:center;">'
        '<table align="center" role="presentation" width="920" cellpadding="0" cellspacing="0" border="0" '
        'class="vd-shell" bgcolor="#090d0b" style="width:92%;max-width:920px;margin:0 auto;'
        'text-align:left;background:#090d0b;background-image:linear-gradient(#090d0b,#090d0b);'
        'border:1px solid #283329;border-collapse:collapse;">'
        + test_banner
        + '<tr><td bgcolor="#050706" style="padding:8px 14px;background:#050706;'
        'background-image:linear-gradient(#050706,#050706);color:#ff9f1c!important;'
        '-webkit-text-fill-color:#ff9f1c!important;border-bottom:1px solid #ff9f1c;'
        'font-family:Consolas,\'Courier New\',monospace;font-size:11px;line-height:16px;'
        'font-weight:900;letter-spacing:.5px;">VENTURE DEBT + AI BRIEF'
        + '<span class="vd-date" style="float:right;color:#ff9f1c!important;'
        '-webkit-text-fill-color:#ff9f1c!important;">'
        + escape(edition_date)
        + " / PT</span></td></tr>"
        + '<tr><td class="vd-pad" bgcolor="#090d0b" style="padding:18px 20px 16px;'
        'background:#090d0b;background-image:linear-gradient(#090d0b,#090d0b);'
        'border-bottom:1px solid #283329;">'
        '<div class="vd-title" style="font-family:Arial,Helvetica,sans-serif;'
        'font-size:32px;line-height:40px;font-weight:700;color:#ffffff!important;'
        '-webkit-text-fill-color:#ffffff!important;">'
        + _white_headline(headline)
        + "</div></td></tr>"
        + '<tr><td bgcolor="#090d0b" style="padding:0;background:#090d0b;'
        'background-image:linear-gradient(#090d0b,#090d0b);border-bottom:1px solid #283329;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'bgcolor="#090d0b" style="width:100%;background:#090d0b;'
        'background-image:linear-gradient(#090d0b,#090d0b);border-collapse:collapse;">'
        '<tr><td bgcolor="#121a14" style="padding:7px 10px;background:#121a14;'
        'background-image:linear-gradient(#121a14,#121a14);'
        'font-family:Consolas,\'Courier New\',monospace;font-size:10px;'
        'font-weight:800;color:#ff9f1c!important;-webkit-text-fill-color:#ff9f1c!important;">'
        + escape(count_label)
        + "</td></tr>"
        + tape_html
        + "</table></td></tr>"
        + sections_html
        + "</table></td></tr></table></body></html>"
    )

    plain_header = ["VENTURE DEBT + AI BRIEF", edition_date, headline]
    if test_mode:
        plain_header.append("TEST RENDER · EMAIL DELIVERY DISABLED")
    text = "\n".join(value for value in plain_header if value) + "\n\n" + glance_text
    text += "\n\n" + plain_sections
    return RenderedEmail(
        subject=subject,
        html=html,
        text=text.strip(),
        browser_html=render_dashboard(
            digest,
            test_mode=test_mode,
            brand_domains=brand_domains,
        ),
    )


def _white_headline(value: str) -> str:
    """Keep white headline glyphs visible when Gmail iOS inverts text colors.

    Gmail-only blend layers counter its color inversion; other clients see ordinary
    escaped text. Do not wrap logos or orange labels in these white-text layers.
    https://www.hteumeuleu.com/2021/fixing-gmail-dark-mode-css-blend-modes/
    """

    return (
        '<div class="gmail-blend-screen"><div class="gmail-blend-difference">'
        + escape(value)
        + "</div></div>"
    )


def _render_tape(stories: list[dict[str, Any]], registry: Mapping[str, str]) -> str:
    if not stories:
        return (
            '<tr><td bgcolor="#090d0b" style="padding:16px 12px;background:#090d0b;'
            'background-image:linear-gradient(#090d0b,#090d0b);'
            'font-family:Consolas,\'Courier New\',monospace;font-size:11px;'
            'color:#8a998d!important;-webkit-text-fill-color:#8a998d!important;">'
            'NO MATERIAL STORIES TODAY</td></tr>'
        )
    rows = []
    for index, story in enumerate(stories, start=1):
        section = _story_section(story)
        rows.append(
            '<tr><td bgcolor="#090d0b" style="padding:7px 10px;background:#090d0b;'
            'background-image:linear-gradient(#090d0b,#090d0b);border-top:1px solid #172019;">'
            '<table class="vd-tape-row" role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            'bgcolor="#090d0b" style="width:100%;background:#090d0b;'
            'background-image:linear-gradient(#090d0b,#090d0b);border-collapse:collapse;">'
            '<tr><td width="28" valign="middle" bgcolor="#090d0b" '
            'style="font-family:Consolas,\'Courier New\',monospace;font-size:10px;'
            'color:#ff9f1c!important;-webkit-text-fill-color:#ff9f1c!important;">'
            + f"{index}."
            + '</td><td class="vd-logo-cell" width="76" valign="middle" bgcolor="#090d0b" '
            'style="width:76px;padding-right:8px;">'
            + _render_logo(story, registry, size=24)
            + '</td><td class="vd-hide-mobile" width="54" valign="middle" bgcolor="#090d0b" style="font-family:Consolas,'
            "'Courier New',monospace;font-size:9px;font-weight:800;color:#4ee7ff!important;-webkit-text-fill-color:#4ee7ff!important;\">"
            + escape(_SECTION_CODES[section])
            + '</td><td valign="middle" bgcolor="#090d0b" style="font-family:Arial,Helvetica,sans-serif;'
            'background:#090d0b;background-image:linear-gradient(#090d0b,#090d0b);'
            'font-size:16px;line-height:23px;font-weight:700;color:#ffffff!important;'
            '-webkit-text-fill-color:#ffffff!important;">'
            + _white_headline(_text(story.get("headline")))
            + "</td></tr></table></td></tr>"
        )
    return "".join(rows)


def _render_section(
    section: str,
    stories: list[dict[str, Any]],
    registry: Mapping[str, str],
    *,
    start_index: int,
) -> str:
    story_rows = "".join(
        _render_story(story, section=section, index=start_index + offset, registry=registry)
        for offset, story in enumerate(stories)
    )
    return (
        '<tr><td bgcolor="#090d0b" style="padding:0;background:#090d0b;'
        'background-image:linear-gradient(#090d0b,#090d0b);border-bottom:1px solid #283329;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'bgcolor="#090d0b" style="width:100%;background:#090d0b;'
        'background-image:linear-gradient(#090d0b,#090d0b);border-collapse:collapse;">'
        '<tr><td bgcolor="#111418" style="padding:7px 10px;background:#111418;'
        'background-image:linear-gradient(#111418,#111418);'
        'font-family:Consolas,\'Courier New\',monospace;font-size:10px;'
        'font-weight:800;color:#ff9f1c!important;-webkit-text-fill-color:#ff9f1c!important;">'
        + escape(_SECTION_LABELS[section])
        + "</td></tr>"
        + story_rows
        + "</table></td></tr>"
    )


def _render_story(
    story: dict[str, Any],
    *,
    section: str,
    index: int,
    registry: Mapping[str, str],
) -> str:
    return (
        '<tr><td class="vd-pad" bgcolor="#090d0b" style="padding:0 20px 18px;background:#090d0b;'
        'background-image:linear-gradient(#090d0b,#090d0b);border-top:1px solid #172019;">'
        '<table class="vd-story-head" role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'bgcolor="#090d0b" style="width:100%;background:#090d0b;'
        'background-image:linear-gradient(#090d0b,#090d0b);border-collapse:collapse;">'
        '<tr><td width="38" bgcolor="#090d0b" style="padding:8px 0;'
        'font-family:Consolas,\'Courier New\',monospace;font-size:13px;font-weight:900;'
        'color:#ff9f1c!important;-webkit-text-fill-color:#ff9f1c!important;">'
        + f"{index}."
        + '</td><td class="vd-logo-cell" width="84" bgcolor="#090d0b" '
        'style="width:84px;padding:8px 8px 8px 0;">'
        + _render_logo(story, registry, size=28)
        + '</td><td bgcolor="#090d0b" style="padding:8px 0;font-family:Consolas,\'Courier New\',monospace;'
        'font-size:10px;line-height:14px;font-weight:800;color:#4ee7ff!important;'
        '-webkit-text-fill-color:#4ee7ff!important;">'
        + escape(_SECTION_CODES[section])
        + " / "
        + escape(_story_company(story).upper())
        + "</td></tr></table>"
        '<div style="padding:11px 0 14px;font-family:Arial,Helvetica,sans-serif;'
        'background:#090d0b;background-image:linear-gradient(#090d0b,#090d0b);'
        'font-size:25px;line-height:32px;font-weight:700;color:#ffffff!important;'
        '-webkit-text-fill-color:#ffffff!important;">'
        + _white_headline(_text(story.get("headline")))
        + '</div><table class="vd-story-fields" role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'border="0" bgcolor="#090d0b" style="width:100%;background:#090d0b;'
        'background-image:linear-gradient(#090d0b,#090d0b);'
        'border-top:1px solid #172019;border-collapse:collapse;">'
        + _render_field("WHAT HAPPENED", _text(story.get("dek")), strong=True)
        + _render_field("ANALYST COMMENTARY", _text(story.get("why_it_matters")))
        + "</table>"
        + _render_sources(story)
        + "</td></tr>"
    )


def _render_field(label: str, value: str, *, strong: bool = False) -> str:
    weight = "750" if strong else "600"
    copy_color = "#dce3dd" if strong else "#aebbb0"
    return (
        '<tr><td class="vd-label" width="145" valign="top" bgcolor="#090d0b" '
        'style="width:145px;padding:11px 10px 11px 0;background:#090d0b;'
        'background-image:linear-gradient(#090d0b,#090d0b);'
        'border-bottom:1px solid #172019;font-family:Consolas,\'Courier New\',monospace;'
        'font-size:9px;line-height:14px;font-weight:900;letter-spacing:.7px;'
        'color:#ff9f1c!important;-webkit-text-fill-color:#ff9f1c!important;">'
        + escape(label)
        + '</td><td class="vd-copy" valign="top" bgcolor="#090d0b" '
        'style="padding:10px 0 11px 14px;background:#090d0b;'
        'background-image:linear-gradient(#090d0b,#090d0b);'
        'border-left:1px solid #172019;border-bottom:1px solid #172019;'
        'font-family:Consolas,\'Courier New\',monospace;font-size:14px;line-height:22px;'
        "white-space:pre-line;font-weight:"
        + weight
        + ";color:"
        + copy_color
        + "!important;-webkit-text-fill-color:"
        + copy_color
        + "!important"
        + ';">'
        + escape(value)
        + "</td></tr>"
    )


def _render_logo(story: Mapping[str, Any], registry: Mapping[str, str], *, size: int) -> str:
    cells: list[str] = []
    seen: set[str] = set()
    for label in _story_logo_entities(story):
        domain = _resolve_logo_domain_for_label(label, story, registry)
        durable_key = domain or normalize_brand_key(label)
        if not durable_key or durable_key in seen:
            continue
        seen.add(durable_key)
        cells.append(
            '<td valign="middle" style="padding:0 3px 0 0;">'
            + _render_logo_mark(label, domain, size=size)
            + "</td>"
        )
    return (
        '<table class="vd-logo-group" role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:auto;max-width:none;table-layout:auto;border-collapse:separate;"><tr>'
        + "".join(cells)
        + "</tr></table>"
    )


def _render_logo_mark(label: str, domain: str, *, size: int) -> str:
    if domain:
        favicon_url = (
            "https://www.google.com/s2/favicons?domain_url=https%3A%2F%2F"
            + quote(domain, safe="")
            + "&sz=128"
        )
        return (
            '<img src="'
            + escape(favicon_url, quote=True)
            + '" width="'
            + str(size)
            + '" height="'
            + str(size)
            + '" alt="'
            + escape(label, quote=True)
            + ' logo" style="display:block;width:'
            + str(size)
            + "px;height:"
            + str(size)
            + 'px;padding:3px;background:#ffffff;border:1px solid #59626b;object-fit:contain;">'
        )
    return (
        '<span style="display:inline-block;width:'
        + str(size)
        + "px;height:"
        + str(size)
        + "px;line-height:"
        + str(size)
        + 'px;text-align:center;background:#f7f8fa;border:1px solid #59626b;'
        'font-family:Consolas,\'Courier New\',monospace;font-size:9px;font-weight:900;'
        'color:#111418!important;-webkit-text-fill-color:#111418!important;">'
        + escape(_initials(label))
        + "</span>"
    )


def _render_sources(story: Mapping[str, Any]) -> str:
    links = []
    for citation in story.get("citations") or []:
        if not isinstance(citation, Mapping):
            continue
        url = _approved_https_url(citation.get("url"))
        if not url:
            continue
        publisher = _text(citation.get("publisher")) or _text(citation.get("source_id")) or "Source"
        links.append(
            '<a href="'
            + escape(url, quote=True)
            + '" style="display:inline-block;margin:4px 7px 0 0;padding:4px 7px;'
            'border:1px solid #2f4f39;color:#6cff8f!important;'
            '-webkit-text-fill-color:#6cff8f!important;text-decoration:none;'
            'font-family:Consolas,\'Courier New\',monospace;font-size:9px;line-height:13px;">'
            + escape(publisher.upper())
            + " ↗</a>"
        )
    if not links:
        return ""
    return (
        '<div style="padding-top:9px;font-family:Consolas,\'Courier New\',monospace;'
        'font-size:8px;color:#667268!important;-webkit-text-fill-color:#667268!important;">'
        'SOURCES&nbsp;&nbsp;'
        + "".join(links)
        + "</div>"
    )


def _render_quiet_state() -> str:
    return (
        '<tr><td class="vd-pad" bgcolor="#090d0b" style="padding:28px 20px;background:#090d0b;'
        'background-image:linear-gradient(#090d0b,#090d0b);border-bottom:1px solid #283329;'
        'font-family:Consolas,\'Courier New\',monospace;"><div style="font-size:20px;'
        'line-height:25px;font-weight:900;color:#ffffff!important;'
        '-webkit-text-fill-color:#ffffff!important;">NO MATERIAL STORIES TODAY</div>'
        "</td></tr>"
    )


def _brand_registry(
    digest: Mapping[str, Any], supplied: Mapping[str, str] | None
) -> dict[str, str]:
    registry: dict[str, str] = {}
    embedded = digest.get("brand_domains")
    for mapping in (embedded if isinstance(embedded, Mapping) else {}, supplied or {}):
        for key, value in mapping.items():
            domain = normalize_logo_domain(value)
            if not domain:
                continue
            raw_key = _text(key).casefold()
            if raw_key.startswith(("source:", "company:")):
                registry[raw_key] = domain
            elif normalized_key := normalize_brand_key(key):
                registry[normalized_key] = domain
    return registry


def _resolve_logo_domain(story: Mapping[str, Any], registry: Mapping[str, str]) -> str:
    return _resolve_logo_domain_for_label(_story_company(story), story, registry)


def _resolve_logo_domain_for_label(
    label: str, story: Mapping[str, Any], registry: Mapping[str, str]
) -> str:
    primary_label = _story_company(story)
    is_primary = normalize_brand_key(label) == normalize_brand_key(primary_label)
    if is_primary and (explicit := normalize_logo_domain(story.get("logo_domain"))):
        return explicit
    if (key := normalize_brand_key(label)) and (domain := registry.get(key)):
        return domain
    if not is_primary:
        return ""
    labels = [primary_label]
    citations = story.get("citations") or []
    for citation in citations:
        if not isinstance(citation, Mapping):
            continue
        source_id = _text(citation.get("source_id")).casefold()
        if source_id and (domain := registry.get(f"source:{source_id}")):
            return domain
        labels.append(citation.get("publisher"))
    for label in labels:
        if (key := normalize_brand_key(label)) and (domain := registry.get(key)):
            return domain
    for citation in citations:
        if not isinstance(citation, Mapping):
            continue
        url = _approved_https_url(citation.get("url"))
        if url:
            return normalize_logo_domain(url)
    return ""


def _story_logo_entities(story: Mapping[str, Any]) -> list[str]:
    values: list[Any] = [_story_company(story)]
    raw_entities = story.get("entity_names") or []
    if isinstance(raw_entities, str):
        values.extend(part.strip() for part in raw_entities.split("|") if part.strip())
    elif isinstance(raw_entities, (list, tuple)):
        values.extend(raw_entities)
    lender = _text(story.get("lender"))
    if lender:
        values.extend(part.strip() for part in lender.split(",") if part.strip())
    labels: list[str] = []
    seen: set[str] = set()
    for value in values:
        label = _text(value)
        key = normalize_brand_key(label)
        if label and key and key not in seen:
            seen.add(key)
            labels.append(label)
    return labels


def _story_company(story: Mapping[str, Any]) -> str:
    company = _text(story.get("company"))
    if company:
        return company
    headline = _text(story.get("headline"))
    prefix = re.split(r"\s+[\u2014\u2013-]\s+", headline, maxsplit=1)[0].strip()
    if prefix and prefix != headline:
        return prefix
    for citation in story.get("citations") or []:
        if isinstance(citation, Mapping) and _text(citation.get("publisher")):
            return _text(citation.get("publisher"))
    return "Source"


def _initials(label: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", label)
    if not words:
        return "•"
    if len(words) > 1:
        return (words[0][0] + words[1][0]).upper()
    capitals = "".join(character for character in words[0] if character.isupper())
    return (capitals[:2] or words[0][:2]).upper()


def _story_section(story: Mapping[str, Any]) -> str:
    section = _text(story.get("section"))
    return section if section in _SECTION_LABELS else "lead"


def _group_stories(digest: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped = {section: [] for section in _SECTION_ORDER}
    validated = digest.get("validated_stories")
    if isinstance(validated, list):
        for story in validated:
            if not isinstance(story, dict):
                continue
            section = _text(story.get("section"))
            if section in grouped:
                grouped[section].append(story)
        return grouped
    lead = digest.get("lead")
    if isinstance(lead, dict):
        grouped["lead"].append(lead)
    for section in _SECTION_ORDER[1:]:
        stories = digest.get(section)
        if isinstance(stories, list):
            grouped[section].extend(story for story in stories if isinstance(story, dict))
    return grouped


def _render_plain_section(section: str, stories: list[dict[str, Any]]) -> str:
    rendered = [_SECTION_LABELS[section]]
    for story in stories:
        rendered.extend(
            [
                "",
                _text(story.get("headline")),
                _text(story.get("dek")),
                "",
                "ANALYST COMMENTARY",
                _text(story.get("why_it_matters")),
            ]
        )
        citations = []
        for citation in story.get("citations") or []:
            if not isinstance(citation, Mapping):
                continue
            url = _approved_https_url(citation.get("url"))
            if url:
                publisher = _text(citation.get("publisher")) or "Source"
                citations.append(f"{publisher}: {url}")
        if citations:
            rendered.append("Sources: " + " | ".join(citations))
    return "\n".join(rendered)


def _render_plain_at_a_glance(stories: list[dict[str, Any]], count_label: str) -> str:
    rendered = [count_label]
    if stories:
        rendered.extend(
            f"{index}. " + _text(story.get("headline"))
            for index, story in enumerate(stories, start=1)
        )
    else:
        rendered.append("No stories today.")
    return "\n".join(rendered)


def _approved_https_url(value: Any) -> str:
    url = _text(value)
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return ""
    return url


def _text(value: Any) -> str:
    return str(value or "").strip()


__all__ = ["RenderedEmail", "render_digest"]
