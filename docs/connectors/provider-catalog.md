# Provider catalog

This catalog distinguishes implemented mapper scope from provider ideas that still need a
permitted endpoint, source scope, authentication contract, licensing review, or adapter work.
Public access alone does not imply permission to redistribute provider content.

## World and news sources

| Provider | Availability | Bounded scope and known limits |
|---|---|---|
| RSS / Atom | Supported through the generic feed connector | Owner supplies a permitted public feed URL. A returned feed is a snapshot; missing entries do not imply deletion or complete history. |
| Google News | Planned; unsupported operation | Official feed construction and query contract are not established. A generic RSS URL is not Google News support. |
| YouTube | Mapper implemented; integration gate open | One configured public channel Atom feed. Metadata only; no transcripts, media, private videos, or notification guarantee. Returned feed only; missed edits and removals are possible. |
| arXiv | Mapper implemented; integration gate open | One configured category Atom feed, metadata only. Daily feed snapshots are not an archive. A shared PostgreSQL advisory lock spaces requests by at least three seconds. |
| Hugging Face | Mapper implemented; integration gate open | One public author listing, at most 100 model metadata rows. On 429, the latest applicable valid retry/reset deadline is honored, including the official `RateLimit` API bucket `t`; `RateLimit-Policy` describes policy and is not a reset deadline. No model cards, weights, private grants, or inference. Public quotas can change and are shared by egress IP. |
| GitHub Releases | Mapper implemented; integration gate open | One configured public repository, at most five REST pages / 500 releases. No assets or complete edit history. Repository content licensing remains source-specific. |
| GitHub App | Native adapter implemented; runtime/provider acceptance deferred | One repository selected from an installed GitHub App, with separately selected issue, pull request, commit, and release reads. BBD-OS owns expiring user OAuth, encrypted rotating tokens, PKCE, bounded requests, and peer-aware revocation; n8n carries scheduling only. Runtime/provider acceptance remains deferred. |
| Telegram | Mapper implemented; credentials and integration required | Configured channels only, after bot identity, empty webhook, channel type, and administrator rights are verified. Each full Bot API response body is capped at 10 MiB and counted against the 25 MiB trigger budget, including envelope and filtered events. Pending updates only; no historical bootstrap, media download, or inferred deletion. |
| Reddit | Planned; endpoint, authentication, quota, and content terms unresolved | No scraping or generic REST relabeling. |
| Hacker News | Planned; a bounded story scope and content reuse policy are unresolved | Official API documentation does not establish a user-post redistribution license or edit timestamp. |
| Mastodon | Planned; instance and access gates unresolved | Public timeline availability varies by instance and may require an app token with read scope. |
| Bluesky | Planned; endpoint, rate, and content terms unresolved | No endpoint or license assumptions. |
| X / Twitter | Planned; requires an authorized provider/API contract | No scraping or assumed access. |
| Vietnamese press | Planned; site-specific RSS and permission terms required | No blanket press reuse permission. |
| GDELT / government sources | Planned; named source and bounded scope required | No generic provider or quota claims. |
| Alpha Vantage | Daily equity OHLCV adapter and encrypted API-key slot implemented; API key required and provider activation gated | `TIME_SERIES_DAILY` raw as-traded endpoint only; at most five configured symbols, one daily scheduled run, and a local Redis budget of 25 requests per credential reference per UTC day (manual collection uses the same budget). Reservations are conservative and may reset if Redis state is lost; other deployments using the same key and provider-side usage remain outside this local budget. Currency and exchange timezone are explicit source scope because the response date alone does not establish them. No premium adjusted/realtime quote claim. Official Terms of Service PDF at https://www.alphavantage.co/terms_of_service/ was read on 2026-10-05: personal-use grant only absent written agreement; commercial use includes organizational use, redistribution/third-party access, and specified finance-sector affiliations. Key acceptance, exact entitlement, live quota, deployment eligibility and activation remain unverified; provider quota responses remain authoritative and rate-limited runs do not publish empty series. |
| Open-Meteo | Bounded hourly forecast adapter implemented; deployment eligibility and live activation remain owner gates | Official schema at https://open-meteo.com/en/docs and terms at https://open-meteo.com/en/terms inspected 2026-10-05. Three-day forecast only, configured coordinates/metrics/timezone, returned units and attribution retained. Free API is non-commercial with published request ceilings; commercial deployments require an appropriate subscription. Forecast data is not a disaster or climate-history adapter. |
| Other finance families | Unavailable | Crypto, commodities, macro/government, and exchange/composite data each require named provider schemas, terms, entitlements, and bounded scope. Alpha Vantage macro/commodities also depend on FRED API terms; no such adapters are registered. |
| Disaster / climate feeds | Planned; named endpoint and usage terms required | Open-Meteo forecast values are not disaster event observations or climate-history records. |
| Traffic | Unavailable | No authorized named provider/schema or bounded adapter. |
| Cybersecurity / CVE | Unavailable | No authorized named provider/schema or bounded adapter; no complete feed/history claim. |
| Maps / OSINT | Planned; source-specific scope and permission required | Structured observation work is tracked separately. |

## Personal sources

| Provider | Availability | Gate |
|---|---|---|
| Gmail / Calendar / Drive | Planned | Separate least-privilege user OAuth consent and provider scopes. Owner login does not grant collection authority. |
| Browser data | Planned | Explicit local data selection and retention controls. |
| Notes | Planned | A named supported storage adapter and ownership boundary. |
| GitHub | Planned | The public Releases mapper does not implement the separate OAuth repositories/issues/pull requests/commits scope. |
| Health | Planned | Explicit source authorization, sensitive-data handling, and retention terms. |
| Personal finance | Planned | Named institution/API scope and explicit owner authorization. |
| IoT / Home Assistant | Planned | Authenticated local instance scope and device permissions. |
| Notion | Planned | Least-privilege OAuth and selected workspace scope. |
| Slack | Planned | Workspace OAuth, selected scopes, and workspace content terms. |
| MCP sources | Planned | Each server/tool requires its own authentication and permission contract. |

## Shared limits

Native snapshot mappers use fixed HTTPS hosts, verified TLS, no redirects, bounded response sizes,
an absolute 30-second timeout per physical request, and at most 4,000 text characters per record.
Truncation is represented explicitly. Collection time is stored separately from provider observation
time. A metadata hash detects changed snapshots but does not establish chronology. Provider errors
never advance a durable cursor; rate responses carry a finite next-eligible time using the latest
applicable supplied retry/reset evidence. A conservative fallback is used only when no applicable
deadline evidence is supplied; malformed, overflowing, or UTC-unrepresentable supplied deadlines
fail with a fixed provider error. Telegram uses a 60-second fallback only when its
429 response omits `retry_after`.

The mapper files alone do not establish route registration, workflow execution, provider terms
acceptance, live quotas, or runtime/provider acceptance. Those remain integration and operational
gates for the complete R13 slice.
