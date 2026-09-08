"""Dense, terminal-style browser rendering for a digest edition."""

# The browser document is intentionally kept as one self-contained template.
# ruff: noqa: E501

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from html import escape
from string import Template
from typing import Any
from urllib.parse import quote, urlsplit

from vdai.branding import normalize_brand_key, normalize_logo_domain
from vdai.models import Edition

_SECTION_LABELS = {
    "lead": "Top",
    "deal_tape": "Venture debt",
    "competitive_field": "Banks + deposits",
    "ai_radar": "AI intelligence",
    "runway_watch": "Venture capital",
}
_SECTION_CODES = {
    "lead": "TOP",
    "deal_tape": "DEBT",
    "competitive_field": "BANK",
    "ai_radar": "AI",
    "runway_watch": "VC",
}
_SECTION_ORDER = tuple(_SECTION_LABELS)
_SECTION_CLASSES = {
    "lead": "lead",
    "deal_tape": "debt",
    "competitive_field": "banks",
    "ai_radar": "ai",
    "runway_watch": "funding",
}


def render_dashboard(
    digest: dict[str, Any] | Edition,
    *,
    test_mode: bool = False,
    brand_domains: Mapping[str, str] | None = None,
) -> str:
    """Render the validated edition as a responsive intelligence terminal."""

    normalized = _normalize_digest(digest)
    registry = _brand_registry(normalized, brand_domains)
    edition = dict(normalized.get("edition") or {})
    headline = _text(edition.get("headline")) or "Venture Debt + AI Brief"
    edition_date = _text(normalized.get("edition_date"))
    grouped = _group_stories(normalized)
    stories = [story for section in _SECTION_ORDER for story in grouped[section]]
    story_count = len(stories)
    title = escape(headline) + " · Venture Debt + AI Brief"
    template = Template(_DASHBOARD_TEMPLATE)
    return template.substitute(
        title=title,
        test_banner=(
            '<div class="test-banner">TEST PREVIEW / DELIVERY DISABLED</div>'
            if test_mode
            else ""
        ),
        headline=escape(headline),
        date_label=escape(_date_label(edition_date).upper()),
        story_count=str(story_count),
        story_word="STORY" if story_count == 1 else "STORIES",
        filters=_render_filters(grouped),
        tape=_render_tape(stories, registry),
        feed=_render_feed(grouped, registry),
    )


def _normalize_digest(digest: dict[str, Any] | Edition) -> dict[str, Any]:
    if not isinstance(digest, Edition):
        return digest
    return {
        "edition_date": digest.edition_date.isoformat(),
        "edition": {
            "kicker": "VENTURE DEBT + AI",
            "headline": digest.subject,
        },
        "validated_stories": [story.model_dump(mode="json") for story in digest.stories],
    }


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
        values = digest.get(section)
        if isinstance(values, list):
            grouped[section].extend(value for value in values if isinstance(value, dict))
    return grouped


def _render_filters(grouped: Mapping[str, list[dict[str, Any]]]) -> str:
    buttons = [
        '<button class="command active" type="button" data-filter="all" data-key="0" aria-pressed="true"><kbd>0</kbd> ALL <b>'
        + str(sum(len(stories) for stories in grouped.values()))
        + "</b></button>"
    ]
    for index, section in enumerate(_SECTION_ORDER, start=1):
        buttons.append(
            '<button class="command" type="button" data-filter="'
            + escape(_SECTION_CLASSES[section], quote=True)
            + '" data-key="'
            + str(index)
            + '" aria-pressed="false"><kbd>'
            + str(index)
            + "</kbd> "
            + escape(_SECTION_CODES[section])
            + " <b>"
            + str(len(grouped[section]))
            + "</b></button>"
        )
    return "".join(buttons)


def _render_tape(stories: list[dict[str, Any]], registry: Mapping[str, str]) -> str:
    if not stories:
        return '<div class="tape-empty">NO MATERIAL STORIES TODAY</div>'
    rows = []
    for index, story in enumerate(stories, start=1):
        section = _story_section(story)
        rows.append(
            '<a class="tape-row" data-lane="'
            + escape(_SECTION_CLASSES.get(section, "all"), quote=True)
            + '" href="#'
            + _story_id(index, story)
            + '"><span class="tape-num">'
            + f"{index}."
            + "</span>"
            + _render_story_logo(story, registry, modifier="tape-logo")
            + '<span class="tape-lane">'
            + escape(_SECTION_CODES.get(section, "NEWS"))
            + '</span><span class="tape-company">'
            + escape(_story_company(story))
            + '</span><span class="tape-headline">'
            + escape(_text(story.get("headline")))
            + "</span></a>"
        )
    return "".join(rows)


