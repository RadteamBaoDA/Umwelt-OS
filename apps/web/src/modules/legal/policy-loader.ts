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

/** Reads only the six shipped policy files (or the published inventory); never accepts a filesystem path. */
export async function loadPolicy(name: PolicyName | 'inventory', locale: LegalLocale): Promise<string> {
  if (!Object.hasOwn(policyFiles, locale) || (name !== 'inventory' && !Object.hasOwn(policyFiles[locale], name))) {
    throw new Error('Unknown legal document');
  }
  const relative = name === 'inventory' ? '../third-party-services.md' : policyFiles[locale][name];
  const source = await readFile(path.resolve(process.cwd(), '../../docs/legal', relative), 'utf8');
  return renderer.render(source);
}
