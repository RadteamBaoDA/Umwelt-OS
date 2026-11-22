import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { useSkin } from './use-skin';

describe('useSkin', () => {
  afterEach(() => document.documentElement.removeAttribute('data-skin'));

  it('follows data-skin changes on <html>', async () => {
    const { result } = renderHook(() => useSkin());
    expect(result.current).toBe('default');
    await act(async () => {
      document.documentElement.setAttribute('data-skin', 'terminal');
      await Promise.resolve();
    });
    expect(result.current).toBe('terminal');
    await act(async () => {
      document.documentElement.removeAttribute('data-skin');
      await Promise.resolve();
    });
    expect(result.current).toBe('default');
  });
});
