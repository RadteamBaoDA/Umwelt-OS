import type { useTranslations } from 'next-intl';
import { humanizeCode } from '@/modules/shared';

/** Literal `globalSettings` key per backend health status (no runtime key building). */
const STATUS_KEYS = {
  ok: 'st.ok',
  healthy: 'st.healthy',
  degraded: 'st.degraded',
  down: 'st.down',
  error: 'st.error',
  unavailable: 'st.unavailable',
  unconfigured: 'st.unconfigured',
  configured: 'st.configured',
  not_configured: 'st.notConfigured',
  not_installed: 'st.notInstalled',
} as const;

/** Localizes a backend health status via the `globalSettings` `st.*` keys; unknown codes fall back to sentence-case words. */
export function statusLabel(t: ReturnType<typeof useTranslations<'globalSettings'>>, status: string): string {
  return Object.hasOwn(STATUS_KEYS, status) ? t(STATUS_KEYS[status as keyof typeof STATUS_KEYS]) : humanizeCode(status);
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
