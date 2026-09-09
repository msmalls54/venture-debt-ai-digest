from __future__ import annotations

from datetime import UTC, date, datetime

from bs4 import BeautifulSoup

from vdai.models import Citation, DigestSection, Edition, Story
from vdai.renderer import render_digest


def _edition() -> Edition:
    return Edition(
        run_id="edition-run",
        edition_date=date(2026, 8, 25),
        subject="OpenAI tightens the model-cost race",
        stories=(
            Story(
                published_story_id="edition-run:event-1:ai_radar:1",
                candidate_id="candidate-1",
                raw_event_id="event-1",
                section=DigestSection.AI_RADAR,
                rank=1,
                headline="OpenAI — Model X lands",
                dek="The official release puts pricing pressure back on the table.",
                why_it_matters="Lower model costs can change startup burn and runway.",
                company="OpenAI",
                confidence=95,
                fact_ids=("f1",),
                source_ids=("S054",),
                citations=(
                    Citation(
                        source_id="S054",
                        url="https://openai.com/index/model-x/?a=1&b=2",
                        publisher="OpenAI News",
                        fact_ids=("f1",),
                    ),
                ),
            ),
        ),
        word_count=34,
        generated_at=datetime(2026, 8, 25, 10, tzinfo=UTC),
    )


def test_renderer_accepts_strict_edition_and_builds_terminal_email() -> None:
    rendered = render_digest(_edition())

    assert rendered.subject == "OpenAI tightens the model-cost race"
    assert '<table role="presentation"' in rendered.html
    assert "@media only screen and (max-width:720px)" in rendered.html
    assert ".vd-shell table{width:100%!important" not in rendered.html
    assert "table-layout:fixed!important" not in rendered.html
    assert (
        ".vd-logo-group{width:auto!important;max-width:none!important;"
        "table-layout:auto!important}"
    ) in rendered.html
    assert 'class="vd-tape-row"' in rendered.html
    assert 'class="vd-story-head"' in rendered.html
    assert 'class="vd-story-fields"' in rendered.html
    assert '<table align="center" role="presentation" width="920"' in rendered.html
    assert "width:92%;max-width:920px;margin:0 auto;text-align:left" in rendered.html
    assert 'class="vd-logo-cell" width="76"' in rendered.html
    assert 'class="vd-logo-cell" width="84"' in rendered.html
    assert "overflow-wrap:anywhere!important" in rendered.html
    assert 'class="vd-date"' in rendered.html
    assert "background:#050706" in rendered.html
    assert "background:#090d0b" in rendered.html
    assert '<meta name="color-scheme" content="dark only">' in rendered.html
    assert 'bgcolor="#050706"' in rendered.html
    assert 'bgcolor="#090d0b"' in rendered.html
    assert "VENTURE DEBT + AI BRIEF" in rendered.html
    assert "linear-gradient(#050706,#050706)" in rendered.html
    assert "color:#ff9f1c!important" in rendered.html
    assert rendered.html.count("OpenAI — Model X lands") == 2
    assert "a=1&amp;b=2" in rendered.html
    assert "1 STORY" in rendered.html
    assert "1 STORY" in rendered.text
    assert ">1.</td>" in rendered.html
    assert '<span class="story-index">1.</span>' in rendered.browser_html
    assert ">01</td>" not in rendered.html
    assert '<span class="story-index">01</span>' not in rendered.browser_html
    assert "AI INTELLIGENCE" in rendered.text
    assert "No new venture-debt transaction" not in rendered.text
    assert "WHAT HAPPENED" not in rendered.text
    assert "COMMENTARY" in rendered.text
    assert "https://openai.com/index/model-x/?a=1&b=2" in rendered.text
    assert "www.google.com/s2/favicons" in rendered.html
    assert 'alt="OpenAI logo"' in rendered.html
    assert "ANALYST COMMENTARY" in rendered.html
    assert "font-size:14px;line-height:22px" in rendered.html
    assert "color:#dce3dd!important" in rendered.html
    assert "color:#aebbb0!important" in rendered.html
    assert '<body class="vd-root" bgcolor="#050706" text="#e7eee8"' in rendered.html
    assert '<font color="#ffffff">' not in rendered.html
    assert "-webkit-text-fill-color:#ffffff!important" in rendered.html
    assert "color:#4ee7ff!important" in rendered.html
    assert "color:#6cff8f!important" in rendered.html
    assert "background:#ece9df" not in rendered.html
    assert "background:#f7f5ee" not in rendered.html
    assert "background:#eef0f2" not in rendered.html
    assert "VENTURE DEBT + AI BRIEF" in rendered.browser_html
    assert "ANALYST COMMENTARY" in rendered.browser_html
    assert "Lower model costs can change startup burn and runway." in rendered.browser_html
    assert 'data-lane="ai"' in rendered.browser_html
    assert "a=1&amp;b=2" in rendered.browser_html
    assert "www.google.com/s2/favicons" in rendered.browser_html
    assert 'aria-label="OpenAI logo"' in rendered.browser_html
    assert "OA</i>" in rendered.browser_html
    assert "tape-logo" in rendered.browser_html
    assert 'data-key="4"' in rendered.browser_html
    assert "verified" not in rendered.html.casefold()
    assert "verified" not in rendered.text.casefold()
    assert "verified" not in rendered.browser_html.casefold()
    for noise in (
        "public-source intelligence",
        "run state",
        "sources healthy",
        "jobs healthy",
        "active lanes",
        "raw candidates",
        "delivery mode",
        "no new venture-debt",
    ):
        assert noise not in rendered.html.casefold()
        assert noise not in rendered.browser_html.casefold()


