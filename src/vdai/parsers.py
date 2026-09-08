"""Deterministic parsers for the collector's supported source lanes."""

from __future__ import annotations

import calendar
import hashlib
import html
import json
import re
import textwrap
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import feedparser
from bs4 import BeautifulSoup, Tag
from dateutil import parser as date_parser
from lxml import etree

TRACKING_PARAMETERS = frozenset(
    {
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "ref",
        "source",
        "utm_campaign",
        "utm_content",
        "utm_medium",
        "utm_source",
        "utm_term",
    }
)
PROMPT_INJECTION_PATTERN = re.compile(
    r"(?:ignore\s+(?:all|any|the|your)?\s*(?:previous|prior|above)\s+instructions|"
    r"system\s+prompt|developer\s+message|call\s+(?:a\s+)?tool|"
    r"do\s+not\s+follow\s+your\s+instructions)",
    re.IGNORECASE,
)
UNSAFE_NAMED_ENTITY_PATTERN = re.compile(r"&([A-Za-z][A-Za-z0-9]+);")
XML_SAFE_ENTITIES = frozenset({"amp", "apos", "gt", "lt", "quot"})
DATE_HINT_PATTERN = re.compile(
    r"\b(?:(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}|"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\.?\s+\d{1,2}(?:st|nd|rd|th)?[,]?\s+(?:19|20)\d{2}|"
    r"\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|"
    r"Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|"
    r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?[,]?\s+(?:19|20)\d{2})\b",
    re.IGNORECASE,
)
ARTICLE_PATH_PATTERN = re.compile(
    r"/(?:blog|articles?|news|newsroom|press-releases?|posts|research)/[^/?#]+",
    re.IGNORECASE,
)
CARD_CTA_PATTERN = re.compile(
    r"\b(?:learn more|read more|read (?:the )?(?:blog )?post|view (?:the )?article)\b.*$",
    re.IGNORECASE,
)


class ParseError(ValueError):
    """Raised when a source payload cannot be parsed deterministically."""


@dataclass(frozen=True, slots=True)
class ParsedItem:
    title: str
    url: str
    item_id: str
    body_text: str
    summary_text: str = ""
    published_at: datetime | None = None
    native_ids: Mapping[str, str | None] = field(default_factory=dict)
    parse_warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class SitemapEntry:
    url: str
    last_modified: datetime | None = None
    is_sitemap: bool = False


def clean_unsafe_xml_entities(payload: str) -> str:
    """Neutralize K2-style undeclared HTML entities before strict XML parsing."""

    def replace(match: re.Match[str]) -> str:
        entity = match.group(1)
        if entity in XML_SAFE_ENTITIES:
            return match.group(0)
        if entity.lower() == "nbsp":
            return "&#160;"
        return " "

    return UNSAFE_NAMED_ENTITY_PATTERN.sub(replace, payload)


def canonicalize_url(raw_url: str, base_url: str) -> str:
    """Resolve a URL and remove fragments and known tracking parameters."""

    absolute = urljoin(base_url, raw_url.strip())
    parts = urlsplit(absolute)
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").rstrip(".").lower()
    if not scheme or not hostname:
        raise ParseError("URL is missing scheme or hostname")
    try:
        hostname = hostname.encode("idna").decode("ascii")
        port = parts.port
    except (UnicodeError, ValueError) as exc:
        raise ParseError("URL contains an invalid hostname or port") from exc
    if parts.username is not None or parts.password is not None:
        raise ParseError("Credentials in item URLs are forbidden")
    if port is not None and not (scheme == "https" and port == 443):
        netloc = f"{hostname}:{port}"
    else:
        netloc = hostname
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMETERS
    ]
    query.sort()
    return urlunsplit((scheme, netloc, path, urlencode(query, doseq=True), ""))


def revision_hash(title: str, body_text: str) -> str:
    """Hash the human-visible revision, not merely the stable source URL."""

    normalized_title = " ".join(title.split())
    normalized_body = "\n".join(line.strip() for line in body_text.splitlines() if line.strip())
    return hashlib.sha256(f"{normalized_title}\n{normalized_body}".encode()).hexdigest()


