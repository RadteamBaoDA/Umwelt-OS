import { apiRequest, csrfHeaders } from '@/core/api';
import type {
  TranslationBatch, TranslationBatchAccepted, TranslationItemRequest, TranslationSettings, TranslationSettingsUpdate,
} from './types';

/** Reads the workspace translation setting; members may read. */
export function fetchTranslationSettings(signal?: AbortSignal): Promise<TranslationSettings> {
  return apiRequest<TranslationSettings>('/api/v1/settings/translation', { signal });
}

/** Owner-only compare-and-set update; 409 means the revision is stale. */
export function saveTranslationSettings(value: TranslationSettingsUpdate, csrfToken: string): Promise<TranslationSettings> {
  return apiRequest<TranslationSettings>('/api/v1/settings/translation', {
    method: 'PATCH', headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' }, body: JSON.stringify(value),
  });
}

/** Queues up to 25 resource references. */
export function submitTranslationBatch(items: TranslationItemRequest[], csrfToken: string, signal?: AbortSignal): Promise<TranslationBatchAccepted> {
  return apiRequest<TranslationBatchAccepted>('/api/v1/translations/batches', {
    method: 'POST', headers: { ...csrfHeaders(csrfToken), 'Content-Type': 'application/json' }, body: JSON.stringify({ items }), signal,
  });
}

/** Reads one batch of this actor. */
export function readTranslationBatch(batchId: string, signal?: AbortSignal): Promise<TranslationBatch> {
  return apiRequest<TranslationBatch>(`/api/v1/translations/batches/${encodeURIComponent(batchId)}`, { signal });
}
