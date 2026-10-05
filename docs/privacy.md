# Privacy and data handling

Phase 0 stores one owner password hash, opaque session-token hashes, CSRF-token hashes, and service data in local PostgreSQL/Redis volumes. Raw owner passwords and session tokens are not persisted. Browser cookies are HTTPOnly, SameSite=Lax, and Secure when enabled for HTTPS.

The setup token, CSRF signing secret, database password, and optional OmniRoute key belong only in the ignored local `.env` file or the deployment's protected secret store. Do not commit them or include them in screenshots, support bundles, or shared logs. Request validation errors omit submitted input; unhandled errors log a request ID without request bodies.

Phase 2 stores uploaded originals under the local data directory and extracted text, chunks, connector observations, and provenance in local PostgreSQL. Phase 2 does not create embeddings, semantic indexes, or AI prompts. Search and AI answers must not be presented as ready until their later phases are implemented.

n8n stores provider credentials in its local encrypted data volume. Set `N8N_ENCRYPTION_KEY` before adding credentials. The API collector token is shown once during connector setup; PostgreSQL stores only its hash. Manual Sync now calls a private n8n webhook protected by the shared `N8N_WEBHOOK_TOKEN` Header Auth credential. Keep both values in the ignored `.env` file or protected deployment secrets, never in workflow exports, source configuration, screenshots, or logs. Provider credentials remain in n8n and are not copied into Umwelt-OS.

Deleting a source's data first commits an archived source generation and a durable purge operation with the raw-file URI snapshot. A worker then removes documents, versions, chunks, observations, pending ingestion work, and raw files before marking the operation complete. The owner can poll `/api/v1/system/operations/{operation_id}`. If the worker stops, the stored operation and outbox event allow cleanup to resume; until completion the UI reports the operation state. Source archiving without “Delete all data” preserves its content. Local backups can retain previously backed-up copies, so remove those separately according to the backup retention policy.
