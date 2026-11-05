import { describe, expect, it } from 'vitest';
import { buildStandaloneHtmlDocument } from '../sandbox/export-utils';
import { buildCspMetaTag, buildFrameDocument, buildFrameShellCsp, frameDocumentProps, SANDBOX_BRIDGE_JS } from '../sandbox/frame-content';

describe('sandbox frame nonce + export origin', () => {
  it('embeds the nonce in srcdoc and gates the in-frame listener on source and nonce', () => {
    const doc = buildFrameDocument({ html: '<p>x</p>', nonce: 'abc-123' });
    expect(doc).toContain('window.__umweltNonce="abc-123"');
    expect(SANDBOX_BRIDGE_JS).toContain('event.source !== window.parent');
    expect(SANDBOX_BRIDGE_JS).toContain('data.nonce !== NONCE');
    expect(SANDBOX_BRIDGE_JS).toContain('umwelt:send-prompt-result');
    expect(SANDBOX_BRIDGE_JS).toContain('umwelt:send-prompt-error');
    expect(SANDBOX_BRIDGE_JS).toContain('umwelt:send-prompt-ack');
  });

  it('generates distinct nonces when none is supplied', () => {
    const nonce = (d: string) => /__umweltNonce="([^"]+)"/.exec(d)?.[1];
    expect(nonce(buildFrameDocument())).not.toBe(nonce(buildFrameDocument()));
  });

  it('exports an import map with absolute app-origin URLs', () => {
    const doc = buildStandaloneHtmlDocument({ html: '<p>x</p>' }, 'T');
    expect(doc).toContain(`${window.location.origin}/vendor/genui/`);
  });

  it('ships the vendored import map before the bridge and runs widget JS without eval (frame CSP has no unsafe-eval)', () => {
    const doc = buildFrameDocument({ html: '<p>x</p>' });
    expect(doc).toContain('<script type="importmap">');
    expect(doc.indexOf('type="importmap"')).toBeLessThan(doc.indexOf('__umweltNonce'));
    expect(SANDBOX_BRIDGE_JS).not.toMatch(/eval\s*\)?\s*\(/);
    expect(SANDBOX_BRIDGE_JS).toContain('runScript(data.jsFunctions)');
  });
});

describe('frame document delivery and CSP', () => {
  it('never embeds a host nonce and keeps widget scripts un-nonced (frame CSP allows unsafe-inline itself)', () => {
    document.head.innerHTML = '<meta name="csp-nonce" content="HOSTN">';
    const doc = buildFrameDocument({ html: '<p>x</p>', executeScripts: true, jsExpressions: 'go()' });
    document.head.innerHTML = '';
    expect(doc).not.toContain('HOSTN');
    expect(doc).not.toMatch(/<script nonce=/);
    expect(doc).toContain('<script id="umwelt-widget-js">');
  });

  it('loads documents through the shell route (src + name), never srcdoc, so the app CSP is not inherited', () => {
    expect(frameDocumentProps('<p>doc</p>')).toEqual({ src: '/genui-frame', name: '<p>doc</p>' });
  });

  it('shell header CSP is at least as strict as the in-document meta policy and only frameable by the app', () => {
    const csp = buildFrameShellCsp('https://app.test');
    const meta = /content="([^"]+)"/.exec(buildCspMetaTag('https://app.test'))![1];
    expect(csp).toBe(`${meta}; frame-ancestors 'self'`);
    expect(csp).toContain("default-src 'none'");
    expect(csp).toContain("connect-src 'none'");
    expect(csp).toContain("script-src 'unsafe-inline' https://app.test/vendor/genui/");
  });
});
