import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { loadPolicy, policyRoot } from './policy-loader';

describe('public policy loader', () => {
  it('finds canonical documents from web and standalone working directories', () => {
    const root = path.resolve(process.cwd(), '../..');
    expect(policyRoot(path.join(root, 'apps/web'))).toBe(path.join(root, 'docs/legal'));
    expect(policyRoot(root)).toBe(path.join(root, 'docs/legal'));
  });

  it('renders both languages and inventory without accepting arbitrary paths', async () => {
    expect(await loadPolicy('privacy', 'en-us')).toContain('Privacy notice');
    expect(await loadPolicy('privacy', 'vi-vi')).toContain('Thông báo quyền riêng tư');
    expect(await loadPolicy('inventory', 'en-us')).toContain('Services and data flows');
    await expect(loadPolicy('../privacy' as never, 'en-us')).rejects.toThrow('Unknown legal document');
    await expect(loadPolicy('privacy', '../../secrets' as never)).rejects.toThrow('Unknown legal document');
  });

  it('resolves every inventory fragment and renders all six table cells', async () => {
    const html = await loadPolicy('inventory', 'en-us');
    const fragments = [...html.matchAll(/href="#([^"]+)"/g)].map((match) => match[1]);
    expect(fragments.length).toBeGreaterThan(50);
    for (const fragment of fragments) expect(html).toContain(`id="${fragment}"`);
    const rows = [...html.matchAll(/<tr>([\s\S]*?)<\/tr>/g)].slice(1);
    for (const row of rows) expect(row[1].match(/<td>/g)).toHaveLength(6);
  });

  it('keeps repository evidence readable without broken app links', async () => {
    const inventory = await loadPolicy('inventory', 'en-us');
    expect(inventory).toContain('<span>core/model_gateway/client.py</span>');
    expect(inventory).not.toMatch(/href="(?:\.\.|development\.md)/);
    expect(inventory).toContain('href="#models"');
  });
});