def _render_feed(
    grouped: Mapping[str, list[dict[str, Any]]], registry: Mapping[str, str]
) -> str:
    story_index = 0
    sections = []
    for section in _SECTION_ORDER:
        if not grouped[section]:
            continue
        articles = []
        for story in grouped[section]:
            story_index += 1
            articles.append(_render_story(story, registry, section=section, index=story_index))
        sections.append(
            '<section class="story-section" data-section="'
            + escape(_SECTION_CLASSES[section], quote=True)
            + '"><div class="section-bar"><span>'
            + escape(_SECTION_LABELS[section].upper())
            + "</span></div>"
            + "".join(articles)
            + "</section>"
        )
    if sections:
        return "".join(sections)
    return (
        '<section class="quiet-state"><h2>NO MATERIAL STORIES TODAY</h2></section>'
    )


def _render_story(
    story: dict[str, Any],
    registry: Mapping[str, str],
    *,
    section: str,
    index: int,
) -> str:
    story_id = _story_id(index, story)
    commentary = escape(_text(story.get("why_it_matters")))
    dek = escape(_text(story.get("dek")))
    return (
        '<article class="story-panel" id="'
        + story_id
        + '" data-lane="'
        + escape(_SECTION_CLASSES[section], quote=True)
        + '"><header class="story-header"><span class="story-index">'
        + f"{index}."
        + "</span>"
        + _render_story_logo(story, registry)
        + '<div class="story-identity"><span>'
        + escape(_SECTION_CODES[section])
        + " / "
        + escape(_story_company(story).upper())
        + "</span></div></header>"
        + '<div class="story-body"><h2>'
        + escape(_text(story.get("headline")))
        + '</h2><div class="story-grid"><div class="field-label">WHAT HAPPENED</div><p class="story-dek">'
        + dek
        + '</p><div class="field-label analysis-label">ANALYST COMMENTARY</div><div class="analysis-copy">'
        + commentary
        + "</div></div>"
        + _render_sources(story)
        + "</div></article>"
    )


def _render_story_logo(
    story: Mapping[str, Any],
    registry: Mapping[str, str],
    *,
    modifier: str = "",
) -> str:
    marks: list[str] = []
    seen: set[str] = set()
    for label in _story_logo_entities(story):
        domain = _resolve_logo_domain_for_label(label, story, registry)
        durable_key = domain or normalize_brand_key(label)
        if not durable_key or durable_key in seen:
            continue
        seen.add(durable_key)
        marks.append(_render_story_logo_mark(label, domain))
    css_class = "story-logos" + (" " + modifier if modifier else "")
    return '<span class="' + css_class + '">' + "".join(marks) + "</span>"


def _render_story_logo_mark(label: str, domain: str) -> str:
    image = ""
    if domain:
        favicon_url = (
            "https://www.google.com/s2/favicons?domain_url=https%3A%2F%2F"
            + quote(domain, safe="")
            + "&sz=128"
        )
        image = (
            '<img src="'
            + escape(favicon_url, quote=True)
            + '" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.hidden=true">'
        )
    return (
        '<span class="story-logo" aria-label="'
        + escape(label, quote=True)
        + ' logo" title="'
        + escape(domain or label, quote=True)
        + '"><i>'
        + escape(_initials(label))
        + "</i>"
        + image
        + "</span>"
    )


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


def _story_section(story: Mapping[str, Any]) -> str:
    section = _text(story.get("section"))
    return section if section in _SECTION_LABELS else "lead"


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
            + '" target="_blank" rel="noopener noreferrer"><span>READ</span> '
            + escape(publisher.upper())
            + " ↗</a>"
        )
    if not links:
        return ""
    return '<footer class="story-sources"><b>SOURCES</b>' + "".join(links) + "</footer>"


