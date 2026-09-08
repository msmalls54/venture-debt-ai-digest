# ruff: noqa: E501
"""Stage and selectively activate the 2026-09-02 source expansion.

The script is deliberately fail-closed:

* every new source is written inactive unless its ID is explicitly passed to
  ``--activate``;
* the live Sources header must exactly match the audited contract;
* existing populated IDs outside this expansion are never rewritten;
* the complete write is read back before the command reports success.

It never runs collection, calls a model, changes recipients, or sends email.
"""

from __future__ import annotations

import argparse
import json
from urllib.parse import quote_plus

import gspread

from vdai.config import AppSettings

HEADERS = (
    "source_id",
    "section",
    "organization",
    "source_name",
    "url",
    "method",
    "parser",
    "cadence",
    "source_tier",
    "priority",
    "active",
    "region",
    "notes",
    "allowed_domains",
    "primary_source",
    "discovery_only",
    "headers_profile",
    "max_items",
    "max_pages",
    "max_bytes",
    "parser_version",
    "logo_domain",
)


def source_row(
    source_id: str,
    section: str,
    organization: str,
    source_name: str,
    url: str,
    method: str,
    parser: str,
    cadence: str,
    source_tier: str,
    priority: str,
    region: str,
    notes: str,
    allowed_domains: str,
    *,
    primary_source: bool,
    discovery_only: bool,
    headers_profile: str = "STANDARD",
    max_items: int = 20,
    max_pages: int = 1,
    max_bytes: int = 2_000_000,
) -> list[str]:
    values = [
        source_id,
        section,
        organization,
        source_name,
        url,
        method,
        parser,
        cadence,
        source_tier,
        priority,
        "FALSE",
        region,
        notes,
        allowed_domains,
        "TRUE" if primary_source else "FALSE",
        "TRUE" if discovery_only else "FALSE",
        headers_profile,
        str(max_items),
        str(max_pages),
        str(max_bytes),
        "candidate.v1",
        "",
    ]
    if len(values) != len(HEADERS):
        raise ValueError(f"{source_id} has an invalid source contract")
    return values


def official(
    source_id: str,
    section: str,
    organization: str,
    source_name: str,
    url: str,
    method: str,
    parser: str,
    cadence: str,
    region: str,
    notes: str,
    allowed_domains: str,
    *,
    priority: str = "P1",
    source_tier: str = "3-primary",
    headers_profile: str = "STANDARD",
    max_items: int = 20,
    max_pages: int = 1,
    max_bytes: int = 2_000_000,
) -> list[str]:
    return source_row(
        source_id,
        section,
        organization,
        source_name,
        url,
        method,
        parser,
        cadence,
        source_tier,
        priority,
        region,
        notes,
        allowed_domains,
        primary_source=True,
        discovery_only=False,
        headers_profile=headers_profile,
        max_items=max_items,
        max_pages=max_pages,
        max_bytes=max_bytes,
    )


def discovery(
    source_id: str,
    section: str,
    organization: str,
    query: str,
    region: str,
    notes: str,
    *,
    priority: str = "P2",
    lookback: str = "30d",
) -> list[str]:
    complete_query = f"({query}) when:{lookback}"
    url = (
        "https://news.google.com/rss/search?q="
        f"{quote_plus(complete_query)}&hl=en-US&gl=US&ceid=US%3Aen"
    )
    return source_row(
        source_id,
        section,
        organization,
        "Targeted Google News discovery",
        url,
        "RSS",
        "RSS/Atom",
        "Daily",
        "2-discovery",
        priority,
        region,
        notes + "; discovery only and requires primary-source corroboration",
        "news.google.com",
        primary_source=False,
        discovery_only=True,
        max_items=20,
        max_pages=1,
        max_bytes=1_000_000,
    )


ROWS: dict[str, list[str]] = {}


def add(values: list[str]) -> None:
    source_id = values[0]
    if source_id in ROWS:
        raise ValueError(f"duplicate staged source: {source_id}")
    ROWS[source_id] = values


