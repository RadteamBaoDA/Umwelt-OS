# Privacy notice

Updated 7 November 2026. Operator: **RADTeam**. Privacy and support contact: **[contact@baoda.io.vn](mailto:contact@baoda.io.vn)**. Website: [umwelt.baoda.io.vn](https://umwelt.baoda.io.vn). Intended operating jurisdiction: Vietnam. The operator must confirm its legal identity, applicable notices and required procedures before making this instance available to additional users.

## Who controls this deployment

Umwelt-OS is self-hosted software. The person or organization operating your instance determines its purposes, infrastructure, access, optional integrations and retention. The software project is not automatically the operator of your instance. Ask the operator about the legal basis for processing, statutory rights, international transfers, hosting location and complaints to the appropriate regulator. The operator must supply jurisdiction-specific disclosures and any required consent before collecting data.

## Data used to provide the workspace

The instance stores account identifiers, password hashes, sessions, preferences, workspace membership and invitation records. Optional Google sign-in requests the OpenID Connect identity scopes `openid`, `email` and `profile`; the application stores the identity issuer, subject and email to authenticate a linked account. Sign-in uses identity scopes only. Separately, the application supports optional read-only Gmail, Google Calendar and Google Drive connectors. If an operator enables them and you authorize a configured source, the connector can collect messages from selected Gmail labels (including message headers and inline plain-text content, not attachments), events from selected calendars, and supported text files or Google Docs from selected Drive locations. The instance processes imported source content and provenance, uploaded originals, extracted text, chunks, embeddings, entities and relationships, search indexes, chat messages, attachments, AI results, translations, briefs, notes, tasks, automations, source configuration, connection state and operational records. Some source material may identify other people. Import only what you have authority to process.

These data support authentication, ingestion, search, citations, AI assistance, workspace features, permissions and operation. Derived data can reproduce source content. Ordinary logs and metadata can still contain identifiers; redaction reduces exposure but is not a guarantee that every host log is free of personal data.

## Cookies and your browser

Authentication/session and anti-forgery mechanisms use cookies. Browser storage holds appearance/language preferences and may hold cached application state, drafts and workspace content. This repository does not add advertising cookies or an advertising analytics SDK. Your host, reverse proxy or extensions can add other processing; the operator must disclose that separately. Signing out or erasure cleanup cannot erase screenshots, browser history, downloads or another person's exported copies. On a shared device, review site storage and downloads.

## Credentials and security

Provider keys, OAuth credentials, connector tokens and infrastructure secrets are intended for server-side configuration or protected backend storage. Entering a credential through a settings form necessarily sends it to your instance. The operator controls deployment secrets, encryption keys, access, TLS, patches and logging. No software can promise absolute security. Do not put credentials in prompts, public source URLs or imported documents.

## Optional recipients and network requests

Enabling an AI provider may send prompts, selected chat/history and source excerpts, tool inputs/results included in messages, embedding text, reranking queries/documents, model/tool schemas and generation settings to that endpoint, according to the enabled capability. A gateway or aggregator and its downstream model operator are separate potential recipients; the operator must identify both. Tools and integrations may send their requested arguments or scoped content. Web search sends literal search terms and options; a SearXNG instance may forward them to the upstream engines selected in its own configuration. Collectors send source identifiers, filters, URLs and any required authentication. Google sign-in sends an identity-only authorization request and exchanges authentication data with Google. Google content connectors use separate read-only authorization scopes and send requests only for the configured source and selected scope; collected content is then stored and processed by this instance and may be included in later AI requests according to the enabled features. SEC EDGAR requests include the operator-entered contact email in the User-Agent.

Optional Langfuse export sends bounded model-call metadata (model name, token counts, timing and correlation identifiers), not prompt/document/completion bodies through that exporter; it requires both enablement and egress approval. All network recipients can separately observe source IP, time, headers and request metadata. Local model endpoints remain local only if your network and endpoint configuration keep them local.

Expand **Services and data flows** below for the implementation inventory. Code presence does not mean a service is configured or active. Hosting, DNS, TLS certificate authorities, tunnels and backups depend on the operator. In the supplied Docker configuration Next.js telemetry is disabled and n8n diagnostics/personalization are disabled. Development tools or other deployments may differ.

## Retention, export and deletion

The instance retains content and derived records until removed by supported controls, account erasure or the operator's retention process. No universal retention period is established for all record types. Use supported export controls to obtain available data; an export may contain sensitive originals and derived content. Secure and delete your exported copies yourself.

Account deletion requires identity confirmation and eligibility checks, disables ordinary account access, revokes sessions and runs asynchronous cleanup. A status receipt reports progress or failure; completion must not be inferred while required cleanup has failed. Keep the receipt private. If it is lost, the host can inspect redacted progress and retry failed cleanup, but cannot recreate browser receipt access. Shared workspaces and other accounts' content require ownership-aware handling, rather than deleting everyone else's data. The bootstrap account leaves a sanitized disabled technical tombstone that keeps public setup closed. Privileged host recovery can create new empty administration after completed erasure; it restores no old content or sessions. The deletion status receipt lasts seven days after completion.

The graph store retains a permanent technical deletion marker containing only an opaque partition identifier and an erased flag, without source content or an account/workspace mapping. Original-file storage also retains a permanent lock/one-byte deletion marker identified only by an opaque workspace UUID, without account details or source content. These markers prevent delayed requests from recreating deleted content; removing them would remove that protection.

Deletion concerns this instance's supported active persistence stores and browser cleanup. Host backups, snapshots, logs outside the application's control, environment secrets, downloaded/exported copies and external providers require separate operator or recipient action. Restoring an older backup can reintroduce deleted data; the operator must apply deletion decisions on restore. No external provider deletion, training exclusion or retention deadline is promised. Review each recipient's terms and settings before enabling it, and contact that recipient when necessary.

## Your choices and requests

Avoid optional integrations, restrict sources and tool permissions, select an appropriate local model, export data or request erasure as your situation requires. Contact **[contact@baoda.io.vn](mailto:contact@baoda.io.vn)** for access, correction, objection, restriction, portability, erasure and other applicable rights. The operator must explain lawful exceptions and applicable response periods; this notice does not invent a deadline or waive mandatory rights.
