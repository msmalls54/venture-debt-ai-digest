# Security

## Secrets

Keep Google service-account JSON, OpenRouter keys, AgentMail keys, recipient data, and live
configuration in Railway variables or an ignored local `.env` file. Never commit credentials,
provider receipts, generated editions, or production Sheet exports.

If a credential is committed or posted in an issue, revoke it at the provider immediately,
replace it in Railway, and review recent provider activity before resuming the worker.

## Reporting a problem

Do not place sensitive details in a GitHub issue. Report the affected component and a redacted
reproduction privately to the repository owner.

The collector intentionally restricts URLs, redirects, response sizes, timeouts, and source
allowlists. Changes to those boundaries should include regression coverage.
