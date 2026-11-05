import { act, renderHook } from '@testing-library/react';
import { render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';
import { sourcesMessages } from '@/core/messages/sources';
import { ProviderScope } from './provider-scope';
import { useIdListText } from './scope-fields';

describe('useIdListText', () => {
  it('keeps typed separators on our own echo but resyncs on external change and reset', () => {
    const { result, rerender } = renderHook(({ ids, epoch }) => useIdListText(ids, epoch), { initialProps: { ids: ['a', 'b'], epoch: 0 } });
    expect(result.current.value).toBe('a, b');
    let parsed: string[] = [];
    act(() => { parsed = result.current.edit('a, b, '); });
    expect(parsed).toEqual(['a', 'b']);
    rerender({ ids: parsed, epoch: 0 });
    expect(result.current.value).toBe('a, b, ');
    rerender({ ids: ['x'], epoch: 0 });
    expect(result.current.value).toBe('x');
    act(() => { result.current.edit('x, '); });
    rerender({ ids: ['x'], epoch: 1 });
    expect(result.current.value).toBe('x');
  });
});

describe('ProviderScope registry', () => {
  const wrap = (provider: 'bluesky' | 'notion', onChange = vi.fn()) => render(
    <NextIntlClientProvider locale="en-US" messages={{ sources: sourcesMessages['en-us'] }}>
      <ProviderScope provider={provider} configuration={{} as never} disabled={false} resetEpoch={0} onChange={onChange} />
    </NextIntlClientProvider>);

  it('keeps the Bluesky handle pattern with single backslash escape', () => {
    wrap('bluesky');
    const input = document.getElementById('source-scope-bluesky_actor') as HTMLInputElement;
    expect(input.getAttribute('pattern')).toContain('{0,61}[A-Za-z0-9])?\\.)+');
    expect(input.getAttribute('aria-describedby')).toBe('source-hint-bluesky_actor');
  });

  it('emits every parsed Notion page id while typing', async () => {
    const onChange = vi.fn();
    wrap('notion', onChange);
    await userEvent.type(document.getElementById('source-scope-notion_page_ids')!, 'p1, p2');
    expect(onChange).toHaveBeenLastCalledWith('notion_page_ids', ['p1', 'p2']);
    expect(document.getElementById('source-scope-notion-help')).not.toBeNull();
  });
});