def test_headlines_have_gmail_inversion_layers_without_touching_logos() -> None:
    rendered = render_digest(_edition())
    soup = BeautifulSoup(rendered.html, "html.parser")
    protected = soup.select(".gmail-blend-screen > .gmail-blend-difference")
    assert [node.get_text() for node in protected] == [
        _edition().subject, _edition().stories[0].headline, _edition().stories[0].headline
    ]
    assert all(node.find("img") is None for node in protected)
    selector = "u + .vd-root .gmail-blend-screen{background:#000;mix-blend-mode:screen}"
    assert selector in rendered.html
    assert "mix-blend-mode:difference" in rendered.html
    assert "font-size:16px;line-height:23px" in rendered.html


def test_renderer_uses_editorial_section_labels_and_lists_every_story_at_a_glance() -> None:
    sections = (
        ("lead", "Lead headline", "TOP STORY"),
        ("deal_tape", "Debt headline", "VENTURE DEBT"),
        ("competitive_field", "Bank headline", "BANKS + DEPOSITS"),
        ("ai_radar", "AI headline", "AI INTELLIGENCE"),
        ("runway_watch", "Funding headline", "VENTURE CAPITAL"),
    )
    digest = {
        "edition_date": "2026-08-25",
        "edition": {"headline": "Five material stories"},
        "validated_stories": [
            {
                "section": section,
                "headline": headline,
                "dek": "A concise, material lead.",
                "why_it_matters": "Full model commentary remains present.",
                "citations": [],
            }
            for section, headline, _ in sections
        ],
    }

    rendered = render_digest(digest)

    assert "5 STORIES" in rendered.html
    assert "5 STORIES" in rendered.text
    assert "1. Lead headline" in rendered.text
    assert "5. Funding headline" in rendered.text
    for _, headline, label in sections:
        assert rendered.html.count(headline) == 2
        assert rendered.text.count(headline) == 2
        assert label.replace("&", "&amp;") in rendered.html
        assert label in rendered.text


