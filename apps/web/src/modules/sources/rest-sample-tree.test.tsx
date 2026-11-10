import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

const request = vi.hoisted(() => vi.fn());
vi.mock('@/core/api', async (original) => ({ ...(await original<typeof import('@/core/api')>()), apiRequest: request }));
vi.mock('next-intl', async () => {
  const { sourcesMessages } = await import('@/core/messages/sources');
  const en = sourcesMessages['en-us'] as unknown as Record<string, string>;
  return { useTranslations: () => (key: string) => en[key] ?? key };
});

import { restSample } from './rest-sample';
import { pathForTarget, RestSampleTree, restSampleErrorMessage } from './rest-sample-tree';

const sample = { data: { items: [{ id: 1, title: 'A' }] } };

describe('RestSampleTree', () => {
  it('renders nested data and fills items_path with the full path', () => {
    const onFill = vi.fn();
    render(<RestSampleTree result={{ status_code: 200, truncated: false, sample }} itemsPath="" onFill={onFill} />);
    fireEvent.click(screen.getByRole('button', { name: 'items' }));
    expect(onFill).toHaveBeenCalledWith('items_path', 'data.items');
    expect(screen.getByText(/HTTP status: 200/)).toBeTruthy();
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('fills item fields relative to items_path and shows the truncated note', () => {
    const onFill = vi.fn();
    render(<RestSampleTree result={{ status_code: 200, truncated: true, sample }} itemsPath="data.items" onFill={onFill} />);
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'title_field' } });
    fireEvent.click(screen.getByRole('button', { name: 'title' }));
    expect(onFill).toHaveBeenCalledWith('title_field', 'title');
    expect(screen.getByRole('status')).toBeTruthy();
  });

  it('maps error codes to messages', () => {
    const t = (key: string) => key;
    expect(restSampleErrorMessage(t, { message: 'rest_sample_not_json' })).toBe('restError_rest_sample_not_json');
    expect(restSampleErrorMessage(t, { code: 'x', message: 'free text' })).toBe('restFallback');
    expect(pathForTarget('id_field', 'a.b', '')).toBe('a.b');
  });
});

describe('restSample', () => {
  it('POSTs the draft to the rest-sample endpoint', async () => {
    request.mockResolvedValue({ status_code: 200, truncated: false, sample: {} });
    const body = { expected_revision: 1, expected_source_generation: 2, configuration: {}, auth_method: 'none' } as never;
    await restSample('s1', body);
    expect(request).toHaveBeenCalledWith('/api/v1/connectors/s1/rest-sample', expect.objectContaining({ method: 'POST', body: JSON.stringify(body) }));
  });
});