# Deposit competitors and the partner-bank infrastructure behind them.
add(
    official(
        "S130",
        "Deposit Watch",
        "Grasshopper Bank",
        "Official press releases",
        "https://www.grasshopper.bank/press/",
        "HTML",
        "Article cards",
        "Every 4 hours",
        "US",
        "Startup banking, treasury, deposits, lending, partnerships and acquisitions",
        "grasshopper.bank",
        priority="P0",
        max_items=30,
    )
)
add(
    official(
        "S131",
        "Deposit Watch",
        "Meow",
        "Official blog",
        "https://www.meow.com/blog",
        "HTML",
        "Article cards",
        "Every 4 hours",
        "US",
        "Startup checking, T-bills, treasury, partner banks and agentic finance",
        "meow.com",
        priority="P0",
        max_items=30,
    )
)
add(
    official(
        "S132",
        "Deposit Watch",
        "Bluevine",
        "Official product updates",
        "https://www.bluevine.com/blog/category/bluevine-product-updates",
        "HTML",
        "Article cards",
        "Daily",
        "US",
        "Business checking, yield, treasury, payments and account controls",
        "bluevine.com",
        priority="P1",
        max_items=30,
    )
)
add(
    official(
        "S133",
        "Competitive Field",
        "Cross River",
        "Official company news",
        "https://www.crossriver.com/category/company-news",
        "HTML",
        "Article cards",
        "Daily",
        "US",
        "Embedded finance, partner-bank infrastructure, lending, payments and acquisitions",
        "crossriver.com",
        priority="P0",
        max_items=30,
    )
)
add(
    official(
        "S134",
        "Competitive Field",
        "Lead Bank",
        "Official blog and releases",
        "https://www.lead.bank/blog",
        "HTML",
        "Article cards",
        "Daily",
        "US",
        "Fintech banking, embedded finance, payments, stablecoins and acquisitions",
        "lead.bank",
        priority="P0",
        max_items=30,
    )
)

DEPOSIT_DISCOVERY = (
    (
        "S135",
        "Deposit Watch",
        "Brex",
        'Brex (banking OR treasury OR deposits OR acquisition OR partnership OR "cash management")',
        "US",
        "Brex product, bank-partner and deposit competition",
    ),
    (
        "S136",
        "Deposit Watch",
        "Mercury",
        'Mercury (banking OR treasury OR deposits OR acquisition OR partnership OR "cash management")',
        "US",
        "Mercury product, partner-bank and deposit competition",
    ),
    (
        "S137",
        "Deposit Watch",
        "Ramp",
        'Ramp (treasury OR banking OR deposits OR acquisition OR partnership OR "cash management")',
        "US",
        "Ramp treasury, spend, banking and platform expansion",
    ),
    (
        "S138",
        "Deposit Watch",
        "Revolut Business",
        "Revolut (business OR bank OR banking OR deposits OR treasury OR acquisition)",
        "Global",
        "Revolut business banking, charter, deposit and acquisition activity",
    ),
    (
        "S139",
        "Deposit Watch",
        "Chime",
        "Chime (bank OR banking OR deposits OR acquisition OR partnership)",
        "US",
        "Chime banking, deposit and partner-bank developments",
    ),
    (
        "S140",
        "Deposit Watch",
        "Wise Business",
        '"Wise Business" (interest OR banking OR treasury OR deposits OR cash)',
        "Global",
        "Wise business cash, interest and banking features",
    ),
    (
        "S141",
        "Deposit Watch",
        "Relay",
        'Relay ("business banking" OR treasury OR deposits OR acquisition OR partnership)',
        "US",
        "Relay business banking and cash-management expansion",
    ),
    (
        "S142",
        "Deposit Watch",
        "Novo",
        'Novo ("business banking" OR treasury OR deposits OR fintech OR acquisition)',
        "US",
        "Novo business banking and fintech platform changes",
    ),
    (
        "S143",
        "Deposit Watch",
        "NorthOne",
        'NorthOne ("business banking" OR treasury OR deposits OR acquisition OR partnership)',
        "US",
        "NorthOne business banking and bank-partner developments",
    ),
    (
        "S144",
        "Competitive Field",
        "Column",
        "Column (bank OR banking OR fintech OR payments OR acquisition OR partnership)",
        "US",
        "Fintech-bank infrastructure and platform partnerships",
    ),
    (
        "S145",
        "Competitive Field",
        "Coastal Community Bank",
        '"Coastal Community Bank" (fintech OR BaaS OR banking OR partnership OR acquisition)',
        "US",
        "Partner-bank, BaaS and fintech program developments",
    ),
    (
        "S146",
        "Competitive Field",
        "Evolve Bank & Trust",
        '"Evolve Bank" (fintech OR BaaS OR banking OR enforcement OR partnership OR acquisition)',
        "US",
        "Partner-bank, compliance and fintech-program developments",
    ),
    (
        "S147",
        "Competitive Field",
        "Treasury Prime",
        '"Treasury Prime" (bank OR banking OR fintech OR partnership OR acquisition)',
        "US",
        "Embedded-banking platform and bank-network changes",
    ),
    (
        "S148",
        "Competitive Field",
        "Unit",
        'Unit ("banking as a service" OR fintech OR bank partnership OR acquisition)',
        "US",
        "Embedded-finance infrastructure and partner-bank developments",
    ),
    (
        "S149",
        "Deposit Watch",
        "Stripe Treasury",
        '"Stripe Treasury" OR (Stripe "financial accounts") OR (Stripe banking)',
        "Global",
        "Embedded financial accounts and treasury product changes",
    ),
    (
        "S150",
        "Deposit Watch",
        "Square Banking",
        '"Square Banking" OR (Block business banking) OR (Square savings)',
        "US",
        "SMB banking, lending and deposit competition",
    ),
    (
        "S151",
        "Deposit Watch",
        "Shopify Balance",
        '"Shopify Balance" OR (Shopify business banking) OR (Shopify cash management)',
        "Global",
        "Merchant cash, banking and treasury product changes",
    ),
    (
        "S152",
        "Deposit Watch",
        "Arc",
        "Arc (startup banking OR treasury OR venture debt OR credit facility OR financing)",
        "US",
        "Startup cash management and financing platform activity",
    ),
    (
        "S153",
        "Deposit Watch",
        "Treasure Financial",
        '"Treasure Financial" (treasury OR cash OR banking OR acquisition OR partnership)',
        "US",
        "Startup and SMB treasury-management developments",
    ),
    (
        "S154",
        "Deposit Watch",
        "Vesto",
        'Vesto (treasury OR "cash management" OR fintech OR banking)',
        "US",
        "Treasury platform and cash-management developments",
    ),
    (
        "S155",
        "Competitive Field",
        "Modern Treasury",
        '"Modern Treasury" (payments OR banking OR acquisition OR partnership OR financing)',
        "US",
        "Payments infrastructure and bank-connectivity developments",
    ),
    (
        "S156",
        "Competitive Field",
        "Plaid",
        "Plaid (banking OR payments OR acquisition OR partnership OR deposits OR fintech)",
        "US",
        "Financial-data infrastructure and bank/fintech strategy",
    ),
    (
        "S157",
        "Deposit Watch",
        "Slash",
        'Slash ("business banking" OR deposits OR fintech OR treasury OR partnership)',
        "US",
        "Startup and business banking competition",
    ),
    (
        "S158",
        "Competitive Field",
        "Enova / Grasshopper",
        "Enova Grasshopper (acquisition OR bank OR merger OR approval OR closing)",
        "US",
        "Enova-Grasshopper transaction status and integration",
    ),
)
for values in DEPOSIT_DISCOVERY:
    add(discovery(*values, priority="P1", lookback="180d"))


