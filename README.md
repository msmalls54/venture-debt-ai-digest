# Venture Debt + AI Digest

Railway is the only intended live runtime. The legacy n8n workflows must remain unable to
send and are retained only as a rollback reference. This service is one finite Railway cron
job; it uses the existing Google Sheet as its registry and durable operating record, OpenRouter
for evidence analysis and drafting, and AgentMail for private-BCC delivery.

No database, queue, dashboard, vector store, agent framework, or second live service is
required.

## Launch shape

The current production snapshot has **133 active sources** spanning venture debt, banks and
deposits, AI labs and infrastructure, and venture-capital context. Failed, quarantined, noisy,
and lower-signal registry rows remain preserved but inactive.

The 133 active rows currently expand to 137 collection jobs because targeted sources such as
SEC source `S003` expand across active watched companies. Inactive watchlist rows never produce
jobs or ranking hits. The 2026-09-07 full run completed all 137 jobs successfully. Treat these
numbers as a dated production snapshot; the Google Sheet remains the live source of truth.

Before evidence analysis, the worker removes candidates already known by durable ID or content
hash and rejects source-dated archive items older than the current send window. It allows at
most two candidates per source and per source job, removes duplicate canonical URLs and content
hashes, and prefers watched-company hits, first-party sources, higher priority, and recent
publication. Candidate selection rotates across the three operating lanes and has a hard
ceiling of **60 evidence candidates per run**, even if a larger Sheet or environment value is
configured. The evidence pass preserves source-backed market intelligence for the final editor
instead of requiring every item to be a completed transaction. Validated model drops are
versioned and recorded so unchanged pages do not repeatedly consume the review budget, while
older-policy drops and transient model failures remain eligible for a bounded retry.

## Schedule and weekend behavior

Railway is configured with:

```text
0 14,15 * * *
```

Those two UTC invocations cover Pacific daylight and standard time. For a normal scheduled
`run`, the process first checks the configured local timezone. Only the invocation that lands in
the 7:00 AM Pacific hour proceeds; the other exits before reading the Sheet or opening network
clients.

The surviving invocation collects every calendar day, including Saturday and Sunday. Editing
and scheduled delivery occurs only Monday, Wednesday, and Friday inside the configured 7:00 AM
local window. Tuesday, Thursday, and weekend events are written to the Sheet and remain eligible
for the next scheduled edition.

An edition considers verified events since the last successful send, capped at eight days. If
there is no prior successful send, it uses a 96-hour bootstrap window. Published-story history
is applied again before selection, so a weekend story can appear Monday without repeating a
story that was already sent.

## Local setup

Python 3.12 is required. Keep real secrets in a local `.env` file or Railway variables; never
commit them.

```text
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
copy .env.example .env
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m ruff check src tests
```

Fill the copied `.env` with your own Google Sheet ID, Google service-account JSON, OpenRouter
key, AgentMail key, and AgentMail inbox. The example keeps the worker and delivery disabled.

## Commands

```text
vdai-digest validate
vdai-digest validate --offline
vdai-digest fetch-canary
vdai-digest fetch-canary --source-id S003
vdai-digest collect --dry-run
vdai-digest run --dry-run --force-editor --preview-dir artifacts/preview
vdai-digest render-canary
vdai-digest run
vdai-digest send-once
```

`fetch-canary` is the source-boundary check for the current active registry. It reads the source
registry and watchlist, expands every active source, and performs real source fetches. It does
**not** call either model, write to the Sheet, or send email. It returns a secret-safe receipt
for every job and fails on network/parser errors, empty results, undated streams, and collapsed
multi-story feeds that cannot support distinct citations. `--source-id` may be repeated and can
test an inactive registry row before activation.

`run --dry-run --force-editor` is the model-powered no-send preview path. It writes the
email-safe HTML, plain text, and a responsive `digest-dashboard.html` browser interface to
`--preview-dir`. The browser interface uses the same validated edition and citations but is
designed for a full desktop or mobile screen instead of email-client width. Dry-run mode does
not write health, event, edition, story, or send records to the Sheet and cannot send email.

`send-once` is the idempotent first-delivery path. It bypasses only the weekday and time-window
restriction. It still requires valid credentials, an enabled worker, healthy sources, a valid
edition with at least one verified story, valid recipients, both live-delivery switches, test
mode off in Railway and the Sheet, the final pre-send recheck, and duplicate-send protection.
Re-running it for an already-sent deterministic send ID returns `ALREADY_SENT` rather than
creating another email.

If delivery fails after an edition becomes ready, the exact rendered edition remains persisted
in `Digest Runs` and is reused on retry. The worker never silently regenerates different copy
under the same send ID.

Secrets are Railway variables. They are never stored in the Google Sheet, logs, fixtures, or
model prompts.

## Editorial format

Each published story is rendered in this order:

1. A `Company — Event` headline.
2. A prominent one-to-three-sentence factual explanation directly below the headline.
3. **Commentary:** the full evidence-supported implications, context, uncertainty,
   and what to watch next. Multiple paragraphs are allowed when they add value.
4. Approved HTTPS citations added by deterministic code.

The email opens with an `At a glance` list of every selected headline and a story count, then
presents the complete commentary in a responsive Bloomberg-terminal-inspired layout: near-black
panels, orange interface labels, white headlines, and explicit email-safe contrast controls.

The editor may leave any editorial section empty; healthy coverage in every possible section is
not required. A normal edition is capped at 10 stories. The 5,000-word edition limit is a
technical safety ceiling, not a target, and applies to the complete validated edition.

