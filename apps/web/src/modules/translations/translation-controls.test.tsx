import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';
import { CollectionTranslationBar, ItemTranslateButton, TranslateActionButton } from './translation-controls';
import type { ItemTranslationController } from './use-item-translation';

/** Builds a minimal item controller; only the fields ItemTranslateButton reads are meaningful. */
function controller(overrides: Partial<ItemTranslationController> = {}): ItemTranslationController {
  return {
    enabled: true,
    targetLanguage: 'vi',
    results: new Map(),
    issues: new Map(),
    unchanged: new Set(),
    pendingIds: new Set(),
    translate: vi.fn(),
    targets: [],
    showOriginal: false,
    toggleAllOriginal: vi.fn(),
    isShowingOriginal: () => false,
    toggleItemOriginal: vi.fn(),
    shown: () => undefined,
    translateOne: vi.fn(),
    ...overrides,
  } as ItemTranslationController;
}

/** Renders the button inside the en-US intl provider the app uses. */
function renderButton(c: ItemTranslationController, title?: string) {
  return render(
    <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
      <ItemTranslateButton controller={c} target={{ id: 'd1', revision: 'r1' }} title={title} />
    </NextIntlClientProvider>,
  );
}

describe('ItemTranslateButton', () => {
  it('renders nothing when translation is disabled', () => {
    const { container } = renderButton(controller({ enabled: false }));
    expect(container).toBeEmptyDOMElement();
  });

  it('flips aria-pressed between original and translation when a translation is ready', async () => {
    const c = controller({ results: new Map([['d1', { status: 'ready', translation: { title: 'x' } }]]) });
    renderButton(c);
    const button = screen.getByRole('button');
    expect(button).toHaveAttribute('aria-pressed', 'true');
    await userEvent.click(button);
    expect(c.toggleItemOriginal).toHaveBeenCalledWith('d1');
  });

  it('shows the already-in-target-language note instead of a button when unchanged', () => {
    renderButton(controller({ unchanged: new Set(['d1']) }));
    expect(screen.queryByRole('button')).toBeNull();
    expect(screen.getByText('Already in the target language')).toBeInTheDocument();
  });

  it('includes the item title in the accessible name', () => {
    renderButton(controller(), 'Market open');
    expect(screen.getByRole('button', { name: /Market open/ })).toBeInTheDocument();
  });

  it('renders a note with a live Translate button for a still_queued issue', () => {
    renderButton(controller({ issues: new Map([['d1', { status: 'failed', translation: null, errorCode: 'still_queued' }]]) }));
    expect(screen.getByText('Still translating in the background. Try again in a moment.')).toBeInTheDocument();
    expect(screen.getByRole('button')).toBeEnabled();
  });
});

describe('TranslateActionButton pending state', () => {
  it('marks the button busy and renders the shared Spinner while pending', () => {
    render(
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <TranslateActionButton label="Translate" pending onClick={vi.fn()} />
      </NextIntlClientProvider>,
    );
    const button = screen.getByRole('button');
    expect(button).toHaveAttribute('aria-busy', 'true');
    expect(button.querySelector('.animate-spin')).not.toBeNull();
    expect(button.querySelector('.spinner-glyph')).not.toBeNull();
  });
});

describe('CollectionTranslationBar progress', () => {
  /** Renders the collection bar in the en-US provider with a controller that has a live run. */
  function bar(c: ItemTranslationController) {
    return (
      <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
        <CollectionTranslationBar controller={c} />
      </NextIntlClientProvider>
    );
  }
  const readyResults = new Map([0, 1, 2].map((i) => [`d${i}`, { status: 'ready' as const, translation: { title: 'x' } }]));

  it('shows only the original/translation toggle once an item is ready', () => {
    const { container, rerender } = render(bar(controller()));
    expect(container).toBeEmptyDOMElement();
    rerender(bar(controller({ results: readyResults })));
    expect(screen.getByRole('button')).toBeInTheDocument();
    expect(screen.queryByRole('progressbar')).toBeNull();
  });
});
