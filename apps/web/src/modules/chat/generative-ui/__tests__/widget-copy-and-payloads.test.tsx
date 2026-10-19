import * as React from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { chatMessages } from '@/core/messages/chat';
import { PromptBridgeProvider, usePromptBridge } from '../bridge';
import { ToolRendering } from '../components/tool-rendering';
import type { GenerativeUIPayload } from '../sandbox/schema';
import { buildStandaloneHtmlDocument, escapeHtml } from '../sandbox/export-utils';

afterEach(() => cleanup());

describe('ToolRendering payload validation', () => {
  const bad: Array<[string, unknown]> = [
    ['plan', { kind: 'plan', plan: { steps: 'nope' } }],
    ['table', { kind: 'table', table: { columns: 'x' } }],
    ['bar_chart', { kind: 'bar_chart', chart: { data: 'x' } }],
    ['pie_chart', { kind: 'pie_chart', chart: { data: [1, 2] } }],
    ['meeting_time_picker', { kind: 'meeting_time_picker', form: { slots: [{ nope: 1 }] } }],
    ['sandboxed_ui', { kind: 'sandboxed_ui', sandbox: { html: 42 } }],
  ];

  it.each(bad)('renders a localized role=alert for an invalid %s payload without throwing', (_kind, payload) => {
    render(<ToolRendering payload={payload as GenerativeUIPayload} locale="vi-vi" />);
    const alert = screen.getByRole('alert');
    expect(alert.textContent).toBe(chatMessages['vi-vi'].genUiInvalidPayload);
  });

  it('still renders a valid table', () => {
    render(
      <ToolRendering
        payload={{ kind: 'table', table: { title: 'T', columns: [{ key: 'a', label: 'A' }], rows: [{ a: 1 }] } }}
        locale="en-us"
      />,
    );
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByTestId('generative-table-catalog')).toBeTruthy();
  });
});

describe('export-utils escaping', () => {
  it('escapes single quotes and neutralizes the title in the standalone document', () => {
    expect(escapeHtml(`a'b"c<d>&`)).toBe('a&#39;b&quot;c&lt;d&gt;&amp;');
    const doc = buildStandaloneHtmlDocument({ html: '<p>x</p>' }, `x'><script>alert(1)</script>`);
    expect(doc).toContain('<title>x&#39;&gt;&lt;script&gt;');
    expect(doc).not.toContain('<title>x\'><script>');
  });
});

describe('genUi i18n parity', () => {
  it('en-us and vi-vi expose identical genUi*/markdown* keys', () => {
    const pick = (l: 'en-us' | 'vi-vi') =>
      Object.keys(chatMessages[l]).filter((k) => k.startsWith('genUi') || k.startsWith('markdown')).sort();
    expect(pick('vi-vi')).toEqual(pick('en-us'));
    // Placeholders must match too, so interpolation never drops a variable in one locale.
    for (const key of pick('en-us')) {
      const ph = (l: 'en-us' | 'vi-vi') =>
        ((chatMessages[l] as unknown as Record<string, string>)[key].match(/\{\w+\}/g) ?? []).sort();
      expect(ph('vi-vi'), key).toEqual(ph('en-us'));
    }
  });
});

describe('PromptBridgeProvider rejection', () => {
  it('posts a retryable error back to the iframe when onSendPrompt rejects', async () => {
    const frame = { postMessage: vi.fn() };
    const onSendPrompt = vi.fn().mockRejectedValue(new Error('busy'));
    function Registrar() {
      const { registerFrame } = usePromptBridge();
      React.useEffect(() => registerFrame(frame, 'n1'), [registerFrame]);
      return null;
    }
    render(
      <PromptBridgeProvider onSendPrompt={onSendPrompt} cooldownMs={0}>
        <Registrar />
      </PromptBridgeProvider>,
    );
    window.dispatchEvent(
      new MessageEvent('message', {
        data: { type: 'umwelt:send-prompt', id: 'p7', prompt: 'hi', nonce: 'n1' },
        source: frame as unknown as Window,
        origin: 'null',
      }),
    );
    await vi.waitFor(() => expect(frame.postMessage).toHaveBeenCalledTimes(2));
    expect(frame.postMessage.mock.calls[1][0]).toEqual({
      type: 'umwelt:send-prompt-error',
      id: 'p7',
      retryable: true,
      message: 'busy',
      nonce: 'n1',
    });
  });
});