When source coverage is healthy but no verified development clears the materiality bar, a
scheduled weekday run can send a short, transparent quiet-day edition instead of filler. The
`send-once` command is stricter and holds with `HELD_NO_MATERIAL_STORIES` unless at least one
verified story is present.

## What each part does

| Part | Job | Why it exists |
|---|---|---|
| `cli.py` | Starts one finite Railway run, applies the daylight-saving guard, and prints a secret-safe receipt. | Railway needs one clear entry point. |
| `config.py` | Validates Railway variables and turns the active source registry into due jobs. | Bad settings stop the run before they can cause damage. |
| `security.py` | Blocks private IPs, unsafe redirects, and domains outside each source allowlist. | A news page cannot turn the collector into a network probe. |
| `fetch.py` | Downloads a page with hard time, byte, and redirect limits. | One bad site cannot consume the whole run. |
| `parsers.py` | Handles RSS, API, HTML, Markdown, and approved email-backed pages. | Different first-party sources need different extraction rules. |
| `collector.py` | Runs the current source jobs with bounded concurrency and per-job failure isolation. | One broken source cannot cancel the rest of the core. |
| `evidence.py` | Asks Gemini 3.8 Flash through OpenRouter at low reasoning to extract facts, then checks every fact in code. | The model may judge; it may not invent evidence. |
| `ranking.py` | Removes previously published events and orders the remaining evidence. | The editor sees the strongest fresh material first. |
| `editor.py` | Drafts the briefing at high reasoning under strict story, fact, number, entity, citation, and 5,000-word edition limits. | High-cost reasoning is reserved for the final editorial pass; unsupported claims are not. |
| `renderer.py` | Produces responsive terminal-style HTML and a plain-text email fallback. | The model never controls layout or executable HTML. |
| `agentmail.py` | Sends one private-BCC message with a stable idempotency key. | Retries cannot create duplicate emails. |
| `sheets.py` | Reads controls and writes events, Railway health receipts, persisted editions, stories, and send receipts in batches. Legacy n8n health rows are preserved and excluded from Railway validation. | The Sheet remains the durable operating record. |
| `health.py` | Requires at least 80% healthy core-job coverage plus fresh coverage in all three operating lanes. | A broken collector cannot masquerade as a quiet news day. |
| `orchestrator.py` | Connects the parts and applies the final delivery gates. | There is one live workflow and one place that can authorize a send. |

## Sheet and Railway controls

Normal operation should not require a code edit or redeploy.

- Add a recipient in the `Recipients` tab and set `active` to `TRUE`. Active recipients are
  validated, deduplicated, and private-BCC'd; the AgentMail sender address is excluded. Delivery
  holds if an address is invalid or the 49-recipient cap is exceeded.
- Add a company in `Company Watchlist`. Aliases support filing and announcement matching; active
  watched companies also control expansion of targeted SEC source `S003`.
- Add a deposit competitor in `Deposit Watch`. Matching candidates receive a deterministic
  ranking boost.
- Activate a `Sources` row only after its read-only canary returns dated, citable items. Every
  active row needs an HTTPS URL, allowed domains, parser lane, cadence, priority, and parser
  version. Inactive, staged, and legacy rows remain preserved.
- Pause everything with Railway `WORKER_ENABLED=false`. The command exits before Sheet or
  network access.
- Pause delivery with any one of: Railway `AGENTMAIL_SEND_ENABLED=false`, Sheet
  `agentmail_send_enabled=FALSE`, Railway `TEST_MODE=true`, or Sheet `test_mode=TRUE`.

## Live-delivery gates

Before an email can leave, all applicable gates must agree:

1. n8n is independently verified inactive as a sender; Railway is the only live runtime.
2. `WORKER_ENABLED=true` and required credentials/configuration validate.
3. A scheduled send is Monday, Wednesday, or Friday inside the local 7:00 AM window, or the operator explicitly
   invokes `send-once`.
4. At least 80% of the applicable launch-core jobs are fresh and healthy. `SUCCESS`,
   `UNCHANGED`, and `VALID_EMPTY` count as healthy only when the parser version matches.
5. Fresh healthy coverage exists in venture debt, competitors/deposits, and AI. Individual
   editorial sections may still be empty.
6. The current collector completion is fresh; stale prior receipts cannot authorize a send.
7. The edition passes deterministic story, fact, entity, number, URL, citation, story-count, and
   word-count validation. `send-once` additionally requires at least one story.
8. Railway and Sheet test mode are both off.
9. Railway and Sheet AgentMail delivery switches are both on.
10. The active recipient list is nonempty, valid, within the 49-recipient cap, and excludes the
    sender inbox.
11. The deterministic send ID has no prior `SENT` receipt.
12. Immediately before delivery, the worker rereads Sheet settings, recipients, Railway-owned
    health rows, source health, the send window when applicable, and duplicate-send state.
13. AgentMail must return both a message ID and thread ID before stories and the digest run are
    recorded as `SENT`.

Provider acceptance is not the same as visible inbox receipt. A first launch is complete only
after the exact subject and body are visible in the configured recipient's inbox and match the
Railway receipt, AgentMail identifiers, `Digest Runs` `SENT` row, and published-story records.

## Deployment status boundary

This README documents the implemented repository behavior and required launch configuration.
It does **not** assert that Railway has been deployed or enabled, that n8n has been verified
inactive, that a live canary or model preview has passed, or that an email has been sent or seen
in an inbox. Those are separate operational checks that require current external receipts.
