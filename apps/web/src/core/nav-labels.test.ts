import { describe, expect, it } from 'vitest';
import { shellMessages } from '@/core/messages/shell';
import { commandDestinations, sourcesSubNavigation } from './module-registry';

describe('navigation labels', () => {
  it.each(['en-us', 'vi-vi'] as const)('every destination has a %s shell label', (locale) => {
    const messages = shellMessages[locale] as Record<string, string>;
    for (const d of [...commandDestinations, ...sourcesSubNavigation]) expect(messages[d.messageKey], d.id).toBeTruthy();
  });
});
