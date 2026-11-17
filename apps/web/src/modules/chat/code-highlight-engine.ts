import hljs from 'highlight.js/lib/core';
import bash from 'highlight.js/lib/languages/bash';
import css from 'highlight.js/lib/languages/css';
import javascript from 'highlight.js/lib/languages/javascript';
import json from 'highlight.js/lib/languages/json';
import markdown from 'highlight.js/lib/languages/markdown';
import python from 'highlight.js/lib/languages/python';
import sql from 'highlight.js/lib/languages/sql';
import typescript from 'highlight.js/lib/languages/typescript';
import xml from 'highlight.js/lib/languages/xml';
import yaml from 'highlight.js/lib/languages/yaml';

// Only common languages are registered (core build) to keep the chat bundle small; aliases such as ts, js, sh, yml,
// md and html come from each language definition. Anything else renders as plain escaped text.
hljs.registerLanguage('bash', bash);
hljs.registerLanguage('css', css);
hljs.registerLanguage('javascript', javascript);
hljs.registerLanguage('json', json);
hljs.registerLanguage('markdown', markdown);
hljs.registerLanguage('python', python);
hljs.registerLanguage('sql', sql);
hljs.registerLanguage('typescript', typescript);
hljs.registerLanguage('xml', xml);
hljs.registerLanguage('yaml', yaml);

/** Fences larger than this are left unhighlighted so streaming re-renders stay cheap. */
const MAX_HIGHLIGHT_CHARS = 20_000;

/**
 * Highlights code for a fence. The result is HTML in which every character of the source is escaped by highlight.js
 * (it only adds `hljs-*` spans); the caller still sanitizes the final document.
 *
 * @param code - Raw fence content.
 * @param language - Language word from {@link fenceLanguage}; unknown or empty languages are not highlighted.
 * @returns Highlighted HTML, or null when the language is unregistered or the code is too large.
 */
export function highlightFence(code: string, language: string): string | null {
  if (!language || code.length > MAX_HIGHLIGHT_CHARS || !hljs.getLanguage(language)) return null;
  try {
    return hljs.highlight(code, { language, ignoreIllegals: true }).value;
  } catch {
    return null;
  }
}