# Venture debt, growth credit, private credit and adjacent non-dilutive finance.
add(
    official(
        "S159",
        "Deal Tape",
        "Western Technology Investment",
        "Official SEC submissions",
        "https://data.sec.gov/submissions/CIK0001850938.json",
        "API",
        "JSON",
        "Daily",
        "US",
        "WTI venture-loan portfolio, commitments and regulatory filings",
        "data.sec.gov;www.sec.gov",
        priority="P0",
        source_tier="3-regulatory",
        headers_profile="SEC",
        max_items=30,
    )
)
add(
    official(
        "S160",
        "Deal Tape",
        "ORIX Growth Capital",
        "Official growth-capital news",
        "https://www.orix.com/news-insights/growth-capital/",
        "HTML",
        "Article cards",
        "Daily",
        "North America",
        "Growth lending, private-credit investments and portfolio financings",
        "orix.com",
        priority="P0",
        max_items=30,
    )
)
add(
    official(
        "S161",
        "Deal Tape",
        "Flow Capital",
        "Official newsroom",
        "https://www.flowcap.com/learn/newsroom",
        "HTML",
        "Article cards",
        "Daily",
        "North America",
        "Venture debt, growth capital, follow-ons, repayments and portfolio exits",
        "flowcap.com",
        priority="P0",
        max_items=40,
    )
)

LENDER_DISCOVERY = (
    (
        "S162",
        "Structural Capital",
        '"Structural Capital" (venture debt OR credit facility OR growth debt OR financing)',
        "US",
        "Venture-debt and growth-credit transactions",
    ),
    (
        "S163",
        "National Bank of Canada Technology Banking",
        '"National Bank" (technology banking OR innovation banking OR venture debt OR credit facility)',
        "Canada",
        "Canadian technology-banking and venture-credit activity",
    ),
    (
        "S164",
        "Deutsche Bank Innovation Economy",
        '"Deutsche Bank" (innovation economy OR venture debt OR startup banking OR technology banking)',
        "Global",
        "Innovation-economy banking and credit strategy",
    ),
    (
        "S165",
        "Citi Innovation / Technology Banking",
        "Citi (startup banking OR innovation banking OR technology banking OR venture debt)",
        "Global",
        "Citi startup, technology and innovation-economy banking",
    ),
    (
        "S166",
        "Wells Fargo Technology Banking",
        '"Wells Fargo" (technology banking OR venture debt OR innovation banking OR startup credit)',
        "US",
        "Technology-banking and growth-credit activity",
    ),
    (
        "S167",
        "Scotiabank Technology Banking",
        "Scotiabank (technology banking OR innovation banking OR venture debt OR credit facility)",
        "Canada",
        "Canadian technology and innovation banking",
    ),
    (
        "S170",
        "Barings",
        "Barings (private credit OR direct lending OR credit facility) (technology OR software OR healthcare)",
        "Global",
        "Private-credit platform and technology/healthcare financings",
    ),
    (
        "S171",
        "Blackstone Credit",
        '"Blackstone Credit" (financing OR debt OR direct lending OR credit facility) (technology OR software OR AI)',
        "Global",
        "Large growth-credit and technology financings",
    ),
    (
        "S172",
        "Oaktree Capital",
        "Oaktree (private credit OR debt financing OR direct lending) (technology OR software OR healthcare)",
        "Global",
        "Private-credit and special-situations activity",
    ),
    (
        "S173",
        "Madryn Asset Management",
        '"Madryn Asset Management" (credit OR debt OR financing OR facility)',
        "US",
        "Life-sciences and healthcare growth-credit deals",
    ),
    (
        "S174",
        "Perceptive Advisors",
        '"Perceptive Advisors" (credit facility OR debt financing OR venture debt)',
        "US",
        "Life-sciences and healthcare credit activity",
    ),
)
for source_id, organization, query, region, notes in LENDER_DISCOVERY:
    add(
        discovery(
            source_id,
            "Deal Tape",
            organization,
            query,
            region,
            notes,
            priority="P1",
            lookback="180d",
        )
    )

