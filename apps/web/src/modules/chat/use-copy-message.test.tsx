import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useCopyMessage } from './use-copy-message';

const writeText = vi.fn();

beforeEach(() => {
  vi.useFakeTimers();
  writeText.mockReset().mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
});
afterEach(() => vi.useRealTimers());

describe('useCopyMessage', () => {
  it('copies plain text, flags the message, and clears the flag after two seconds', async () => {
    const { result } = renderHook(() => useCopyMessage('c1'));

    await act(async () => result.current.copyMessage('m1', 'hello'));
    expect(writeText).toHaveBeenCalledWith('hello');
    expect(result.current.copiedMessageId).toBe('m1');

    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(result.current.copiedMessageId).toBeNull();
  });

  it('reports clipboard denial for the message', async () => {
    writeText.mockRejectedValue(new Error('denied'));
    const { result } = renderHook(() => useCopyMessage('c1'));

    await act(async () => result.current.copyMessage('m1', 'hello'));

    expect(result.current.failedMessageId).toBe('m1');
    expect(result.current.copiedMessageId).toBeNull();
  });

  it('moves the feedback to the latest message when copying twice', async () => {
    const { result } = renderHook(() => useCopyMessage('c1'));
    await act(async () => result.current.copyMessage('m1', 'a'));
    await act(async () => result.current.copyMessage('m2', 'b'));
    expect(result.current.copiedMessageId).toBe('m2');
  });

  it('drops copy feedback when the conversation changes', async () => {
    const { result, rerender } = renderHook(({ id }) => useCopyMessage(id), { initialProps: { id: 'c1' } });
    await act(async () => result.current.copyMessage('m1', 'a'));
    expect(result.current.copiedMessageId).toBe('m1');

    rerender({ id: 'c2' });

    expect(result.current.copiedMessageId).toBeNull();
  });
});
