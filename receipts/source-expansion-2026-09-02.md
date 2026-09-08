# Source expansion production receipt — 2026-09-02

Status: **LIVE AND CANARIED**

- Railway production deployment: `a6e2fc77-f681-4656-a3fe-fc80c6312dc0` (`SUCCESS`)
- Google Sheet registry: 224 configured, 131 active, 93 inactive
- Active sections: 47 Deal Tape, 21 Competitive Field, 13 Deposit Watch, 40 AI Radar,
  10 Runway Watch
- Expansion activation: 53 vetted rows
  - 43 new-source canary passes
  - 4 recovered existing feeds
  - 6 repaired and re-canaried feeds
- Full active-registry canary: 131 sources, 135 jobs, 135 successful, 0 failed,
  2,845 parsed candidates
- Canary boundary: fetch and parse only; no event writes, model calls, recipient changes,
  or email sends

The expansion adds deposit competitors and partner banks, venture-debt and private-credit
lenders, AI model/platform/security releases, funding discovery, banking regulation, and
bank/fintech acquisition coverage. Failed, undated, stale, ambiguous, or low-signal feeds remain
inactive in the same Sheet for later repair rather than weakening production health.

The larger registry does not enlarge the reader-facing email. Deterministic code still permits
at most two candidates per source, at most 24 total evidence-model reviews, and at most 10
published stories.
