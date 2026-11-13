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
  const titleKeys: Record<string, 'agent.profileSupervisor' | 'agent.profileKnowledge' | 'agent.profileResearch' | 'agent.profilePersonal' | 'agent.profileProject' | 'agent.profileNews' | 'agent.profilePlanning' | 'agent.profileAutomation'> = {
    supervisor: 'agent.profileSupervisor', knowledge: 'agent.profileKnowledge', research: 'agent.profileResearch',
    personal: 'agent.profilePersonal', project: 'agent.profileProject', news: 'agent.profileNews',
    planning: 'agent.profilePlanning', automation: 'agent.profileAutomation',
  };
  return (
    <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4" aria-label={t('agents.title')}>
      {profiles.map((profile) => (
        <Button
          key={profile.id}
          type="button"
          aria-pressed={selectedId === profile.id}
          onClick={() => onSelect(profile.id)}
          className={`secondary h-auto w-full justify-start rounded-lg p-3 text-left font-normal transition-colors ${selectedId === profile.id ? 'border-primary bg-secondary' : 'border-border bg-background hover:bg-secondary'}`}
        >
          <span className="block font-medium text-foreground">{t(titleKeys[profile.id] ?? 'agent.profileKnowledge')}</span>
          <span className="mt-1 block text-xs text-muted-foreground">
            {profile.capability === 'available' ? t('agent.available') : profile.capability === 'partial' ? t('agent.partial') : t('agent.unavailable')}
            {!profile.enabled ? ` · ${t('agent.disabled')}` : ''}
          </span>
        </Button>
      ))}
    </div>
  );
}
