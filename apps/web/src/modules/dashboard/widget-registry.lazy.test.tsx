import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { GadgetInstance } from './api';
import { isGadgetRendererRegistered, resolveGadgetRenderer } from './widget-registry';

vi.mock('./gadgets/brief-gadget', () => ({
  BriefGadget: ({ instance }: { instance: GadgetInstance }) => <div data-testid="brief">{instance.id}</div>,
}));

const RENDERERS = [
  'daily_brief', 'tasks', 'goals', 'timeline', 'entity', 'personal_context', 'news_feed', 'telegram_feed', 'feed',
  'text_panel', 'table_panel', 'job_list', 'video_panel', 'watch_rules', 'highlights', 'finance_chart',
  'metrics_chart', 'github_project', 'weather', 'provider_observation', 'map', 'intelligence_panel',
];

describe('lazy gadget registry', () => {
  it('registers every renderer id and rejects unknown ids', () => {
    for (const id of RENDERERS) {
      expect(isGadgetRendererRegistered(id)).toBe(true);
      expect(resolveGadgetRenderer(id)).not.toBeNull();
    }
    expect(isGadgetRendererRegistered('nope')).toBe(false);
    expect(resolveGadgetRenderer('nope')).toBeNull();
  });

  it('shows the skeleton while the chunk loads, then the gadget with its instance', async () => {
    const Renderer = resolveGadgetRenderer('daily_brief')!;
    const instance = { id: 'inst-1' } as GadgetInstance;
    const { container } = render(<Renderer instance={instance} isEditMode={false} />);
    expect(container.querySelector('.skeleton')).not.toBeNull();
    expect(await screen.findByTestId('brief')).toHaveTextContent('inst-1');
  });
});
