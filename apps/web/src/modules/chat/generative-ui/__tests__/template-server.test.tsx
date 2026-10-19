import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { TemplateLibrary } from '../templates/template-library';
import { clearUserTemplates, deleteUserTemplate, listUserTemplates, refreshUserTemplates, saveUserTemplate, setTemplateAccount } from '../templates/template-store';

const server = (id: string, name: string) => ({ id, name, description: '', html: '{"html":"x"}', data_description: '', version: 1, created_at: '2026-10-19T00:00:00Z' });
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

beforeEach(() => {
  window.localStorage.clear();
  setTemplateAccount('1');
});
afterEach(() => vi.unstubAllGlobals());

describe('server-backed template store', () => {
  it('migrates browser-only templates once and mirrors the server list (idempotent)', async () => {
    saveUserTemplate('Old', '{"html":"x"}');
    const posted: string[] = [];
    let stored: ReturnType<typeof server>[] = [];
    vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        const body = JSON.parse(String(init.body));
        posted.push(body.name);
        stored = [server('11111111-1111-1111-1111-111111111111', body.name)];
        return json(stored[0], 201);
      }
      return json(stored);
    }));
    await refreshUserTemplates('tok');
    await refreshUserTemplates('tok');
    expect(posted).toEqual(['Old']);
    expect(listUserTemplates().map((t) => t.id)).toEqual(['11111111-1111-1111-1111-111111111111']);
  });

  it('keeps the browser copy when the API is unreachable', async () => {
    saveUserTemplate('Local', '{"html":"x"}');
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')));
    await refreshUserTemplates('tok');
    expect(listUserTemplates().map((t) => t.title)).toEqual(['Local']);
  });

  it('save with a token posts in the background and swaps the local id; delete calls the API for server ids', async () => {
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) =>
      init?.method === 'POST' ? json(server('22222222-2222-2222-2222-222222222222', 'Mine'), 201) : new Response(null, { status: 204 }));
    vi.stubGlobal('fetch', fetchMock);
    expect(saveUserTemplate('Mine', '{"html":"x"}', 'tok')).toBe(true);
    await waitFor(() => expect(listUserTemplates()[0].id).toBe('22222222-2222-2222-2222-222222222222'));
    expect(new Headers(fetchMock.mock.calls[0][1]?.headers).get('X-CSRF-Token')).toBe('tok');
    deleteUserTemplate('22222222-2222-2222-2222-222222222222', 'tok');
    expect(listUserTemplates()).toEqual([]);
    await waitFor(() => expect(fetchMock.mock.calls.at(-1)?.[1]?.method).toBe('DELETE'));
  });
});

describe('per-account template mirror', () => {
  it('never shows or uploads account A entries to account B, and drops the legacy key', async () => {
    window.localStorage.setItem('umwelt.genui.templates.v1', JSON.stringify([
      { id: 'tpl-legacy', title: 'Legacy', content: '{}', createdAt: 1 },
    ]));
    setTemplateAccount('2'); // re-bind so the legacy drop runs
    setTemplateAccount('1');
    saveUserTemplate('A secret', '{"html":"a"}');
    expect(listUserTemplates().map((t) => t.title)).toEqual(['A secret']);

    setTemplateAccount('2');
    expect(listUserTemplates()).toEqual([]);
    const posted: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === 'POST') posted.push(JSON.parse(String(init.body)).name);
      return json([]);
    }));
    await refreshUserTemplates('tok-b');
    expect(posted).toEqual([]);
    expect(listUserTemplates()).toEqual([]);
    expect(window.localStorage.getItem('umwelt.genui.templates.v1')).toBeNull();

    setTemplateAccount('1');
    expect(listUserTemplates().map((t) => t.title)).toEqual(['A secret']);
  });

  it('clears the mirror on logout and stays empty until an account is bound', () => {
    saveUserTemplate('Keep?', '{"html":"a"}');
    clearUserTemplates();
    expect(listUserTemplates()).toEqual([]);
    expect(saveUserTemplate('x', '{}')).toBe(false);
    expect(window.localStorage.getItem('umwelt.genui.templates.v2:1')).toBeNull();
  });
});

describe('TemplateLibrary pending template', () => {
  it('selects the template server-side and sends the short apply prompt', async () => {
    const user = userEvent.setup();
    const onSendPrompt = vi.fn();
    const fetchMock = vi.fn(async (...args: [string, RequestInit?]) => (args[0].endsWith('/pending-template') ? json({ template_id: 'seed-weather-ny' }) : json([])));
    vi.stubGlobal('fetch', fetchMock);
    render(<TemplateLibrary onSendPrompt={onSendPrompt} locale="en-us" csrfToken="tok" conversationId="c1" />);
    await user.click(screen.getByTestId('template-library-open'));
    await user.click(screen.getAllByRole('button', { name: 'Use template' })[0]);
    await waitFor(() => expect(onSendPrompt).toHaveBeenCalledTimes(1));
    const put = fetchMock.mock.calls.find(([u]) => String(u).endsWith('/pending-template'))!;
    expect(String(put[0])).toBe('/api/v1/conversations/c1/pending-template');
    expect(JSON.parse(String((put[1] as RequestInit).body))).toEqual({ template_id: 'seed-weather-ny' });
    expect(onSendPrompt.mock.calls[0][0]).toContain('apply_template');
    expect(onSendPrompt.mock.calls[0][0]).not.toContain('Template JSON');
  });

  it('falls back to the inline JSON prompt when the pending call fails or there is no conversation', async () => {
    const user = userEvent.setup();
    const onSendPrompt = vi.fn();
    vi.stubGlobal('fetch', vi.fn(async (url: string) => (url.endsWith('/pending-template') ? json({ detail: 'x' }, 404) : json([]))));
    const { unmount } = render(<TemplateLibrary onSendPrompt={onSendPrompt} locale="en-us" csrfToken="tok" conversationId="c1" />);
    await user.click(screen.getByTestId('template-library-open'));
    await user.click(screen.getAllByRole('button', { name: 'Use template' })[0]);
    await waitFor(() => expect(onSendPrompt).toHaveBeenCalledTimes(1));
    expect(onSendPrompt.mock.calls[0][0]).toContain('Template JSON');
    unmount();
    const second = vi.fn();
    render(<TemplateLibrary onSendPrompt={second} locale="en-us" csrfToken="tok" conversationId={null} />);
    await user.click(screen.getByTestId('template-library-open'));
    await user.click(screen.getAllByRole('button', { name: 'Use template' })[0]);
    await waitFor(() => expect(second).toHaveBeenCalledTimes(1));
    expect(second.mock.calls[0][0]).toContain('Template JSON');
  });
});
