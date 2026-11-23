import { render } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { getCatalog } from '@locales';
import { GadgetActionsProvider, useTranslateAllAction, type GadgetAction } from './gadget-actions';

/** Renderer that hands a fresh translateAll callback on every render, like `useItemTranslation` does. */
function Renderer({ onTranslateAll, untranslated, enabled = true }: { onTranslateAll: () => void; untranslated: number; enabled?: boolean }) {
  useTranslateAllAction({ enabled, pendingCount: 0, untranslatedCount: untranslated, progress: null, onTranslateAll: () => onTranslateAll() });
  return null;
}

/** Renders the renderer inside an actions provider and the translations catalog. */
function renderWith(report: (actions: readonly GadgetAction[]) => void, onTranslateAll: () => void, untranslated: number, enabled = true) {
  return (
    <NextIntlClientProvider locale="en-US" messages={{ translations: getCatalog('en-us', 'translations') }}>
      <GadgetActionsProvider report={report}>
        <Renderer onTranslateAll={onTranslateAll} untranslated={untranslated} enabled={enabled} />
      </GadgetActionsProvider>
    </NextIntlClientProvider>
  );
}

describe('useTranslateAllAction', () => {
  it('publishes once across re-renders with new callbacks and runs the latest callback', () => {
    const report = vi.fn();
    const first = vi.fn();
    const second = vi.fn();
    const view = render(renderWith(report, first, 3));
    view.rerender(renderWith(report, second, 3));
    expect(report).toHaveBeenCalledTimes(1);
    const [actions] = report.mock.calls[0] as [GadgetAction[]];
    expect(actions[0]).toMatchObject({ id: 'translate-all', label: 'Translate all', disabled: false });
    actions[0].onSelect();
    expect(second).toHaveBeenCalledTimes(1);
    expect(first).not.toHaveBeenCalled();
  });

  it('disables the entry when nothing is left and clears the menu on unmount', () => {
    const report = vi.fn();
    const view = render(renderWith(report, vi.fn(), 0));
    expect((report.mock.calls[0][0] as GadgetAction[])[0].disabled).toBe(true);
    view.unmount();
    expect(report).toHaveBeenLastCalledWith([]);
  });

  it('publishes no entry while workspace translation is off', () => {
    const report = vi.fn();
    render(renderWith(report, vi.fn(), 3, false));
    expect(report).toHaveBeenCalledWith([]);
  });
});