add(
    official(
        "S168",
        "Deal Tape",
        "Capital Southwest",
        "Official investor news RSS",
        "https://ir.capitalsouthwest.com/rss/news-releases.xml",
        "RSS",
        "RSS/Atom",
        "Daily",
        "US",
        "Portfolio investments, credit facilities and private-credit platform developments",
        "ir.capitalsouthwest.com;capitalsouthwest.com",
        priority="P1",
        max_items=30,
    )
)
add(
    official(
        "S169",
        "Deal Tape",
        "Ares Capital",
        "Official investor news RSS",
        "https://www.arescapitalcorp.com/rss/news-releases.xml",
        "RSS",
        "RSS/Atom",
        "Daily",
        "US",
        "Direct-lending, portfolio and private-credit developments",
        "arescapitalcorp.com",
        priority="P1",
        max_items=30,
    )
)
add(
    official(
        "S175",
        "Deal Tape",
        "Capchase",
        "Official blog",
        "https://www.capchase.com/blog",
        "HTML",
        "Article cards",
        "Daily",
        "Global",
        "Non-dilutive SaaS financing, lending products and capital partnerships",
        "capchase.com",
        priority="P1",
        max_items=30,
    )
)
add(
    official(
        "S176",
        "Deal Tape",
        "Founderpath",
        "Official blog",
        "https://founderpath.com/blog",
        "HTML",
        "Article cards",
        "Daily",
        "US",
        "SaaS financing, founder liquidity and non-dilutive capital",
        "founderpath.com",
        priority="P1",
        max_items=30,
    )
)
add(
    official(
        "S177",
        "Deal Tape",
        "Clearco",
        "Official newsroom",
        "https://clear.co/newsroom",
        "HTML",
        "Article cards",
        "Daily",
        "Global",
        "Ecommerce growth capital, credit products and platform strategy",
        "clear.co",
        priority="P1",
        max_items=30,
    )
)
add(
    official(
        "S178",
        "Deal Tape",
        "Pipe",
        "Official company news",
        "https://pipe.com/blog",
        "HTML",
        "Article cards",
        "Daily",
        "Global",
        "Embedded capital, financing products and platform partnerships",
        "pipe.com",
        priority="P1",
        max_items=30,
    )
)
add(
    official(
        "S179",
        "Deal Tape",
        "Wayflyer",
        "Official news",
        "https://wayflyer.com/news",
        "HTML",
        "Article cards",
        "Daily",
        "Global",
        "Revenue-based financing, credit facilities and ecommerce lending",
        "wayflyer.com",
        priority="P1",
        max_items=30,
    )
)
add(
    discovery(
        "S180",
        "Deal Tape",
        "Uncapped",
        "Uncapped (growth finance OR revenue based financing OR credit facility OR debt)",
        "Global",
        "Growth-finance and non-dilutive funding activity",
        priority="P1",
        lookback="180d",
    )
)
add(
    discovery(
        "S181",
        "Deal Tape",
        "Liquidity Capital",
        '"Liquidity Capital" (growth debt OR credit OR financing OR investment)',
        "Global",
        "Technology growth-credit and portfolio financings",
        priority="P1",
        lookback="180d",
    )
)
add(
    official(
        "S182",
        "Runway Watch",
        "Canadian Business Growth Fund",
        "Official news",
        "https://cbgf.com/news/",
        "HTML",
        "Article cards",
        "Daily",
        "Canada",
        "Growth-capital investments, exits and portfolio developments",
        "cbgf.com",
        priority="P1",
        max_items=30,
    )
)
add(
    official(
        "S183",
        "Competitive Field",
        "European Investment Fund",
        "Official news",
        "https://www.eif.org/what_we_do/news/index.htm",
        "HTML",
        "Article cards",
        "Daily",
        "Europe",
        "Fund guarantees, venture financing programs and lender partnerships",
        "eif.org",
        priority="P1",
        max_items=30,
    )
)
add(
    official(
        "S184",
        "Competitive Field",
        "EBRD",
        "Official news",
        "https://www.ebrd.com/home/news-and-events/news.html",
        "HTML",
        "Article cards",
        "Daily",
        "Europe",
        "Innovation financing, fintech investment and growth-capital programs",
        "ebrd.com",
        priority="P1",
        max_items=30,
    )
)


