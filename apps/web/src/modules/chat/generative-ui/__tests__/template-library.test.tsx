import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import {
  GenerativeUIStarterGallery,
  SEED_TEMPLATES,
  TEMPLATE_CATEGORIES,
  TemplateChip,
} from '../templates';

describe('Generative UI Starter Templates & Gallery', () => {
  it('provides bilingual seed templates across all five required categories', () => {
    const categoriesPresent = new Set(SEED_TEMPLATES.map((t) => t.category));
    for (const category of TEMPLATE_CATEGORIES) {
      expect(categoriesPresent.has(category)).toBe(true);
    }

    for (const template of SEED_TEMPLATES) {
      expect(template.title['en-us'].length).toBeGreaterThan(0);
      expect(template.title['vi-vi'].length).toBeGreaterThan(0);
      expect(template.description['en-us'].length).toBeGreaterThan(0);
      expect(template.description['vi-vi'].length).toBeGreaterThan(0);
      expect(template.prompt['en-us'].length).toBeGreaterThan(0);
      expect(template.prompt['vi-vi'].length).toBeGreaterThan(0);
    }
  });

  it('filters templates by category in GenerativeUIStarterGallery and dispatches prompts on click', async () => {
    const user = userEvent.setup();
    const onSelectPrompt = vi.fn();

    render(
      <GenerativeUIStarterGallery
        onSelectPrompt={onSelectPrompt}
        locale="en-us"
      />,
    );

    expect(screen.getByTestId('generative-ui-starter-gallery')).toBeInTheDocument();
    expect(screen.getByTestId('generative-explainer-cards')).toBeInTheDocument();

    // Filter to 'planning' category only
    await user.click(screen.getByTestId('category-filter-planning'));
    expect(screen.getByTestId('template-card-tpl-research-plan')).toBeInTheDocument();
    expect(screen.queryByTestId('template-card-tpl-architecture-diagram')).not.toBeInTheDocument();

    // Click the template action button
    await user.click(screen.getByTestId('template-card-action-tpl-research-plan'));
    expect(onSelectPrompt).toHaveBeenCalledTimes(1);
    expect(onSelectPrompt.mock.calls[0][0]).toContain('step-by-step execution plan');
  });

  it('renders TemplateChip in Vietnamese and supports clear action', async () => {
    const user = userEvent.setup();
    const onSelectPrompt = vi.fn();
    const onClear = vi.fn();

    render(
      <TemplateChip
        template={SEED_TEMPLATES[0]}
        locale="vi-vi"
        onSelectPrompt={onSelectPrompt}
        onClear={onClear}
      />,
    );

    expect(screen.getByText(SEED_TEMPLATES[0].title['vi-vi'])).toBeInTheDocument();
    await user.click(screen.getByText(SEED_TEMPLATES[0].title['vi-vi']));
    expect(onSelectPrompt).toHaveBeenCalledWith(SEED_TEMPLATES[0].prompt['vi-vi']);

    await user.click(screen.getByTestId('template-chip-clear'));
    expect(onClear).toHaveBeenCalledTimes(1);
  });
});

