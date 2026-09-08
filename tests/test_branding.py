from vdai.branding import build_brand_domains, normalize_logo_domain
from vdai.models import DigestSection, SourceConfig


def test_brand_registry_covers_source_ids_watchlist_names_and_aliases() -> None:
    source = SourceConfig(
        source_id="S054",
        section=DigestSection.AI_RADAR,
        organization="OpenAI",
        source_name="OpenAI News",
        url="https://openai.com/news/rss.xml",
        method="RSS",
        allowed_domains=("openai.com",),
        logo_domain="openai.com",
    )
    registry = build_brand_domains(
        [source],
        [
            {
                "company_id": "C001",
                "canonical_name": "SVB / First Citizens",
                "aliases": ("Silicon Valley Bank", "SVB"),
                "official_url": "https://www.svb.com/",
                "logo_domain": "svb.com",
            }
        ],
    )

    assert registry["source:s054"] == "openai.com"
    assert registry["openai"] == "openai.com"
    assert registry["silicon valley bank"] == "svb.com"
    assert registry["svb"] == "svb.com"
    assert registry["company:c001"] == "svb.com"


def test_logo_domains_reject_paths_and_non_https_urls() -> None:
    assert normalize_logo_domain("https://www.example.com/news") == "example.com"
    assert normalize_logo_domain("example.com/path") == ""
    assert normalize_logo_domain("http://example.com") == ""


def test_brand_registry_uses_the_source_host_when_logo_domain_is_blank() -> None:
    source = SourceConfig(
        source_id="S054",
        section=DigestSection.AI_RADAR,
        organization="OpenAI",
        source_name="OpenAI News",
        url="https://www.openai.com/news/rss.xml",
        method="RSS",
        allowed_domains=("openai.com",),
        logo_domain="",
    )

    registry = build_brand_domains([source], [])

    assert registry["openai"] == "openai.com"


def test_brand_registry_includes_verified_entity_domains() -> None:
    registry = build_brand_domains([], [])

    assert registry["dealer services network"] == "dsn.net"
    assert registry["kbra"] == "kbra.com"
    assert registry["j s held"] == "jsheld.com"
    assert registry["atlas"] == "atlas.mitre.org"
    assert registry["mitre atlas"] == "atlas.mitre.org"


def test_verified_entity_domain_overrides_a_discovery_publisher_host() -> None:
    source = SourceConfig(
        source_id="S999",
        section=DigestSection.COMPETITIVE_FIELD,
        organization="KBRA",
        source_name="Asset-Based Finance Journal",
        url="https://www.abfjournal.com/example",
        method="HTML",
        allowed_domains=("abfjournal.com",),
        logo_domain="abfjournal.com",
    )

    registry = build_brand_domains([source], [])

    assert registry["source:s999"] == "abfjournal.com"
    assert registry["kbra"] == "kbra.com"