# AI releases, infrastructure, evaluations and cybersecurity.
add(
    official(
        "S185",
        "AI Radar",
        "Cohere",
        "Official release notes",
        "https://docs.cohere.com/v2/changelog",
        "HTML",
        "HTML page",
        "Every 4 hours",
        "Global",
        "Model, API, pricing and platform releases",
        "docs.cohere.com;cohere.com",
        priority="P0",
        max_items=30,
    )
)
add(
    official(
        "S186",
        "AI Radar",
        "Together AI",
        "Official changelog",
        "https://docs.together.ai/docs/changelog",
        "HTML",
        "HTML page",
        "Every 4 hours",
        "Global",
        "Model availability, pricing, deprecations and inference-platform changes",
        "docs.together.ai;together.ai",
        priority="P0",
        max_items=40,
    )
)
add(
    official(
        "S187",
        "AI Radar",
        "Groq",
        "Official changelog",
        "https://console.groq.com/docs/changelog",
        "HTML",
        "HTML page",
        "Every 4 hours",
        "Global",
        "Inference models, APIs, pricing and platform changes",
        "console.groq.com;groq.com",
        priority="P0",
        max_items=30,
    )
)
add(
    official(
        "S188",
        "AI Radar",
        "Moonshot AI / Kimi",
        "Official research blog",
        "https://www.kimi.com/en/blog/",
        "HTML",
        "Article cards",
        "Every 4 hours",
        "Global",
        "Kimi model, agent, benchmark and research releases",
        "kimi.com",
        priority="P0",
        max_items=30,
    )
)
add(
    official(
        "S189",
        "AI Radar",
        "Alibaba Qwen",
        "Official model-release changelog",
        "https://docs.qwencloud.com/changelog/models",
        "HTML",
        "HTML page",
        "Every 4 hours",
        "Global",
        "Qwen model snapshots, open weights, APIs and capabilities",
        "docs.qwencloud.com;qwencloud.com",
        priority="P0",
        max_items=40,
    )
)
add(
    official(
        "S190",
        "AI Radar",
        "NVIDIA",
        "Official developer blog RSS",
        "https://developer.nvidia.com/blog/feed/",
        "RSS",
        "RSS/Atom",
        "Every 4 hours",
        "Global",
        "AI models, inference, agents, security, chips and developer-platform releases",
        "developer.nvidia.com;nvidia.com",
        priority="P0",
        max_items=30,
    )
)
add(
    official(
        "S191",
        "AI Radar",
        "Cloudflare",
        "Official AI blog RSS",
        "https://blog.cloudflare.com/tag/ai/rss/",
        "RSS",
        "RSS/Atom",
        "Every 4 hours",
        "Global",
        "Workers AI, inference, model serving, agents and security",
        "blog.cloudflare.com;cloudflare.com",
        priority="P1",
        max_items=30,
    )
)
add(
    official(
        "S192",
        "AI Radar",
        "Cursor",
        "Official changelog",
        "https://cursor.com/changelog",
        "HTML",
        "Article cards",
        "Every 4 hours",
        "Global",
        "Coding-agent models, features, pricing and enterprise controls",
        "cursor.com",
        priority="P1",
        max_items=30,
    )
)
add(
    official(
        "S193",
        "AI Radar",
        "Windsurf",
        "Official changelog",
        "https://windsurf.com/changelog",
        "HTML",
        "Article cards",
        "Every 4 hours",
        "Global",
        "Coding-agent models, product releases and enterprise controls",
        "windsurf.com",
        priority="P1",
        max_items=30,
    )
)
add(
    official(
        "S194",
        "AI Radar",
        "Replicate",
        "Official changelog",
        "https://replicate.com/changelog",
        "HTML",
        "Article cards",
        "Every 4 hours",
        "Global",
        "Hosted models, APIs, pricing and deployment changes",
        "replicate.com",
        priority="P1",
        max_items=30,
    )
)
add(
    official(
        "S195",
        "AI Radar",
        "Fireworks AI",
        "Official changelog",
        "https://docs.fireworks.ai/changelog",
        "HTML",
        "HTML page",
        "Every 4 hours",
        "Global",
        "Inference, fine-tuning, model and API releases",
        "docs.fireworks.ai;fireworks.ai",
        priority="P1",
        max_items=30,
    )
)
add(
    official(
        "S196",
        "AI Radar",
        "Baseten",
        "Official changelog",
        "https://www.baseten.co/changelog/",
        "HTML",
        "Article cards",
        "Every 4 hours",
        "Global",
        "Model deployment, inference and platform releases",
        "baseten.co",
        priority="P1",
        max_items=30,
    )
)
add(
    official(
        "S197",
        "AI Radar",
        "Stability AI",
        "Official news",
        "https://stability.ai/news",
        "HTML",
        "Article cards",
        "Every 4 hours",
        "Global",
        "Image, audio, video and foundation-model releases",
        "stability.ai",
        priority="P1",
        max_items=30,
    )
)
add(
    official(
        "S198",
        "AI Radar",
        "Black Forest Labs",
        "Official news",
        "https://bfl.ai/news",
        "HTML",
        "Article cards",
        "Every 4 hours",
        "Global",
        "FLUX model and image-generation platform releases",
        "bfl.ai",
        priority="P1",
        max_items=30,
    )
)
add(
    official(
        "S199",
        "AI Radar",
        "ElevenLabs",
        "Official blog",
        "https://elevenlabs.io/blog",
        "HTML",
        "Article cards",
        "Every 4 hours",
        "Global",
        "Speech, audio, agent and multimodal releases",
        "elevenlabs.io",
        priority="P1",
        max_items=30,
    )
)
add(
    official(
        "S200",
        "AI Radar",
        "NIST CAISI",
        "Official news and assessments",
        "https://www.nist.gov/caisi",
        "HTML",
        "Article cards",
        "Daily",
        "US",
        "Frontier-model evaluations, cyber-capability assessments and agent security standards",
        "nist.gov",
        priority="P0",
        source_tier="3-regulatory",
        max_items=30,
    )
)
add(
    official(
        "S201",
        "AI Radar",
        "UK AI Security Institute",
        "Official blog",
        "https://www.aisi.gov.uk/blog",
        "HTML",
        "Article cards",
        "Daily",
        "UK",
        "Frontier-model evaluations, AI security and capability research",
        "aisi.gov.uk",
        priority="P0",
        source_tier="3-regulatory",
        max_items=30,
    )
)
add(
    official(
        "S202",
        "AI Radar",
        "OWASP GenAI Security Project",
        "Official project feed",
        "https://genai.owasp.org/feed/",
        "RSS",
        "RSS/Atom",
        "Daily",
        "Global",
        "LLM, agent, model and application security guidance",
        "genai.owasp.org;owasp.org",
        priority="P1",
        max_items=30,
    )
)
add(
    official(
        "S203",
        "AI Radar",
        "MITRE ATLAS",
        "Official GitHub releases",
        "https://github.com/mitre-atlas/atlas-data/releases.atom",
        "RSS",
        "RSS/Atom",
        "Daily",
        "Global",
        "Adversarial AI techniques, mitigations and knowledge-base releases",
        "github.com",
        priority="P1",
        max_items=30,
    )
)
add(
    official(
        "S204",
        "AI Radar",
        "Perplexity",
        "Official changelog",
        "https://docs.perplexity.ai/changelog",
        "HTML",
        "HTML page",
        "Every 4 hours",
        "Global",
        "Search, model, API and agent product releases",
        "docs.perplexity.ai;perplexity.ai",
        priority="P1",
        max_items=30,
    )
)
add(
    official(
        "S205",
        "AI Radar",
        "Hugging Face",
        "Official blog feed",
        "https://huggingface.co/blog/feed.xml",
        "RSS",
        "RSS/Atom",
        "Every 4 hours",
        "Global",
        "Official open-model, inference and platform announcements",
        "huggingface.co",
        priority="P1",
        max_items=30,
    )
)
add(
    official(
        "S206",
        "AI Radar",
        "Allen Institute for AI",
        "Official blog feed",
        "https://allenai.org/blog/rss.xml",
        "RSS",
        "RSS/Atom",
        "Daily",
        "US",
        "Open models, datasets, evaluations and research releases",
        "allenai.org",
        priority="P1",
        max_items=30,
    )
)
add(
    discovery(
        "S207",
        "AI Radar",
        "Microsoft Security",
        "site:microsoft.com/security/blog (AI OR agent OR Copilot) (security OR threat OR vulnerability)",
        "Global",
        "AI-security research and defensive product developments",
        priority="P1",
        lookback="30d",
    )
)
add(
    discovery(
        "S208",
        "AI Radar",
        "Google Threat Intelligence",
        "site:cloud.google.com/blog/topics/threat-intelligence (AI OR Gemini OR model) (security OR threat OR attack)",
        "Global",
        "AI-enabled cyber threats and model-security research",
        priority="P1",
        lookback="30d",
    )
)
add(
    official(
        "S209",
        "AI Radar",
        "Apollo Research",
        "Official research",
        "https://www.apolloresearch.ai/research",
        "HTML",
        "Article cards",
        "Daily",
        "Global",
        "Frontier-model scheming, agent behavior and safety evaluations",
        "apolloresearch.ai",
        priority="P1",
        max_items=30,
    )
)


