import { act, renderHook } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { messages } from '@/core/messages';

const upload = vi.hoisted(() => vi.fn());
vi.mock('./api', async (orig) => ({ ...(await orig<typeof import('./api')>()), uploadChatAttachment: upload, getChatAttachment: vi.fn() }));

import { useChatAttachments } from './chat-attachments';

const wrapper = ({ children }: { children: ReactNode }) => (
  <NextIntlClientProvider locale="en-US" messages={messages['en-us']}>
    {children}
  </NextIntlClientProvider>
);
const file = (name: string) => new File(['x'], name, { type: 'text/plain' });

describe('attachment sharing choice (C5)', () => {
  beforeEach(() => {
    upload.mockReset();
    upload.mockResolvedValue({ document_id: 'd', status: 'ready', local_only: false, source_id: 's', document_version_id: 'v' });
  });

  it('holds the first file and blocks Send until the owner answers', () => {
    const { result } = renderHook(() => useChatAttachments('csrf'), { wrapper });
    act(() => result.current.add([file('a.txt')]));
    expect(upload).not.toHaveBeenCalled();
    expect(result.current.pendingShare).toHaveLength(1);
    expect(result.current.sendBlock).toBe('Let the AI model read your attached files?');
  });

  it('uploads the held file with the answer and remembers it for later files, even after a send clears the bar', () => {
    const { result } = renderHook(() => useChatAttachments('csrf'), { wrapper });
    act(() => result.current.add([file('a.txt')]));
    act(() => result.current.resolveShare(true));
    expect(upload).toHaveBeenLastCalledWith(expect.any(File), 'csrf', true, expect.anything());
    act(() => result.current.clear());
    act(() => result.current.add([file('b.txt')]));
    expect(result.current.pendingShare).toHaveLength(0);
    expect(upload).toHaveBeenLastCalledWith(expect.any(File), 'csrf', true, expect.anything());
  });

  it('asks again after the choice is reset (new conversation)', () => {
    const { result } = renderHook(() => useChatAttachments('csrf'), { wrapper });
    act(() => result.current.add([file('a.txt')]));
    act(() => result.current.resolveShare(false));
    expect(upload).toHaveBeenLastCalledWith(expect.any(File), 'csrf', false, expect.anything());
    act(() => result.current.resetShareChoice());
    act(() => result.current.add([file('b.txt')]));
    expect(result.current.pendingShare).toHaveLength(1);
  });
});
