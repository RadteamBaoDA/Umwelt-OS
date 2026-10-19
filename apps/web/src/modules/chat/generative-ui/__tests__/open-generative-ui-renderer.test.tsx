import * as React from 'react';
import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  CANONICAL_STREAM_KEYS,
  clampReportedHeight,
  getStreamingStage,
  OpenGenerativeUIRenderer,
} from '../sandbox';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('OpenGenerativeUIRenderer', () => {
  it('exports the canonical stream key ordering and streaming stage helper', () => {
    expect(CANONICAL_STREAM_KEYS).toEqual([
      'initialHeight',
      'placeholderMessages',
      'css',
      'html',
      'jsFunctions',
      'jsExpressions',
    ]);
    expect(getStreamingStage({}, 'streaming')).toBe('placeholder');
    expect(getStreamingStage({ css: '.card {}' }, 'streaming')).toBe('css');
    expect(getStreamingStage({ css: '.card {}', html: '<div>Hi</div>' }, 'streaming')).toBe(
      'html',
    );
    expect(
      getStreamingStage(
        { css: '.card {}', html: '<div>Hi</div>', jsExpressions: 'init();' },
        'streaming',
      ),
    ).toBe('js');
    expect(getStreamingStage({ html: '<div>Hi</div>' }, 'completed')).toBe('completed');
  });

  it('enforces sandbox="allow-scripts" and never includes allow-same-origin', () => {
    render(
      <OpenGenerativeUIRenderer
        status="completed"
        args={{
          title: 'Compound Interest Simulator',
          initialHeight: 380,
          css: '.box { padding: 12px; }',
          html: '<div class="box">Result: $12,400</div>',
        }}
      />,
    );

    const iframe = screen.getByTestId('sandboxed-ui-iframe') as HTMLIFrameElement;
    expect(iframe.getAttribute('sandbox')).toBe('allow-scripts');
    expect(iframe.getAttribute('sandbox')).not.toContain('allow-same-origin');
    expect(iframe.getAttribute('srcdoc')).toContain('Content-Security-Policy');
    expect(iframe.getAttribute('srcdoc')).toContain('Result: $12,400');
  });

  it('rotates progressive placeholderMessages while status === "streaming" and defers JS until "completed"', () => {
    vi.useFakeTimers();

    const { rerender } = render(
      <OpenGenerativeUIRenderer
        status="streaming"
        placeholderIntervalMs={1000}
        args={{
          initialHeight: 300,
          placeholderMessages: ['Analyzing portfolio...', 'Rendering compound curves...'],
          css: '.chart { color: var(--accent); }',
          html: '<div class="chart">Streaming chart',
          jsFunctions: 'function computeCurve() { return 42; }',
          jsExpressions: 'computeCurve();',
        }}
      />,
    );

    expect(screen.getByTestId('sandboxed-ui-placeholder').textContent).toContain(
      'Analyzing portfolio...',
    );

    act(() => {
      vi.advanceTimersByTime(1100);
    });

    expect(screen.getByTestId('sandboxed-ui-placeholder').textContent).toContain(
      'Rendering compound curves...',
    );

    const streamingIframe = screen.getByTestId('sandboxed-ui-iframe') as HTMLIFrameElement;
    // JS must NOT be embedded in srcdoc while streaming
    expect(streamingIframe.getAttribute('srcdoc')).not.toContain('computeCurve()');
    // Unclosed <div class="chart"> is balanced by processPartialHtml
    expect(streamingIframe.getAttribute('srcdoc')).toContain(
      '<div class="chart">Streaming chart</div>',
    );

    // Transition to completed -> executes JS and hides streaming placeholder
    rerender(
      <OpenGenerativeUIRenderer
        status="completed"
        args={{
          initialHeight: 300,
          placeholderMessages: ['Analyzing portfolio...', 'Rendering compound curves...'],
          css: '.chart { color: var(--accent); }',
          html: '<div class="chart">Completed chart</div>',
          jsFunctions: 'function computeCurve() { return 42; }',
          jsExpressions: 'computeCurve();',
        }}
      />,
    );

    expect(screen.queryByTestId('sandboxed-ui-placeholder')).toBeNull();
    expect(streamingIframe.getAttribute('srcdoc')).toContain('computeCurve()');
    expect(streamingIframe.dataset.scriptsExecuted).toBe('true');
  });

  it('updates container height on umwelt:resize postMessage and opens fullscreen preview via ExportOverlay', async () => {
    render(
      <OpenGenerativeUIRenderer
        status="completed"
        args={{
          title: 'Interactive Diagram',
          initialHeight: 280,
          html: '<div>Diagram Nodes</div>',
        }}
      />,
    );

    const container = screen.getByTestId('sandboxed-ui-container');
    const iframe = screen.getByTestId('sandboxed-ui-iframe') as HTMLIFrameElement;
    expect(container.style.height).toBe('280px');

    act(() => {
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { type: 'umwelt:resize', height: 540 },
          source: iframe.contentWindow,
        }),
      );
    });

    expect(container.style.height).toBe('540px');
    expect(clampReportedHeight(99999)).toBe(4000);
    expect(clampReportedHeight(10)).toBe(50);
    expect(clampReportedHeight(Number.NaN)).toBeNull();

    // Open fullscreen preview modal
    const fullscreenBtn = screen.getByTestId('generative-ui-fullscreen-btn');
    await userEvent.click(fullscreenBtn);

    const modalIframe = await screen.findByTestId('generative-ui-fullscreen-iframe');
    expect(modalIframe.getAttribute('sandbox')).toBe('allow-scripts');
  });
});