# Funding discovery, private-credit trades and regulatory changes.
add(
    discovery(
        "S210",
        "Runway Watch",
        "Crunchbase News",
        "site:news.crunchbase.com (funding OR raises OR acquisition OR venture) (startup OR AI)",
        "Global",
        "Notable startup rounds, acquisitions and financing context",
        priority="P1",
        lookback="7d",
    )
)
add(
    source_row(
        "S211",
        "Runway Watch",
        "FinSMEs",
        "Funding news RSS",
        "https://www.finsmes.com/feed",
        "RSS",
        "RSS/Atom",
        "Every 4 hours",
        "2-discovery",
        "P1",
        "Global",
        "Startup funding, venture debt and acquisition discovery; requires primary corroboration",
        "finsmes.com",
        primary_source=False,
        discovery_only=True,
        max_items=40,
    )
)
add(
    source_row(
        "S212",
        "Runway Watch",
        "TechCrunch Venture",
        "Venture RSS",
        "https://techcrunch.com/category/venture/feed/",
        "RSS",
        "RSS/Atom",
        "Every 4 hours",
        "2-discovery",
        "P1",
        "Global",
        "Venture rounds, investors, acquisitions and market context; requires primary corroboration",
        "techcrunch.com",
        primary_source=False,
        discovery_only=True,
        max_items=30,
    )
)
add(
    discovery(
        "S213",
        "Runway Watch",
        "Sifted",
        "site:sifted.eu (raises OR funding OR acquisition OR venture debt)",
        "Europe",
        "European startup rounds, acquisitions and venture-debt discovery",
        priority="P1",
        lookback="7d",
    )
)
add(
    source_row(
        "S214",
        "Runway Watch",
        "Tech.eu",
        "Official news feed",
        "https://tech.eu/feed/",
        "RSS",
        "RSS/Atom",
        "Every 4 hours",
        "2-discovery",
        "P1",
        "Europe",
        "European technology funding and acquisition discovery; requires primary corroboration",
        "tech.eu",
        primary_source=False,
        discovery_only=True,
        max_items=30,
    )
)
add(
    source_row(
        "S215",
        "Runway Watch",
        "EU-Startups",
        "Official news feed",
        "https://www.eu-startups.com/feed/",
        "RSS",
        "RSS/Atom",
        "Every 4 hours",
        "2-discovery",
        "P1",
        "Europe",
        "European startup funding and acquisition discovery; requires primary corroboration",
        "eu-startups.com",
        primary_source=False,
        discovery_only=True,
        max_items=30,
    )
)
add(
    source_row(
        "S216",
        "Runway Watch",
        "Silicon Canals",
        "Official news feed",
        "https://siliconcanals.com/feed/",
        "RSS",
        "RSS/Atom",
        "Every 4 hours",
        "2-discovery",
        "P1",
        "Europe",
        "European startup funding, fintech and acquisition discovery; requires primary corroboration",
        "siliconcanals.com",
        primary_source=False,
        discovery_only=True,
        max_items=30,
    )
)
add(
    source_row(
        "S217",
        "Competitive Field",
        "Private Credit Daily",
        "Private-credit news",
        "https://www.privatecreditdaily.com/",
        "HTML",
        "Article cards",
        "Daily",
        "2-discovery",
        "P1",
        "US",
        "Private-credit deals, funds, hires and lender strategy; requires primary corroboration",
        "privatecreditdaily.com",
        primary_source=False,
        discovery_only=True,
        max_items=30,
    )
)
add(
    source_row(
        "S218",
        "Deal Tape",
        "ABF Journal",
        "Asset-based finance RSS",
        "https://www.abfjournal.com/feed/",
        "RSS",
        "RSS/Atom",
        "Daily",
        "2-discovery",
        "P1",
        "US",
        "Asset-based lending, specialty finance and lender transactions; requires primary corroboration",
        "abfjournal.com",
        primary_source=False,
        discovery_only=True,
        max_items=30,
    )
)
add(
    official(
        "S219",
        "Competitive Field",
        "FTC",
        "Competition press-release RSS",
        "https://www.ftc.gov/feeds/press-release-competition.xml",
        "RSS",
        "RSS/Atom",
        "Daily",
        "US",
        "Competition enforcement, merger challenges and fintech-market actions",
        "ftc.gov",
        priority="P1",
        source_tier="3-regulatory",
        max_items=30,
    )
)
add(
    official(
        "S220",
        "Competitive Field",
        "DOJ Antitrust Division",
        "Official antitrust news RSS",
        "https://www.justice.gov/news/rss?type%5B0%5D=press_release&field_component=376&search_api_language=en&show_public_archived=0&require_all=0",
        "RSS",
        "RSS/Atom",
        "Daily",
        "US",
        "Antitrust cases, merger enforcement and competition policy",
        "justice.gov",
        priority="P1",
        source_tier="3-regulatory",
        max_items=30,
    )
)
add(
    official(
        "S221",
        "Competitive Field",
        "CFPB",
        "Official newsroom RSS",
        "https://www.consumerfinance.gov/about-us/newsroom/feed/",
        "RSS",
        "RSS/Atom",
        "Daily",
        "US",
        "Fintech, payments, banking and nonbank regulatory changes",
        "consumerfinance.gov",
        priority="P1",
        source_tier="3-regulatory",
        max_items=30,
    )
)
add(
    discovery(
        "S222",
        "Competitive Field",
        "New York DFS",
        "site:dfs.ny.gov (fintech OR bank OR banking OR merger OR acquisition OR enforcement)",
        "US",
        "New York banking, fintech, charter and enforcement developments",
        priority="P1",
        lookback="30d",
    )
)
add(
    discovery(
        "S223",
        "Competitive Field",
        "California DFPI",
        "site:dfpi.ca.gov (fintech OR bank OR banking OR merger OR acquisition OR enforcement)",
        "US",
        "California fintech, banking, lending and enforcement developments",
        priority="P1",
        lookback="30d",
    )
)
add(
    source_row(
        "S224",
        "Competitive Field",
        "Banking Dive",
        "Banking industry RSS",
        "https://www.bankingdive.com/feeds/news/",
        "RSS",
        "RSS/Atom",
        "Daily",
        "2-discovery",
        "P1",
        "US",
        "Bank acquisitions, fintech partnerships, deposits and strategy; requires primary corroboration",
        "bankingdive.com",
        primary_source=False,
        discovery_only=True,
        max_items=30,
    )
)