def _initials(label: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", label)
    if not words:
        return "•"
    if len(words) > 1:
        return (words[0][0] + words[1][0]).upper()
    capitals = "".join(character for character in words[0] if character.isupper())
    return (capitals[:2] or words[0][:2]).upper()


def _story_id(index: int, story: Mapping[str, Any]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", _text(story.get("headline")).casefold()).strip("-")
    return "story-" + str(index) + ("-" + slug[:44] if slug else "")


def _date_label(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return value
    return parsed.strftime("%a %d %b %Y")


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


_DASHBOARD_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>$title</title>
  <style>
    :root{--black:#050706;--panel:#090d0b;--panel2:#0e1310;--grid:#283329;--grid2:#172019;--amber:#ff9f1c;--amber2:#ffc15a;--green:#6cff8f;--cyan:#4ee7ff;--white:#e7eee8;--muted:#8a998d;--red:#ff6b5f}
    *{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--black);color:var(--white);font-family:Consolas,"SFMono-Regular","Liberation Mono","Courier New",monospace;font-size:13px;line-height:1.45;-webkit-font-smoothing:antialiased}button,a{font:inherit}.test-banner{padding:6px 16px;background:var(--amber);color:#111;font-weight:800;text-align:center;letter-spacing:.08em}
    .terminal-head{position:sticky;top:0;z-index:30;background:var(--black);border-bottom:1px solid var(--amber)}.identity-bar{height:34px;display:flex;align-items:center;padding:0 14px;background:var(--amber);color:#100d08;font-weight:900;overflow:hidden}.brief-title{font-size:14px;letter-spacing:.04em;white-space:nowrap}.identity-spacer{flex:1}.identity-meta{white-space:nowrap;font-size:11px}.function-bar{display:flex;align-items:stretch;min-height:36px;border-bottom:1px solid var(--grid);overflow-x:auto;scrollbar-width:none}.function-bar::-webkit-scrollbar{display:none}.command{min-width:116px;border:0;border-right:1px solid var(--grid);background:#0a0e0b;color:var(--muted);padding:7px 12px;text-align:left;cursor:pointer;white-space:nowrap}.command kbd{color:var(--amber);font:800 11px inherit}.command b{float:right;color:#566158}.command:hover,.command.active{background:#1a1208;color:var(--amber2)}.command.active b{color:var(--amber)}.command:focus-visible{outline:2px solid var(--cyan);outline-offset:-2px}.function-status{margin-left:auto;display:flex;align-items:center;padding:0 14px;color:var(--green);font-size:11px;white-space:nowrap}
    .workspace{max-width:1600px;margin:0 auto;padding:14px}.screen-title{border:1px solid var(--grid);background:var(--panel)}.title-copy{padding:17px 18px 16px}.title-copy h1{margin:0;color:#fff;font-size:clamp(21px,2.4vw,33px);line-height:1.08;letter-spacing:-.025em}
    .module{margin-top:10px;border:1px solid var(--grid);background:var(--panel)}.module-head{display:flex;align-items:center;gap:10px;height:30px;padding:0 10px;background:#121a14;border-bottom:1px solid var(--grid);color:var(--amber2);font-weight:800}.module-head b{background:var(--amber);color:#0a0906;padding:1px 5px}.module-head span:last-child{margin-left:auto;color:var(--muted);font-size:10px;font-weight:400}.tape-columns,.tape-row{display:grid;grid-template-columns:42px auto 60px 160px minmax(300px,1fr);align-items:center}.tape-columns{min-height:25px;padding:0 8px;color:#637066;font-size:9px;border-bottom:1px solid var(--grid2)}.tape-row{min-height:43px;padding:0 8px;border-bottom:1px solid var(--grid2);color:var(--white);text-decoration:none}.tape-row:last-child{border-bottom:0}.tape-row:hover{background:#141a15}.tape-num{color:var(--amber)}.tape-lane{color:var(--cyan);font-weight:800}.tape-company{color:#b8c5bb;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.tape-headline{padding-right:14px;color:#f3f6f3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.tape-empty{padding:22px;color:var(--muted)}
    .story-logos{display:inline-flex;align-items:center;padding-right:6px}.story-logos .story-logo+.story-logo{margin-left:-7px}.story-logo{position:relative;width:30px;height:30px;display:inline-grid;place-items:center;overflow:hidden;border:1px solid #4a584c;background:#f7faf7;color:#0b100c;font-weight:900}.story-logo i{font-style:normal;font-size:10px}.story-logo img{position:absolute;inset:0;width:100%;height:100%;padding:4px;background:#fff;object-fit:contain}.tape-logo .story-logo{width:25px;height:25px}.tape-logo .story-logo img{padding:3px}
    .market-note{margin-top:10px;padding:10px 12px;border:1px solid #6a4420;background:#181108;color:#c9a36c}.market-note span{margin-right:15px;color:var(--amber);font-weight:900}.feed{margin-top:10px}.story-section{border:1px solid var(--grid);background:var(--panel)}.story-section+.story-section{margin-top:10px}.section-bar{height:31px;padding:6px 10px;background:#121a14;border-bottom:1px solid var(--grid);color:var(--amber);font-weight:900}.story-panel{scroll-margin-top:82px}.story-panel+.story-panel{border-top:3px double var(--grid)}.story-header{display:grid;grid-template-columns:42px auto minmax(0,1fr);align-items:center;min-height:48px;padding:6px 10px;background:#0d120e;border-bottom:1px solid var(--grid2)}.story-index{color:var(--amber);font-size:15px;font-weight:900}.story-identity span{display:block;color:var(--cyan);font-weight:800}.story-body{padding:16px 18px 17px}.story-body h2{max-width:1180px;margin:0 0 16px;color:#fff;font-size:22px;line-height:1.22;letter-spacing:-.02em}.story-grid{display:grid;grid-template-columns:150px minmax(0,1fr);border-top:1px solid var(--grid2)}.field-label{padding:12px 12px 12px 0;color:var(--amber);font-size:10px;font-weight:900;letter-spacing:.08em;border-bottom:1px solid var(--grid2)}.story-dek,.analysis-copy{margin:0;padding:11px 0 12px 15px;border-left:1px solid var(--grid2);border-bottom:1px solid var(--grid2);color:#dce3dd;max-width:1100px}.story-dek{font-size:14px;font-weight:700;line-height:1.58}.analysis-copy{white-space:pre-line;color:#aebbb0;font-size:13px;line-height:1.72}.story-sources{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin-top:12px;color:#667268;font-size:10px}.story-sources>b{margin-right:5px;color:#667268}.story-sources a{border:1px solid #2f4f39;padding:4px 7px;color:var(--green);text-decoration:none}.story-sources a span{color:#78947e}.story-sources a:hover{background:#102116;color:#a4ffb9}.quiet-state{min-height:150px;padding:28px}.quiet-state h2{margin:0;font-size:22px}.hidden{display:none!important}
    @media(max-width:980px){.tape-columns,.tape-row{grid-template-columns:38px auto 54px minmax(110px,150px) minmax(280px,1fr)}.story-grid{grid-template-columns:125px 1fr}}
    @media(max-width:680px){body{font-size:12px}.identity-meta,.function-status{display:none}.workspace{padding:7px}.title-copy{padding:13px}.title-copy h1{font-size:22px}.tape-columns{display:none}.tape-row{grid-template-columns:34px auto 50px minmax(0,1fr);padding:7px 8px}.tape-company{display:none}.tape-headline{white-space:normal;line-height:1.3}.story-header{grid-template-columns:34px auto minmax(0,1fr)}.story-body{padding:13px}.story-body h2{font-size:18px}.story-grid{display:block}.field-label{padding:11px 0 5px;border-bottom:0}.story-dek,.analysis-copy{padding:0 0 13px;border-left:0}.analysis-copy{line-height:1.62}}
  </style>
</head>
<body>
  $test_banner
  <header class="terminal-head">
    <div class="identity-bar"><span class="brief-title">VENTURE DEBT + AI BRIEF</span><span class="identity-spacer"></span><span class="identity-meta">$date_label / PT</span></div>
    <nav class="function-bar" aria-label="Story filters">$filters<span class="function-status" id="visibleStatus">$story_count $story_word VISIBLE</span></nav>
  </header>
  <main class="workspace">
    <section class="screen-title"><div class="title-copy"><h1>$headline</h1></div></section>
    <section class="module">
      <div class="module-head"><span>$story_count $story_word</span></div>
      <div class="tape-columns"><span>#</span><span>LOGO</span><span>LANE</span><span>COMPANY</span><span>HEADLINE</span></div>
      <div id="storyTape">$tape</div>
    </section>
    <div class="feed" id="storyFeed">$feed</div>
    <div id="filterEmpty" class="market-note hidden"><span>FILTER</span>NO STORIES IN THIS SECTION.</div>
  </main>
  <script>
    (function(){
      var buttons=Array.prototype.slice.call(document.querySelectorAll('.command'));
      var sections=Array.prototype.slice.call(document.querySelectorAll('.story-section'));
      var rows=Array.prototype.slice.call(document.querySelectorAll('.tape-row'));
      var status=document.getElementById('visibleStatus');
      var empty=document.getElementById('filterEmpty');
      function applyFilter(lane){
        buttons.forEach(function(button){var active=button.getAttribute('data-filter')===lane;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));});
        var visible=0;
        sections.forEach(function(section){var show=lane==='all'||section.getAttribute('data-section')===lane;section.classList.toggle('hidden',!show);if(show){visible+=section.querySelectorAll('.story-panel').length;}});
        rows.forEach(function(row){row.classList.toggle('hidden',lane!=='all'&&row.getAttribute('data-lane')!==lane);});
        status.textContent=String(visible)+' '+(visible===1?'STORY':'STORIES')+' VISIBLE';
        empty.classList.toggle('hidden',visible!==0||lane==='all');
      }
      buttons.forEach(function(button){button.addEventListener('click',function(){applyFilter(button.getAttribute('data-filter'));});});
      document.addEventListener('keydown',function(event){if(event.ctrlKey||event.metaKey||event.altKey||/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)){return;}var button=buttons.find(function(item){return item.getAttribute('data-key')===event.key;});if(button){event.preventDefault();applyFilter(button.getAttribute('data-filter'));}});
    })();
  </script>
</body>
</html>"""


__all__ = ["render_dashboard"]
