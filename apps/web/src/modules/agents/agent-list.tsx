'use client';

import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import type { AgentProfile } from './api';

/** Props for the fixed-roster profile selector used by Settings and full Chat. */
export interface AgentListProps {
  profiles: AgentProfile[];
  selectedId: string;
  onSelect: (profileId: string) => void;
}

/** Lists the fixed specialist roster and keeps capability state visible beside each profile. */
export function AgentList({ profiles, selectedId, onSelect }: AgentListProps) {
  const t = useTranslations('aiSettings');
  const titleKeys: Record<string, 'agentProfileSupervisor' | 'agentProfileKnowledge' | 'agentProfileResearch' | 'agentProfilePersonal' | 'agentProfileProject' | 'agentProfileNews' | 'agentProfilePlanning' | 'agentProfileAutomation'> = {
    supervisor: 'agentProfileSupervisor', knowledge: 'agentProfileKnowledge', research: 'agentProfileResearch',
    personal: 'agentProfilePersonal', project: 'agentProfileProject', news: 'agentProfileNews',
    planning: 'agentProfilePlanning', automation: 'agentProfileAutomation',
  };
  return (
    <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4" aria-label={t('agentsTitle')}>
      {profiles.map((profile) => (
        <Button
          key={profile.id}
          type="button"
          aria-pressed={selectedId === profile.id}
          onClick={() => onSelect(profile.id)}
          className={`secondary h-auto w-full justify-start rounded-lg p-3 text-left font-normal transition-colors ${selectedId === profile.id ? 'border-primary bg-secondary' : 'border-border bg-background hover:bg-secondary'}`}
        >
          <span className="block font-medium text-foreground">{t(titleKeys[profile.id] ?? 'agentProfileKnowledge')}</span>
          <span className="mt-1 block text-xs text-muted-foreground">
            {profile.capability === 'available' ? t('agentAvailable') : profile.capability === 'partial' ? t('agentPartial') : t('agentUnavailable')}
            {!profile.enabled ? ` · ${t('agentDisabled')}` : ''}
          </span>
        </Button>
      ))}
    </div>
  );
}
