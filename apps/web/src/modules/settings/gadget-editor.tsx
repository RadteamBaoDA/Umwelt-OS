'use client';

import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { useState } from 'react';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { TopicSettings } from '@/modules/news/topic-settings';
import { GadgetLibrary } from './gadget-library';
import { HighlightRuleEditor } from './highlight-rule-editor';

/** Settings > Dashboard & Gadget: dashboards and presets, the gadget library and highlight rules. */
export function GadgetEditor() {
  const t = useTranslations('gadgetSettings');
  const session = useWorkspaceSession();
  const [tab, setTab] = useState('library');
  const [ruleTarget, setRuleTarget] = useState<string | null>(null);
  return (
    <Tabs value={tab} onValueChange={setTab} className="space-y-4">
      <TabsList aria-label={t('tabsLabel')} className="h-auto flex-wrap">
        <TabsTrigger value="presets" className="min-h-11">{t('tabPresets')}</TabsTrigger>
        <TabsTrigger value="library" className="min-h-11">{t('tabLibrary')}</TabsTrigger>
        <TabsTrigger value="rules" className="min-h-11">{t('tabRules')}</TabsTrigger>
      </TabsList>
      <TabsContent value="presets" className="space-y-4">
        <div><h2 className="text-base font-semibold">{t('presetsTitle')}</h2><p className="muted">{t('presetsIntro')}</p><Link className="underline" href="/dashboard">{t('openDashboard')}</Link></div>
        <TopicSettings csrfToken={session.csrfToken} />
      </TabsContent>
      <TabsContent value="library"><GadgetLibrary onEditRules={(id) => { setRuleTarget(id); setTab('rules'); }} /></TabsContent>
      <TabsContent value="rules"><HighlightRuleEditor key={ruleTarget ?? 'any'} initialDefinitionId={ruleTarget} /></TabsContent>
    </Tabs>
  );
}
