import type { useTranslations } from 'next-intl';
import { humanizeCode } from '@/modules/shared';

/** Localizes a backend health status via `globalSettings.st_*`; unknown codes fall back to sentence-case words. */
export function statusLabel(t: ReturnType<typeof useTranslations<'globalSettings'>>, status: string): string {
  return t.has(`st_${status}` as never) ? t(`st_${status}` as never) : humanizeCode(status);
}

/** Maps backend component ids to `uiSweep` message keys; shared by System status and Global settings so both show the same service names. */
export const serviceLabelKeys = {
  postgres: 'svc.postgres',
  redis: 'svc.redis',
  worker: 'svc.worker',
  chat_worker: 'svc.chatWorker',
  model_worker: 'svc.modelWorker',
  model_gateway: 'svc.modelGateway',
  graph: 'svc.graph',
  n8n: 'svc.n8n',
  browser: 'svc.browser',
} as const;
