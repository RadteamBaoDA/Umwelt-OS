import 'server-only';

import { readFile } from 'node:fs/promises';
import path from 'node:path';
import MarkdownIt from 'markdown-it';
import type { LegalLocale, PolicyName } from './messages';

const renderer = new MarkdownIt({ html: false, linkify: false, typographer: false });
// Explicit paths prevent user input from selecting arbitrary files, even at runtime.
const policyFiles = {
  'en-us': { privacy: 'en-us/privacy.md', terms: 'en-us/terms.md', refunds: 'en-us/refunds.md' },
  'vi-vi': { privacy: 'vi-vi/privacy.md', terms: 'vi-vi/terms.md', refunds: 'vi-vi/refunds.md' },
} as const;

/** Resolves the canonical policy directory for a web-workspace or standalone-root process. */
export function policyRoot(cwd: string): string {
  const root = path.basename(cwd) === 'web' && path.basename(path.dirname(cwd)) === 'apps'
    ? path.resolve(cwd, '../..') : cwd;
  return path.resolve(root, 'docs/legal');
}

/** Reads only the six shipped policy files (or the published inventory); never accepts a filesystem path. */
export async function loadPolicy(name: PolicyName | 'inventory', locale: LegalLocale): Promise<string> {
  if (!Object.hasOwn(policyFiles, locale) || (name !== 'inventory' && !Object.hasOwn(policyFiles[locale], name))) {
    throw new Error('Unknown legal document');
  }
  const relative = name === 'inventory' ? '../third-party-services.md' : policyFiles[locale][name];
  const source = await readFile(path.resolve(policyRoot(process.cwd()), relative), 'utf8');
  const tokens = renderer.parse(source, {});
  const headings = new Map<string, number>();
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index];
    if (token.type === 'heading_open') {
      const base = (tokens[index + 1]?.content ?? '').normalize('NFC').toLowerCase()
        .replace(/[^\p{L}\p{N}\s_-]/gu, '').trim().replace(/\s+/g, '-');
      const occurrence = headings.get(base) ?? 0;
      headings.set(base, occurrence + 1);
      token.attrSet('id', occurrence === 0 ? base : `${base}-${occurrence}`);
    }
    const repositoryLinks: boolean[] = [];
    for (const child of token.children ?? []) {
      if (child.type === 'link_open') {
        const href = child.attrGet('href') ?? '';
        // Repository evidence remains clickable in Markdown review; it has no public app URL.
        const repositoryLink = typeof href !== 'string' || !/^(?:https?:|mailto:|\/|#)/i.test(href);
        repositoryLinks.push(repositoryLink);
        if (repositoryLink) {
          child.tag = 'span';
          child.attrs = null;
        }
      } else if (child.type === 'link_close' && repositoryLinks.pop()) {
        child.tag = 'span';
      }
    }
  }
  return renderer.renderer.render(tokens, renderer.options, {});
}
