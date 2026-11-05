import 'server-only';

import { readFile } from 'node:fs/promises';
import path from 'node:path';
import MarkdownIt from 'markdown-it';
import type { LegalLocale, PolicyName } from './messages';

const renderer = new MarkdownIt({ html: false, linkify: false, typographer: false });
type Token = ReturnType<typeof renderer.parse>[number];
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

/** True when (locale, name) addresses one of the six shipped files or the inventory; guards against arbitrary keys. */
function isKnownDocument(name: PolicyName | 'inventory', locale: LegalLocale): boolean {
  if (!Object.hasOwn(policyFiles, locale)) return false;
  return name === 'inventory' || Object.hasOwn(policyFiles[locale], name);
}

/** Slugifies heading text into a unicode-preserving anchor base (NFC, lowercase, punctuation stripped, dashes). */
function headingSlug(text: string): string {
  return text.normalize('NFC').toLowerCase()
    .replace(/[^\p{L}\p{N}\s_-]/gu, '').trim().replace(/\s+/g, '-');
}

/** Assigns unique, stable `id`s to every heading token; repeated slugs get `-1`, `-2`, ... suffixes. */
function assignHeadingIds(tokens: Token[]): void {
  const headings = new Map<string, number>();
  tokens.forEach((token, index) => {
    if (token.type !== 'heading_open') return;
    const base = headingSlug(tokens[index + 1]?.content ?? '');
    const occurrence = headings.get(base) ?? 0;
    headings.set(base, occurrence + 1);
    token.attrSet('id', occurrence === 0 ? base : `${base}-${occurrence}`);
  });
}

/**
 * Turns repository-relative links (no public app URL) into inert spans while keeping their text; http(s), mailto,
 * root-relative and fragment links stay clickable. Link open/close pairs are matched per token's inline children.
 */
function neutralizeRepositoryLinks(tokens: Token[]): void {
  for (const token of tokens) {
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
}

/** Reads only the six shipped policy files (or the published inventory); never accepts a filesystem path. */
export async function loadPolicy(name: PolicyName | 'inventory', locale: LegalLocale): Promise<string> {
  if (!isKnownDocument(name, locale)) {
    throw new Error('Unknown legal document');
  }
  const relative = name === 'inventory' ? '../third-party-services.md' : policyFiles[locale][name as PolicyName];
  const source = await readFile(path.resolve(policyRoot(process.cwd()), relative), 'utf8');
  const tokens = renderer.parse(source, {});
  assignHeadingIds(tokens);
  neutralizeRepositoryLinks(tokens);
  return renderer.renderer.render(tokens, renderer.options, {});
}
