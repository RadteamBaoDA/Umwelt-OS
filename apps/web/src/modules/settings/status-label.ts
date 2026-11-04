import type { useTranslations } from 'next-intl';
import { humanizeCode } from '@/modules/shared';

/** Localizes a backend health status via `globalSettings.st_*`; unknown codes fall back to sentence-case words. */
export function statusLabel(t: ReturnType<typeof useTranslations<'globalSettings'>>, status: string): string {
  return t.has(`st_${status}` as never) ? t(`st_${status}` as never) : humanizeCode(status);
}

/** Maps backend component ids to `uiSweep` message keys; shared by System status and Global settings so both show the same service names. */
export const serviceLabelKeys: Record<string, string> = { postgres: 'svcPostgres', redis: 'svcRedis', worker: 'svcWorker', chat_worker: 'svcChatWorker', model_worker: 'svcModelWorker', model_gateway: 'svcModelGateway', graph: 'svcGraph', n8n: 'svcN8n', browser: 'svcBrowser' };
