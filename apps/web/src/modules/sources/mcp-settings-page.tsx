'use client';

import { useTranslations } from 'next-intl';
import { SettingsPageHeader } from '@/modules/settings/settings-layout';
import { McpSettings } from '@/modules/sources/mcp-editor';

/** Data sources > MCP tab body. Wave 3 owns this file's content. */
export function McpSettingsPage() {
  const t = useTranslations('shell');
  return <div className="flex flex-col gap-5">
    <SettingsPageHeader eyebrow={t('settings')} title={t('mcpTab')} />
    <McpSettings />
  </div>;
}