def candidate_key(source_id: str, item_id: str, content_hash: str) -> str:
    return hashlib.sha256(f"{source_id}|{item_id}|{content_hash}".encode()).hexdigest()


def _decode(payload: bytes | str) -> str:
    if isinstance(payload, str):
        return payload
    return payload.decode("utf-8", errors="replace")


def _parse_datetime(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        try:
            parsed = datetime.fromtimestamp(calendar.timegm(value), tz=UTC)
        except (OverflowError, TypeError, ValueError):
            return None
    else:
        try:
            parsed = date_parser.parse(str(value), fuzzy=False)
        except (OverflowError, TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _text_from_html(value: object) -> str:
    raw = str(value or "")
    if "<" not in raw and "&" not in raw:
        return " ".join(raw.split())
    soup = BeautifulSoup(raw, "lxml")
    blocked_tags = [
        "script",
        "style",
        "noscript",
        "template",
        "form",
        "iframe",
        "object",
        "embed",
    ]
    for tag in soup(blocked_tags):
        tag.decompose()
    return " ".join(soup.get_text(" ", strip=True).split())


def _warnings_for_text(text: str) -> tuple[str, ...]:
    return ("PROMPT_INJECTION_TEXT_PRESENT",) if PROMPT_INJECTION_PATTERN.search(text) else ()


def _first_value(mapping: Mapping[str, object], names: Iterable[str]) -> object:
    for name in names:
        value = mapping.get(name)
        if value not in (None, "", [], {}):
            return value
    return ""


def parse_rss_atom(
    payload: bytes | str,
    *,
    base_url: str,
    max_items: int,
) -> list[ParsedItem]:
    cleaned = clean_unsafe_xml_entities(_decode(payload))
    feed = feedparser.parse(cleaned)
    if feed.bozo and not feed.entries:
        raise ParseError(f"Malformed RSS/Atom payload: {feed.bozo_exception}")
    output: list[ParsedItem] = []
    for entry in feed.entries[:max_items]:
        title = _text_from_html(entry.get("title", ""))
        if not title:
            continue
        link = str(entry.get("link") or entry.get("id") or base_url)
        content_blocks = entry.get("content") or []
        content = content_blocks[0].get("value", "") if content_blocks else ""
        body = _text_from_html(content or entry.get("summary") or entry.get("description"))
        if not body:
            body = title
        item_id = str(entry.get("id") or entry.get("guid") or link or title)
        published = _parse_datetime(
            entry.get("published_parsed")
            or entry.get("updated_parsed")
            or entry.get("published")
            or entry.get("updated")
        )
        output.append(
            ParsedItem(
                title=title,
                url=canonicalize_url(link, base_url),
                item_id=item_id,
                body_text=body,
                summary_text=body[:500],
                published_at=published,
                native_ids={"guid": str(entry.get("id") or entry.get("guid") or "") or None},
                parse_warnings=_warnings_for_text(body),
            )
        )
    return output


def _sec_recent_records(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    filings = payload.get("filings")
    if not isinstance(filings, Mapping):
        return []
    recent = filings.get("recent")
    if not isinstance(recent, Mapping):
        return []
    columns = {key: value for key, value in recent.items() if isinstance(value, list)}
    row_count = max((len(value) for value in columns.values()), default=0)
    return [
        {key: values[index] if index < len(values) else None for key, values in columns.items()}
        for index in range(row_count)
    ]


def _json_records(payload: object) -> list[Mapping[str, object]]:
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    sec_records = _sec_recent_records(payload)
    if sec_records:
        return sec_records
    for key in (
        "items",
        "results",
        "data",
        "releases",
        "articles",
        "entries",
        "filings",
        "newsList",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            return [entry for entry in value if isinstance(entry, Mapping)]
        if isinstance(value, Mapping):
            nested = _json_records(value)
            if nested:
                return nested
    return [payload]


def _nested_mapping_value(
    mapping: Mapping[str, object], container: str, names: Iterable[str]
) -> object:
    nested = mapping.get(container)
    if not isinstance(nested, Mapping):
        return ""
    return _first_value(nested, names)


def parse_json_api(
    payload: bytes | str,
    *,
    base_url: str,
    max_items: int,
) -> list[ParsedItem]:
    try:
        decoded = json.loads(_decode(payload))
    except json.JSONDecodeError as exc:
        raise ParseError("Malformed JSON API payload") from exc
    records = _json_records(decoded)
    if (urlsplit(base_url).hostname or "").casefold() == "qwen.ai":
        records.sort(
            key=lambda record: (
                _parse_datetime(_nested_mapping_value(record, "extra", ("date", "published_at")))
                or datetime.min.replace(tzinfo=UTC)
            ),
            reverse=True,
        )
    output: list[ParsedItem] = []
    for record in records[:max_items]:
        title_value = _first_value(
            record,
            ("title", "headline", "name", "primaryDocument", "form", "description"),
        )
        title = _text_from_html(title_value)
        # Qwen's first-party article API nests a compact introduction and date
        # under ``extra`` while also returning multi-megabyte full HTML bodies.
        # Prefer the bounded editorial summary when it is available.
        raw_body = _nested_mapping_value(
            record,
            "extra",
            ("introduction", "description", "summary"),
        ) or _first_value(
            record,
            ("body_text", "body", "summary", "description", "abstract", "text", "content"),
        )
        body = _text_from_html(raw_body) or title
        raw_url_value = _first_value(record, ("canonical_url", "html_url", "url", "link", "href"))
        article_path = str(record.get("path") or "").strip()
        if not raw_url_value and article_path and "qwen.ai" in urlsplit(base_url).hostname:
            raw_url_value = f"/blog?id={quote(article_path)}"
        raw_url = str(raw_url_value or base_url)
        identifier = str(
            _first_value(
                record,
                ("id", "guid", "accessionNumber", "accession_number", "documentNumber"),
            )
            or raw_url
            or title
        )
        published = _parse_datetime(
            _first_value(
                record,
                (
                    "published_at",
                    "published",
                    "publishedAt",
                    "date",
                    "filingDate",
                    "acceptanceDateTime",
                    "created_at",
                    "updated_at",
                ),
            )
            or _nested_mapping_value(record, "extra", ("date", "published_at"))
        )
        output.append(
            ParsedItem(
                title=title,
                url=canonicalize_url(raw_url, base_url),
                item_id=identifier,
                body_text=body,
                summary_text=body[:500],
                published_at=published,
                native_ids={
                    "guid": str(record.get("guid") or "") or None,
                    "document_number": str(
                        record.get("documentNumber") or record.get("document_number") or ""
                    )
                    or None,
                    "accession_number": str(
                        record.get("accessionNumber") or record.get("accession_number") or ""
                    )
                    or None,
                },
                parse_warnings=_warnings_for_text(body),
            )
        )
    return output


def _html_item(node: Tag, base_url: str) -> ParsedItem | None:
    title_node = node.find(["h1", "h2", "h3", "h4"]) or node.find(
        attrs={"class": re.compile(r"title|headline", re.I)}
    )
    title = _text_from_html(title_node) if title_node else ""
    link_node = title_node.find("a", href=True) if isinstance(title_node, Tag) else None
    link_node = link_node or node.find("a", href=True)
    # Some newsrooms wrap the entire article card in an anchor. Without this,
    # every card collapses to the listing URL and cannot be cited or deduped.
    link_node = link_node or node.find_parent("a", href=True)
    raw_url = str(link_node.get("href")) if isinstance(link_node, Tag) else base_url
    body = _text_from_html(node)
    if not title:
        title = body[:160]
    if not title or not body:
        return None
    time_node = node.find("time")
    published_value = None
    if isinstance(time_node, Tag):
        published_value = time_node.get("datetime") or time_node.get_text(" ", strip=True)
    if not published_value:
        date_node = node.select_one(
            "[class*='date'],[class*='publish'],[class*='timestamp'],[data-date]"
        )
        if isinstance(date_node, Tag):
            date_text = str(
                date_node.get("datetime")
                or date_node.get("content")
                or date_node.get("data-date")
                or date_node.get_text(" ", strip=True)
            )
            date_match = DATE_HINT_PATTERN.search(date_text)
            published_value = date_match.group(0) if date_match else None
    if not published_value:
        date_match = DATE_HINT_PATTERN.search(body)
        published_value = date_match.group(0) if date_match else None
    try:
        canonical = canonicalize_url(raw_url, base_url)
    except ParseError:
        # A malformed navigation or contact card must not poison an otherwise valid
        # newsroom page. The collector still rejects unsafe URLs; it simply skips the
        # bad card and continues parsing source-scoped article links.
        return None
    return ParsedItem(
        title=title,
        url=canonical,
        item_id=str(node.get("data-id") or canonical or title),
        body_text=body,
        summary_text=body[:500],
        published_at=_parse_datetime(published_value),
        parse_warnings=_warnings_for_text(body),
    )


def _document_published_at(soup: BeautifulSoup) -> datetime | None:
    selectors = (
        "meta[property='article:published_time']",
        "meta[name='datePublished']",
        "meta[name='date_published']",
        "meta[name='publish-date']",
        "meta[itemprop='datePublished']",
    )
    for selector in selectors:
        node = soup.select_one(selector)
        if isinstance(node, Tag):
            parsed = _parse_datetime(node.get("content") or node.get("datetime"))
            if parsed is not None:
                return parsed
    for node in soup.select("script[type='application/ld+json']"):
        try:
            decoded = json.loads(node.string or node.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        queue = [decoded]
        while queue:
            current = queue.pop(0)
            if isinstance(current, Mapping):
                parsed = _parse_datetime(current.get("datePublished"))
                if parsed is not None:
                    return parsed
                queue.extend(current.values())
            elif isinstance(current, list):
                queue.extend(current)
    # Some first-party deal pages expose the announcement date only in the
    # rendered article body. Keep the fallback scoped to the article/main
    # content so footer copyright years and unrelated navigation do not win.
    body = soup.find("article") or soup.find("main")
    if isinstance(body, Tag):
        time_node = body.find("time")
        if isinstance(time_node, Tag):
            parsed = _parse_datetime(
                time_node.get("datetime") or time_node.get_text(" ", strip=True)
            )
            if parsed is not None:
                return parsed
        date_node = body.select_one(
            "[class*='date'],[class*='publish'],[class*='timestamp'],[data-date]"
        )
        if isinstance(date_node, Tag):
            date_text = str(
                date_node.get("datetime")
                or date_node.get("content")
                or date_node.get("data-date")
                or date_node.get_text(" ", strip=True)
            )
            date_match = DATE_HINT_PATTERN.search(date_text)
            if date_match:
                return _parse_datetime(date_match.group(0))
        date_match = DATE_HINT_PATTERN.search(_text_from_html(body))
        if date_match:
            return _parse_datetime(date_match.group(0))
    return None


def _clean_card_title(text: str) -> str:
    cleaned = DATE_HINT_PATTERN.sub(" ", text)
    cleaned = CARD_CTA_PATTERN.sub(" ", cleaned)
    cleaned = re.sub(r"\b(?:press release|featured)\b", " ", cleaned, flags=re.IGNORECASE)
    return " ".join(cleaned.split()).strip(" -|:")[:300]


def _link_card_items(soup: BeautifulSoup, base_url: str) -> list[ParsedItem]:
    """Extract modern newsroom cards that are implemented as plain anchors."""

    base_host = (urlsplit(base_url).hostname or "").casefold()
    ranked: dict[str, tuple[int, int, ParsedItem]] = {}
    order = 0
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href:
            continue
        try:
            canonical = canonicalize_url(href, base_url)
        except ParseError:
            continue
        parts = urlsplit(canonical)
        if (parts.hostname or "").casefold() != base_host:
            continue
        if not ARTICLE_PATH_PATTERN.search(parts.path):
            continue
        text = _text_from_html(anchor)
        container: Tag | None = None
        if len(text) < 15:
            # Webflow and similar CMS templates often render an empty overlay
            # anchor beside the visible title/date. Walk only to the nearest
            # card-like ancestor so the link can still be paired with its own
            # metadata without absorbing unrelated page text.
            for parent in anchor.parents:
                if not isinstance(parent, Tag) or parent.name in {"body", "html"}:
                    break
                classes = " ".join(str(value) for value in parent.get("class", ()))
                card_like = bool(
                    parent.get("role") == "listitem"
                    or re.search(r"(?:card|item|slide|resource|featured)", classes, re.I)
                    or parent.find(attrs={"data-title": True})
                )
                if not card_like:
                    continue
                parent_text = _text_from_html(parent)
                if len(parent_text) >= 15 or parent.find(attrs={"data-title": True}):
                    container = parent
                    text = parent_text
                    break
        if len(text) < 15:
            continue
        title_node = anchor.find(["h1", "h2", "h3", "h4"])
        if title_node is None and container is not None:
            title_node = container.find(["h1", "h2", "h3", "h4"])
        data_node = container.find(attrs={"data-title": True}) if container is not None else None
        data_title = str(data_node.get("data-title") or "") if isinstance(data_node, Tag) else ""
        title = data_title or (
            _text_from_html(title_node) if isinstance(title_node, Tag) else ""
        )
        title = title or _clean_card_title(text)
        if len(title) < 8:
            continue
        date_value = ""
        if container is not None:
            date_node = container.find(
                attrs={
                    "data-date-start": True,
                }
            ) or container.find(attrs={"data-date-published": True})
            if isinstance(date_node, Tag):
                date_value = str(
                    date_node.get("data-date-start")
                    or date_node.get("data-date-published")
                    or ""
                )
        date_match = DATE_HINT_PATTERN.search(date_value or text)
        published = _parse_datetime(date_match.group(0)) if date_match else None
        body = text[:5_000]
        item = ParsedItem(
            title=title,
            url=canonical,
            item_id=canonical,
            body_text=body,
            summary_text=body[:500],
            published_at=published,
            parse_warnings=_warnings_for_text(body),
        )
        score = (10_000 if published else 0) + (2_000 if title_node else 0) + min(len(text), 1_000)
        existing = ranked.get(canonical)
        if existing is None:
            ranked[canonical] = (order, score, item)
            order += 1
        elif score > existing[1]:
            ranked[canonical] = (existing[0], score, item)
    return [entry[2] for entry in sorted(ranked.values(), key=lambda entry: entry[0])]


def _router_data_items(raw_html: str, base_url: str) -> list[ParsedItem]:
    """Read ByteDance Seed's server-rendered article registry without executing JS."""

    match = re.search(
        r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>",
        raw_html,
        flags=re.DOTALL,
    )
    if not match:
        return []
    try:
        decoded = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    queue: list[object] = [decoded]
    records: list[object] = []
    while queue:
        current = queue.pop(0)
        if isinstance(current, Mapping):
            article_list = current.get("article_list")
            if isinstance(article_list, list):
                records = article_list
                break
            queue.extend(current.values())
        elif isinstance(current, list):
            queue.extend(current)
    output: list[ParsedItem] = []
    language = "zh" if "/zh/" in urlsplit(base_url).path else "en"
    content_key = "ArticleSubContentZh" if language == "zh" else "ArticleSubContentEn"
    for record in records:
        if not isinstance(record, Mapping):
            continue
        meta = record.get("ArticleMeta")
        content = record.get(content_key)
        if not isinstance(meta, Mapping) or not isinstance(content, Mapping):
            continue
        title = _text_from_html(content.get("Title"))
        slug = str(content.get("TitleKey") or "").strip()
        if not title or not slug:
            continue
        abstract = _text_from_html(content.get("Abstract")) or title
        published_value = meta.get("PublishDate")
        published = None
        if isinstance(published_value, (int, float)):
            try:
                published = datetime.fromtimestamp(published_value / 1_000, tz=UTC)
            except (OSError, OverflowError, ValueError):
                published = None
        canonical = canonicalize_url(f"/{language}/blog/{quote(slug)}", base_url)
        output.append(
            ParsedItem(
                title=title,
                url=canonical,
                item_id=str(meta.get("ArticleID") or canonical),
                body_text=abstract,
                summary_text=abstract[:500],
                published_at=published,
                native_ids={"article_id": str(meta.get("ArticleID") or "") or None},
                parse_warnings=_warnings_for_text(abstract),
            )
        )
    return output


def parse_html_document(payload: bytes | str, *, base_url: str) -> ParsedItem | None:
    """Parse the current page itself, including document-level publish metadata."""

    soup = BeautifulSoup(_decode(payload), "lxml")
    published = _document_published_at(soup)
    title_node = (
        soup.select_one("meta[property='og:title']")
        or soup.select_one("meta[name='twitter:title']")
        or soup.find("h1")
        or soup.find("title")
    )
    if isinstance(title_node, Tag) and title_node.name == "meta":
        title = _text_from_html(title_node.get("content"))
    else:
        title = _text_from_html(title_node)
    canonical_node = soup.select_one("link[rel='canonical']") or soup.select_one(
        "meta[property='og:url']"
    )
    raw_url = base_url
    if isinstance(canonical_node, Tag):
        raw_url = str(canonical_node.get("href") or canonical_node.get("content") or base_url)
    blocked_tags = [
        "script",
        "style",
        "noscript",
        "template",
        "form",
        "iframe",
        "object",
        "embed",
    ]
    for tag in soup(blocked_tags):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body
    body = _text_from_html(main)
    if not body:
        description_node = soup.select_one("meta[property='og:description']") or soup.select_one(
            "meta[name='description']"
        )
        if isinstance(description_node, Tag):
            body = _text_from_html(description_node.get("content"))
    # Some current Next.js article pages render an empty <main> server-side but
    # still expose trustworthy article dates and titles in Open Graph metadata.
    # The listing card already carries the body used by hydration, so retaining
    # the title here is sufficient to bring the date across safely.
    body = body or title
    if not title or not body:
        return None
    canonical = canonicalize_url(raw_url, base_url)
    return ParsedItem(
        title=title,
        url=canonical,
        item_id=canonical,
        body_text=body,
        summary_text=body[:500],
        published_at=published,
        parse_warnings=_warnings_for_text(body),
    )


def parse_html(
    payload: bytes | str,
    *,
    base_url: str,
    max_items: int,
) -> list[ParsedItem]:
    raw_html = _decode(payload)
    router_items = _router_data_items(raw_html, base_url)
    if router_items:
        return router_items[:max_items]
    soup = BeautifulSoup(raw_html, "lxml")
    document_published = _document_published_at(soup)
    blocked_tags = [
        "script",
        "style",
        "noscript",
        "template",
        "form",
        "iframe",
        "object",
        "embed",
    ]
    for tag in soup(blocked_tags):
        tag.decompose()
    selectors = (
        "article",
        "[class*='press-release']",
        "[class*='news-card']",
        "[class*='post-card']",
        "[class*='release-card']",
        ".cards__card",
        ".article-short-card",
        ".caption",
        "li[class*='CardContent'][data-variants]",
        "li[class*='CardFeature']",
    )
    nodes: list[Tag] = []
    seen: set[int] = set()
    for selector in selectors:
        for selected in soup.select(selector):
            if id(selected) not in seen:
                seen.add(id(selected))
                nodes.append(selected)
    output = [item for current in nodes if (item := _html_item(current, base_url))]
    output.extend(_link_card_items(soup, base_url))
    if not output:
        main = soup.find("main") or soup.find("article") or soup.body or soup
        if isinstance(main, Tag):
            page_item = _html_item(main, base_url)
            if page_item:
                output.append(page_item)
    if document_published is not None and len(output) == 1:
        output[0] = replace(output[0], published_at=document_published)
    ranked: dict[str, tuple[int, int, ParsedItem]] = {}
    for order, item in enumerate(output):
        generic_title = item.title.strip().casefold() in {
            "news",
            "newsroom",
            "blog",
            "articles",
        }
        score = (
            (10_000 if item.published_at else 0)
            + (0 if generic_title else 5_000)
            + min(len(item.title), 1_000)
        )
        existing = ranked.get(item.url)
        if existing is None:
            ranked[item.url] = (order, score, item)
        elif score > existing[1]:
            ranked[item.url] = (existing[0], score, item)
    deduped = [entry[2] for entry in sorted(ranked.values(), key=lambda entry: entry[0])]
    return deduped[:max_items]


def parse_sitemap(
    payload: bytes | str,
    *,
    base_url: str,
    max_items: int,
) -> list[SitemapEntry]:
    text = clean_unsafe_xml_entities(_decode(payload))
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ParseError("Sitemap DTDs and entities are forbidden")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
    try:
        root = etree.fromstring(text.encode(), parser=parser)
    except etree.XMLSyntaxError as exc:
        raise ParseError("Malformed sitemap XML") from exc
    output: list[SitemapEntry] = []
    for element in root.iter():
        entry_type = etree.QName(element).localname.lower()
        if entry_type not in {"url", "sitemap"}:
            continue
        location = ""
        last_modified: datetime | None = None
        for child in element:
            local_name = etree.QName(child).localname.lower()
            if local_name == "loc":
                location = "".join(child.itertext()).strip()
            elif local_name == "lastmod":
                last_modified = _parse_datetime("".join(child.itertext()).strip())
        if location:
            output.append(
                SitemapEntry(
                    url=canonicalize_url(html.unescape(location), base_url),
                    last_modified=last_modified,
                    is_sitemap=entry_type == "sitemap",
                )
            )
        if len(output) >= max_items:
            break
    return output


def parse_markdown(
    payload: bytes | str,
    *,
    base_url: str,
    max_items: int,
) -> list[ParsedItem]:
    text = textwrap.dedent(_decode(payload).replace("\r\n", "\n"))
    headings = list(re.finditer(r"(?m)^(#{1,4})\s+(.+?)\s*$", text))
    if not headings:
        body = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        if not body:
            return []
        return [
            ParsedItem(
                title=body.splitlines()[0][:160],
                url=canonicalize_url(base_url, base_url),
                item_id=base_url,
                body_text=body,
                summary_text=body[:500],
                parse_warnings=_warnings_for_text(body),
            )
        ]
    output: list[ParsedItem] = []
    for index, heading in enumerate(headings[:max_items]):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        title = heading.group(2).strip()
        section = text[heading.end() : end].strip()
        body = re.sub(r"[`*_>#-]", "", section)
        body = "\n".join(line.strip() for line in body.splitlines() if line.strip())
        link_match = re.search(r"\[[^\]]+\]\(([^)]+)\)", section)
        raw_url = link_match.group(1).strip() if link_match else base_url
        date_match = DATE_HINT_PATTERN.search(f"{title} {section}")
        published = _parse_datetime(date_match.group(0)) if date_match else None
        canonical = canonicalize_url(raw_url, base_url)
        output.append(
            ParsedItem(
                title=title,
                url=canonical,
                item_id=canonical if raw_url != base_url else f"{base_url}#{index}",
                body_text=body or title,
                summary_text=(body or title)[:500],
                published_at=published,
                parse_warnings=_warnings_for_text(body),
            )
        )
    return output


def parse_lane(
    lane: str,
    payload: bytes | str,
    *,
    base_url: str,
    max_items: int,
) -> list[ParsedItem]:
    normalized_lane = lane.strip().lower().replace("/", "_").replace("-", "_")
    if normalized_lane in {"rss", "atom", "rss_atom", "feed"}:
        return parse_rss_atom(payload, base_url=base_url, max_items=max_items)
    if normalized_lane in {"api", "json", "json_api"}:
        return parse_json_api(payload, base_url=base_url, max_items=max_items)
    if normalized_lane in {"html", "web_page", "html_email"}:
        return parse_html(payload, base_url=base_url, max_items=max_items)
    if normalized_lane in {"markdown", "md"}:
        return parse_markdown(payload, base_url=base_url, max_items=max_items)
    raise ParseError(f"Unsupported parser lane: {lane}")
