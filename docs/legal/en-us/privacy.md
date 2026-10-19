# Privacy notice

Draft dated 19 October 2026. Operator: **[LEGAL NAME]**. Contact: **[CONTACT EMAIL]**. Applicable jurisdiction: **[JURISDICTION]**. These placeholders require operator and legal review before publication for other users.

## Who controls this deployment

Umwelt-OS is self-hosted software. The person or organization operating your instance determines its purposes, infrastructure, access, optional integrations and retention. The software project is not automatically the operator of your instance. Ask the operator about the legal basis for processing, statutory rights, international transfers, hosting location and complaints to the appropriate regulator. The operator must supply jurisdiction-specific disclosures and any required consent before collecting data.

## Data used to provide the workspace

The instance stores account identifiers, password hashes, sessions, preferences, workspace membership and invitation records. Optional Google sign-in stores the identity issuer, subject and email. It processes imported source content and provenance, uploaded originals, extracted text, chunks, embeddings, entities and relationships, search indexes, chat messages, attachments, AI results, translations, briefs, notes, tasks, automations, source configuration, connection state and operational records. Some source material may identify other people. Import only what you have authority to process.

These data support authentication, ingestion, search, citations, AI assistance, workspace features, permissions and operation. Derived data can reproduce source content. Ordinary logs and metadata can still contain identifiers; redaction reduces exposure but is not a guarantee that every host log is free of personal data.

## Cookies and your browser

Authentication/session and anti-forgery mechanisms use cookies. Browser storage holds appearance/language preferences and may hold cached application state, drafts and workspace content. This repository does not add advertising cookies or an advertising analytics SDK. Your host, reverse proxy or extensions can add other processing; the operator must disclose that separately. Signing out or erasure cleanup cannot erase screenshots, browser history, downloads or another person's exported copies. On a shared device, review site storage and downloads.

## Credentials and security

Provider keys, OAuth credentials, connector tokens and infrastructure secrets are intended for server-side configuration or protected backend storage. Entering a credential through a settings form necessarily sends it to your instance. The operator controls deployment secrets, encryption keys, access, TLS, patches and logging. No software can promise absolute security. Do not put credentials in prompts, public source URLs or imported documents.

## Optional recipients and network requests

Enabling an AI provider may send prompts, selected chat/history, retrieved context, documents or text to embed, model/tool schemas and generation settings to that endpoint. Tools and integrations may send their requested arguments or scoped content. Web search sends search terms and options; collectors send source identifiers, filters, URLs and any required authentication. Google sign-in exchanges authorization data with Google. SEC EDGAR requests include the operator-entered contact email in the User-Agent.

Optional Langfuse export sends bounded model-call metadata (model name, token counts, timing and correlation identifiers), not prompt/document/completion bodies through that exporter; it requires both enablement and egress approval. All network recipients can separately observe source IP, time, headers and request metadata. Local model endpoints remain local only if your network and endpoint configuration keep them local.

Expand **Services and data flows** below for the implementation inventory. Code presence does not mean a service is configured or active. Hosting, DNS, TLS certificate authorities, tunnels and backups depend on the operator. In the supplied Docker configuration Next.js telemetry is disabled and n8n diagnostics/personalization are disabled. Development tools or other deployments may differ.

## Retention, export and deletion

The instance retains content and derived records until removed by supported controls, account erasure or the operator's retention process. No universal retention period is established for all record types. Use supported export controls to obtain available data; an export may contain sensitive originals and derived content. Secure and delete your exported copies yourself.

Account deletion requires identity confirmation and eligibility checks, disables ordinary account access, revokes sessions and runs asynchronous cleanup. A status receipt reports progress or failure; completion must not be inferred while required cleanup has failed. Keep the receipt private. A lost receipt requires host-assisted recovery. Shared workspaces and other accounts' content require ownership-aware handling, rather than deleting everyone else's data. The bootstrap account may leave a disabled, non-personal technical tombstone. The deletion status receipt lasts seven days after completion.

Deletion concerns this instance's supported active persistence stores and browser cleanup. Host backups, snapshots, logs outside the application's control, environment secrets, downloaded/exported copies and external providers require separate operator or recipient action. Restoring an older backup can reintroduce deleted data; the operator must apply deletion decisions on restore. No external provider deletion, training exclusion or retention deadline is promised. Review each recipient's terms and settings before enabling it, and contact that recipient when necessary.

## Your choices and requests

Avoid optional integrations, restrict sources and tool permissions, select an appropriate local model, export data or request erasure as your situation requires. Contact **[CONTACT EMAIL]** for access, correction, objection, restriction, portability, erasure and other applicable rights. The operator must explain lawful exceptions and applicable response periods; this draft does not invent a deadline or waive mandatory rights.
