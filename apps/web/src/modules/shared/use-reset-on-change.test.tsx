import { act, renderHook } from '@testing-library/react';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';
import { useResetOnChange } from './use-reset-on-change';

/** Test harness: `draft` is edited locally and reset to `source` whenever `source` changes. */
function useDraft(source: string) {
  const [draft, setDraft] = useState('initial');
  useResetOnChange([source], () => setDraft(source));
  return { draft, setDraft };
}

describe('useResetOnChange', () => {
  it('applies the reset on mount and when a dependency changes, but not on unrelated re-renders', () => {
    const { result, rerender } = renderHook(({ source }) => useDraft(source), { initialProps: { source: 'a' } });
    expect(result.current.draft).toBe('a');
    act(() => result.current.setDraft('edited'));
    rerender({ source: 'a' });
    expect(result.current.draft).toBe('edited');
    rerender({ source: 'b' });
    expect(result.current.draft).toBe('b');
  });
});
