import { describe, expect, it } from 'vitest';
import { buildStandaloneHtmlDocument } from '../sandbox/export-utils';
import { buildFrameDocument, SANDBOX_BRIDGE_JS } from '../sandbox/frame-content';

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

  it('ships the vendored import map before the bridge and runs widget JS without eval (host CSP has no unsafe-eval)', () => {
    const doc = buildFrameDocument({ html: '<p>x</p>' });
    expect(doc).toContain('<script type="importmap">');
    expect(doc.indexOf('type="importmap"')).toBeLessThan(doc.indexOf('__umweltNonce'));
    expect(SANDBOX_BRIDGE_JS).not.toMatch(/eval\s*\)?\s*\(/);
    expect(SANDBOX_BRIDGE_JS).toContain('runScript(data.jsFunctions)');
  });
});

describe('host CSP nonce on frame scripts', () => {
  it('tags trusted scripts with the host nonce but never model-authored body scripts', async () => {
    const { nonceScriptTags } = await import('../sandbox/frame-content');
    expect(nonceScriptTags('<script>a</script><script type="importmap">{}</script>', 'N')).toBe(
      '<script nonce="N">a</script><script nonce="N" type="importmap">{}</script>',
    );
    expect(nonceScriptTags('<script>a</script>', '')).toBe('<script>a</script>');
    document.head.innerHTML = '<meta name="csp-nonce" content="HOSTN">';
    const doc = buildFrameDocument({ html: '<script>evil()</script><p>x</p>', executeScripts: true, jsExpressions: 'go()' });
    document.head.innerHTML = '';
    expect(doc).toContain('<script nonce="HOSTN" id="umwelt-widget-js">');
    expect(doc).toContain('<script nonce="HOSTN" type="importmap">');
    expect(doc).not.toMatch(/nonce="HOSTN"[^>]*>\s*evil/);
  });
});
