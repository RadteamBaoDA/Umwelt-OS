'use client';

import { useTranslations } from 'next-intl';
import { McpSettings } from '@/modules/sources/mcp-editor';

/** Data sources > MCP tab body. Wave 3 owns this file's content. */
export function McpSettingsPage() {
  const t = useTranslations('shell');
  return <section className="content-panel">
    <header className="section-heading"><div><h1>{t('mcpTab')}</h1></div></header>
    <McpSettings />
  </section>;
}
