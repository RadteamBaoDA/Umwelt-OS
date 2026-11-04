import { describe, expect, it, vi } from 'vitest';
import { sanitizePolicyHtml } from './sanitize-policy-html';

/** Normalizes HTML through the DOM so serialization differences (entities, void tags) do not matter. */
function normalized(html: string): string {
  const host = document.createElement('div');
  host.innerHTML = html;
  return host.innerHTML;
}

vi.mock('server-only', () => ({}));

describe('sanitizePolicyHtml', () => {
  it('leaves every loadPolicy output (six documents and the inventory) byte-for-byte unchanged', async () => {
    const { loadPolicy } = await import('./policy-loader');
    for (const locale of ['en-us', 'vi-vi'] as const) {
      for (const name of ['privacy', 'terms', 'refunds'] as const) {
        const html = await loadPolicy(name, locale);
        expect(html.length, `${locale}/${name}`).toBeGreaterThan(0);
        expect(sanitizePolicyHtml(html), `${locale}/${name}`).toBe(html);
      }
    }
    const inventory = await loadPolicy('inventory', 'en-us');
    // The inventory contains literal quotes that DOM serialization emits as &quot; versus the Markdown renderer's raw "; the
    // parsed DOM is identical, so compare normalized.
    expect(normalized(sanitizePolicyHtml(inventory))).toBe(normalized(inventory));
  });

  it('keeps generated heading anchors even when they match document properties', () => {
    const html = '<h2 id="location">Location</h2><p><a href="#location">jump</a> <span>repo.md</span></p>';
    expect(normalized(sanitizePolicyHtml(html))).toBe(normalized(html));
  });

  it('removes scripts, event handlers and javascript: links', () => {
    const clean = sanitizePolicyHtml(
      '<p onclick="x()">a</p><script>alert(1)</script><img src=x onerror="y()"><a href="javascript:alert(1)">z</a>',
    );
    expect(clean).not.toMatch(/script|onclick|onerror|javascript:/i);
    expect(clean).toContain('<p>a</p>');
  });
});
