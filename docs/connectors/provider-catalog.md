# Provider catalog

This catalog distinguishes implemented mapper scope from provider ideas that still need a
permitted endpoint, source scope, authentication contract, licensing review, or adapter work.
Public access alone does not imply permission to redistribute provider content.

Fixed-endpoint free presets (BBC, VnExpress, Hacker News top stories, GDELT, World Bank, Frankfurter, ECB, Binance, Alternative.me, USGS, CoinPaprika, CoinGecko, Alpha Vantage) also have terms, quota and setup facts in [free-sources.md](free-sources.md). Their live behaviour is not yet verified; see [provider-research-2026-10-07.md](provider-research-2026-10-07.md).

## Inventory

Generated from `modules/connectors/catalog.py` and `modules/connectors/provider_specs.py` (free-provider facts checked on 2026-10-07, policy revision 1). Read the table as follows:

- **Availability** is the catalog's own value and governs source selection. `implemented` means a collection adapter is registered; it does not mean the provider works live.
- **Code registered** means the shared executor has an adapter for the provider. It is not a runtime, licence or deployment acceptance.
- **Runtime verified** is `no` for every row. No provider has live verification yet; endpoint shapes and quotas are plan-documented, not observed.
- **Eligibility** is the catalog class (`open`, `personal`, `noncommercial`, `review`) or `unknown` for providers without a free preset. It is never a grant for a workspace; see `free-sources.md`.

The 11 registered fixed-endpoint native providers are `bbc_world`, `vnexpress_business`, `hn_top`, `gdelt_economy`, `world_bank`, `frankfurter`, `ecb`, `binance`, `alternative_me`, `usgs` and `coinpaprika`. `gdelt_economy` is `experimental`. The native registry also holds the earlier providers (`youtube`, `arxiv`, `huggingface`, `github_releases`, `github`, `telegram`, `alpha_vantage`, `open_meteo`), which are listed below with their own availability.

Notes on specific rows:

- `hn_top` (implemented top-stories preset) and `hacker_news` (planned, broader catalog entry) are separate rows. Only `hn_top` has an adapter.
- `coingecko` is not registered. Its preset facts exist, but no adapter is registered and the Demo key has no credential slot. Its availability is `planned`.
- `finance` is `planned` in the catalog, while the prose below lists crypto, commodity and macro families as unavailable. Only `alpha_vantage` has an adapter in that family.
- `alpha_vantage` is `requires_credentials`: an owner-supplied key is required, stored encrypted, and activation is gated.
- No row is `disabled`. Free presets start disabled per source; the catalog has no disabled value.

| ID | Availability | Eligibility | Execution | Code registered | Runtime verified | Default interval (min) | Terms checked |
| `rss` | `available` | unknown | - | yes | no | - | - |
| `web` | `available` | unknown | - | yes | no | - | - |
| `rest` | `available` | unknown | - | yes | no | - | - |
| `mcp` | `available` | unknown | - | yes | no | - | - |
| `github` | `requires_credentials` | unknown | - | yes | no | - | - |
| `google_mail` | `unavailable` | unknown | - | no | no | - | - |
| `google_calendar` | `unavailable` | unknown | - | no | no | - | - |
| `google_drive` | `unavailable` | unknown | - | no | no | - | - |
| `youtube` | `implemented` | unknown | - | yes | no | - | - |
| `arxiv` | `implemented` | unknown | - | yes | no | - | - |
| `huggingface` | `implemented` | unknown | - | yes | no | - | - |
| `github_releases` | `implemented` | unknown | - | yes | no | - | - |
| `telegram` | `requires_credentials` | unknown | - | yes | no | - | - |
| `alpha_vantage` | `requires_credentials` | personal | supported | yes | no | 1440 | 2026-10-07 |
| `open_meteo` | `implemented` | unknown | - | yes | no | - | - |
| `google_news` | `planned` | unknown | - | no | no | - | - |
| `reddit` | `planned` | unknown | - | no | no | - | - |
| `hacker_news` | `planned` | unknown | - | no | no | - | - |
| `mastodon` | `planned` | unknown | - | no | no | - | - |
| `bluesky` | `planned` | unknown | - | no | no | - | - |
| `x` | `planned` | unknown | - | no | no | - | - |
| `vietnamese_press` | `planned` | unknown | - | no | no | - | - |
| `gdelt_government` | `planned` | unknown | - | no | no | - | - |
| `finance` | `planned` | unknown | - | no | no | - | - |
| `weather_disaster_climate` | `planned` | unknown | - | no | no | - | - |
| `cyber_cve` | `planned` | unknown | - | no | no | - | - |
| `map_osint` | `planned` | unknown | - | no | no | - | - |
| `browser` | `unsupported_operation` | unknown | - | no | no | - | - |
| `notes` | `planned` | unknown | - | no | no | - | - |
| `health` | `planned` | unknown | - | no | no | - | - |
| `personal_finance` | `planned` | unknown | - | no | no | - | - |
| `iot` | `planned` | unknown | - | no | no | - | - |
| `notion` | `planned` | unknown | - | no | no | - | - |
| `slack` | `planned` | unknown | - | no | no | - | - |
| `home_assistant` | `planned` | unknown | - | no | no | - | - |
| `bbc_world` | `implemented` | review | supported | yes | no | 30 | 2026-10-07 |
| `vnexpress_business` | `implemented` | noncommercial | supported | yes | no | 30 | 2026-10-07 |
| `hn_top` | `implemented` | review | supported | yes | no | 60 | 2026-10-07 |
| `gdelt_economy` | `implemented` | review | experimental | yes | no | 60 | 2026-10-07 |
| `world_bank` | `implemented` | open | supported | yes | no | 1440 | 2026-10-07 |
| `frankfurter` | `implemented` | open | supported | yes | no | 1440 | 2026-10-07 |
| `ecb` | `implemented` | open | supported | yes | no | 1440 | 2026-10-07 |
| `binance` | `implemented` | review | supported | yes | no | 15 | 2026-10-07 |
| `alternative_me` | `implemented` | open | supported | yes | no | 1440 | 2026-10-07 |
| `usgs` | `implemented` | open | supported | yes | no | 60 | 2026-10-07 |
| `coinpaprika` | `implemented` | personal | supported | yes | no | 15 | 2026-10-07 |
| `coingecko` | `planned` | review | supported | no | no | 15 | 2026-10-07 |


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
| Hacker News | `hacker_news` planned; the `hn_top` top-stories preset is implemented (see Inventory) | Official API documentation does not establish a user-post redistribution license or edit timestamp. Linked articles are not fetched or licensed by the HN API. |
| Mastodon | Planned; instance and access gates unresolved | Public timeline availability varies by instance and may require an app token with read scope. |
| Bluesky | Planned; endpoint, rate, and content terms unresolved | No endpoint or license assumptions. |
| X / Twitter | Planned; requires an authorized provider/API contract | No scraping or assumed access. |
| Vietnamese press | Planned; site-specific RSS and permission terms required | No blanket press reuse permission. |
| GDELT / government sources | `gdelt_government` planned; `gdelt_economy` economy article list is implemented as experimental | Government sources need named source and bounded scope. The GDELT preset is metadata-only with no quota claim beyond its pilot ceiling. |
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