if tuple(sorted(ROWS)) != tuple(f"S{value:03d}" for value in range(130, 225)):
    raise RuntimeError("source expansion must contain every ID from S130 through S224")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--activate", nargs="*", default=[])
    args = parser.parse_args()
    activation = set(args.activate)
    unknown = activation - set(ROWS)
    if unknown:
        raise RuntimeError(f"unknown activation IDs: {sorted(unknown)}")

    settings = AppSettings()
    client = gspread.service_account_from_dict(settings.service_account_info())
    spreadsheet = client.open_by_key(settings.google_sheet_id)
    worksheet = spreadsheet.worksheet("Sources")
    values = worksheet.get_all_values()
    if not values or tuple(values[0]) != HEADERS:
        raise RuntimeError("Sources headers changed; refusing to write")

    existing = {
        current[0]: (index, current)
        for index, current in enumerate(values[1:], start=2)
        if current and str(current[0]).strip()
    }
    conflicts: list[str] = []
    for source_id, expected in ROWS.items():
        if source_id not in existing:
            continue
        actual = list(existing[source_id][1])
        actual.extend([""] * (len(HEADERS) - len(actual)))
        # Reruns are allowed only for rows created by this exact expansion. The
        # activation checkbox is intentionally excluded so a tested subset can
        # be promoted without weakening any other source field.
        if actual[:10] + actual[11 : len(HEADERS)] != list(expected[:10]) + list(expected[11:]):
            conflicts.append(source_id)
    if conflicts:
        raise RuntimeError(f"source IDs contain unexpected data: {conflicts[:3]}")

    first_row = 131
    last_row = 225
    if worksheet.row_count < last_row:
        worksheet.add_rows(last_row - worksheet.row_count)

    # Preserve the established visual format and checkbox/data-validation rules.
    requests = []
    for paste_type in ("PASTE_FORMAT", "PASTE_DATA_VALIDATION"):
        requests.append(
            {
                "copyPaste": {
                    "source": {
                        "sheetId": worksheet.id,
                        "startRowIndex": 100,
                        "endRowIndex": 101,
                        "startColumnIndex": 0,
                        "endColumnIndex": len(HEADERS),
                    },
                    "destination": {
                        "sheetId": worksheet.id,
                        "startRowIndex": first_row - 1,
                        "endRowIndex": last_row,
                        "startColumnIndex": 0,
                        "endColumnIndex": len(HEADERS),
                    },
                    "pasteType": paste_type,
                }
            }
        )
    spreadsheet.batch_update({"requests": requests})

    output_rows: list[list[str]] = []
    for value in range(130, 225):
        source_id = f"S{value:03d}"
        output = list(ROWS[source_id])
        if source_id in activation:
            output[10] = "TRUE"
        output_rows.append(output)
    worksheet.update(
        range_name=f"A{first_row}:V{last_row}",
        values=output_rows,
        value_input_option="USER_ENTERED",
    )

    readback = worksheet.get(f"A{first_row}:V{last_row}")
    if len(readback) != len(output_rows):
        raise RuntimeError("source expansion readback was incomplete")
    for expected, actual in zip(output_rows, readback, strict=True):
        if actual[:11] != expected[:11] or actual[13:17] != expected[13:17]:
            raise RuntimeError(f"source expansion readback mismatch: {expected[0]}")

    print(
        json.dumps(
            {
                "status": "SOURCE_EXPANSION_STAGED",
                "configured": len(output_rows),
                "activated": sorted(activation),
                "kept_inactive": len(output_rows) - len(activation),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
