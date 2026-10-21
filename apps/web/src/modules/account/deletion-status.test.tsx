import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { accountDeletionMessages } from '@/core/messages/account-deletion';
import { AccountDeletionStatus } from './deletion-status';

afterEach(() => vi.unstubAllGlobals());

function mount(locale: 'en-us' | 'vi-vi' = 'en-us') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><NextIntlClientProvider locale={locale === 'en-us' ? 'en-US' : 'vi-VN'} messages={{ accountDeletion: accountDeletionMessages[locale] }}><AccountDeletionStatus /></NextIntlClientProvider></QueryClientProvider>);
}

describe('AccountDeletionStatus', () => {
  it('shows a safe cleanup stage and failure category using only the receipt endpoint', async () => {
    const fetcher = vi.fn().mockResolvedValue(Response.json({ status: 'failed', phase: 'graph', error_code: 'graph_cleanup_failed' }));
    vi.stubGlobal('fetch', fetcher);
    mount();
    expect(await screen.findByText('Current step: Removing graph data')).toBeInTheDocument();
    expect(screen.getByText('Cleanup issue: Graph cleanup needs a retry')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('The account stays locked');
    expect(fetcher).toHaveBeenCalledWith('/api/v1/auth/account/deletion-status', expect.objectContaining({ cache: 'no-store', credentials: 'same-origin' }));
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('does not display unknown phases or internal failure values', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ status: 'failed', phase: 'private@example.com', error_code: 'database password=secret' })));
    mount();
    expect(await screen.findByText('Current step: Progress details unavailable')).toBeInTheDocument();
    expect(screen.getByText('Cleanup issue: Cleanup needs a retry or host operator assistance')).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent('private@example.com');
    expect(document.body).not.toHaveTextContent('password=secret');
  });

  it('localizes Vietnamese progress and failure categories', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ status: 'failed', phase: 'storage', error_code: 'storage_cleanup_failed' })));
    mount('vi-vi');
    expect(await screen.findByText('Bước hiện tại: Đang xóa tệp đã tải lên')).toBeInTheDocument();
    expect(screen.getByText('Vấn đề khi dọn dữ liệu: Cần thử lại việc xóa tệp đã tải lên')).toBeInTheDocument();
  });

  it('tells a browser without its receipt to request host recovery without implying completion', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 404 })));
    mount();
    expect(await screen.findByRole('alert')).toHaveTextContent('No valid deletion receipt');
    expect(screen.queryByText('App-controlled account data has been erased.')).not.toBeInTheDocument();
  });
});
