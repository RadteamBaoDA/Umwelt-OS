import { describe, expect, it, vi } from 'vitest';
import { getCatalog } from '@locales';

vi.mock('@/modules/automations', () => ({ getRuleConversationId: vi.fn() }));
vi.mock('@/core/session', () => ({ useSession: () => ({}) }));

import { WIDGET_TITLE_KEY, widgetTitleKey } from './daily-brief';

describe('widgetTitleKey', () => {
  it('maps the fact widgets to their own titles', () => {
    expect(widgetTitleKey('dayMarkets')).toBe('day.markets');
    expect(widgetTitleKey('dayWeather')).toBe('day.weather');
    expect(widgetTitleKey('dayHazards')).toBe('day.hazards');
    expect(widgetTitleKey('daySourceHealth')).toBe('day.sourceHealth');
  });

  it('falls back to events for an unknown id', () => {
    expect(widgetTitleKey('unknown')).toBe('day.events');
  });

  it.each(['en-us', 'vi-vi'] as const)('has every title key in the %s catalog', (locale) => {
    // Catalog keys are dotted paths into the nested JSON.
    const catalog = getCatalog(locale, 'daily') as unknown;
    for (const key of Object.values(WIDGET_TITLE_KEY)) {
      const value = key.split('.').reduce<unknown>((node, part) => (node as Record<string, unknown> | undefined)?.[part], catalog);
      expect(value, key).toBeTruthy();
    }
  });
});