def test_renderer_escapes_copy_and_marks_test_output_without_network_state() -> None:
    digest = {
        "edition_date": "2026-08-25",
        "edition": {
            "kicker": "VENTURE DEBT + AI",
            "headline": "OpenAI <script>alert(1)</script>",
            "deck": 'Quoted "copy" & context',
        },
        "validated_stories": [],
    }

    rendered = render_digest(digest, test_mode=True)

    assert rendered.subject.startswith("[TEST] ")
    assert "<script>" not in rendered.html
    assert "&lt;script&gt;" in rendered.html
    assert "Quoted &quot;copy&quot; &amp; context" not in rendered.html
    assert "TEST RENDER · EMAIL DELIVERY DISABLED" in rendered.html
    assert "TEST RENDER" in rendered.text
    assert "0 STORIES" in rendered.html
    assert "No stories today." in rendered.text
    assert "No material stories today." in rendered.text
    assert "NO MATERIAL STORIES TODAY" in rendered.browser_html
    assert "TEST PREVIEW / DELIVERY DISABLED" in rendered.browser_html


def test_browser_logo_registry_overrides_discovery_publisher_domain() -> None:
    digest = {
        "edition_date": "2026-08-25",
        "edition": {"headline": "One material story"},
        "validated_stories": [
            {
                "section": "ai_radar",
                "company": "OpenAI",
                "headline": "OpenAI — Model launch",
                "dek": "A material release.",
                "why_it_matters": "Full commentary.",
                "citations": [
                    {
                        "source_id": "S999",
                        "publisher": "Google News",
                        "url": "https://news.google.com/rss/articles/example",
                    }
                ],
            }
        ],
    }

    rendered = render_digest(digest, brand_domains={"openai": "openai.com"})

    assert "https%3A%2F%2Fopenai.com" in rendered.html
    assert "https%3A%2F%2Fnews.google.com" not in rendered.html
    assert "https%3A%2F%2Fopenai.com" in rendered.browser_html
    assert "https%3A%2F%2Fnews.google.com" not in rendered.browser_html


def test_renderer_shows_every_supported_company_logo_for_multi_company_story() -> None:
    digest = {
        "edition_date": "2026-08-25",
        "edition": {"headline": "One material story"},
        "validated_stories": [
            {
                "section": "competitive_field",
                "company": "Kelvion",
                "entity_names": ["Kelvion", "SLB"],
                "headline": "Kelvion — SLB agrees to acquire the company",
                "dek": "Kelvion and SLB reached an agreement.",
                "why_it_matters": "The transaction expands SLB's data-center exposure.",
                "citations": [
                    {
                        "source_id": "S999",
                        "publisher": "Kelvion",
                        "url": "https://www.kelvion.com/news/example",
                    }
                ],
            }
        ],
    }

    rendered = render_digest(
        digest,
        brand_domains={"kelvion": "kelvion.com", "slb": "slb.com"},
    )

    assert 'alt="Kelvion logo"' in rendered.html
    assert 'alt="SLB logo"' in rendered.html
    assert rendered.html.count('class="vd-logo-group"') == 2
    assert 'class="vd-logo-group" role="presentation"' in rendered.html
    assert (
        'style="width:auto;max-width:none;table-layout:auto;'
        'border-collapse:separate;"'
    ) in rendered.html
    assert ".vd-shell table{width:100%!important" not in rendered.html
    assert 'aria-label="Kelvion logo"' in rendered.browser_html
    assert 'aria-label="SLB logo"' in rendered.browser_html
    assert "https%3A%2F%2Fkelvion.com" in rendered.html
    assert "https%3A%2F%2Fslb.com" in rendered.html


def test_renderer_can_mark_an_explicit_full_update_subject() -> None:
    rendered = render_digest(_edition(), subject_prefix="[FULL UPDATE] ")

    assert rendered.subject == "[FULL UPDATE] OpenAI tightens the model-cost race"
    assert "[FULL UPDATE]" not in rendered.html
