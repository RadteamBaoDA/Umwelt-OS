'use client';

import { useTranslations } from 'next-intl';
import { SettingsPageHeader } from '@/components/settings-layout';
import { McpSettings } from '@/modules/sources/mcp-editor';

/** Data sources > MCP tab body: one page heading (title and one-line description) above the connections section. */
export function McpSettingsPage() {
  const t = useTranslations('shell');
  const tMcp = useTranslations('mcp');
  return <div className="flex flex-col gap-5">
    <SettingsPageHeader eyebrow={t('settings')} title={tMcp('title')} description={tMcp('description')} />
    <McpSettings />
  </div>;
}
