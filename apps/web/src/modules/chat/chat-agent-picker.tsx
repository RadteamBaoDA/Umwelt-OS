'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import type { AgentProfile } from '@/modules/agents';
import { profileTitleKeys } from './chat-session-header';

/** Props of {@link AgentPicker}. */
export interface AgentPickerProps {
  /** Profile id shown (a pending retry pins its own profile). */
  value: string;
  disabled: boolean;
  onChange: (id: string) => void;
  /** Loaded lazily: undefined until the picker was first opened, so plain chat never fetches the agent list. */
  profiles: AgentProfile[] | undefined;
  /** Called when the list opens; the session starts loading profiles on the first call. */
  onOpen: () => void;
}

/**
 * Agent profile picker of the full composer: the Assistant plus each specialist, disabled when unavailable.
 * Styled like the model picker so both read as one toolbar.
 *
 * @param props - See {@link AgentPickerProps}.
 */
export function AgentPicker({ value, disabled, onChange, profiles, onOpen }: AgentPickerProps) {
  const tAgents = useTranslations('aiSettings');
  return (
    <Select value={value} onValueChange={onChange} disabled={disabled} onOpenChange={(open) => open && onOpen()}>
      <SelectTrigger
        aria-label={tAgents('agentSelect')}
        data-testid="chat-agent-picker"
        className="h-8 w-auto max-w-32 min-w-0 border-0 bg-transparent text-xs text-muted-foreground shadow-none hover:bg-active"
      >
        <SelectValue placeholder={tAgents('agentSelect')} />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="assistant">{tAgents('agentAssistant')}</SelectItem>
        {(profiles ?? []).map((profile) => (
          <SelectItem key={profile.id} value={profile.id} disabled={!profile.enabled || profile.capability === 'unavailable'}>
            {tAgents(profileTitleKeys[profile.id] ?? 'agentProfileKnowledge')}
            {profile.capability === 'partial'
              ? ` · ${tAgents('agentPartial')}`
              : profile.capability === 'unavailable'
                ? ` · ${tAgents('agentUnavailable')}`
                : ''}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
