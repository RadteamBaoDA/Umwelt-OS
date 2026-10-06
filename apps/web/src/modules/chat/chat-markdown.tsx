'use client';

import * as React from 'react';
import MarkdownIt, { type MarkdownIt as MarkdownItInstance } from 'markdown-it';
import DOMPurify from 'dompurify';

// The markdown-it renderer and safe link/code boundaries adapt AnythingLLM's
// frontend/src/utils/chat/markdown.js; the source slices and local repairs are recorded in docs/anythingllm-port.md.

const SAFE_LINK_PROTOCOLS = new Set(['http:', 'https:', 'mailto:']);
const MARKDOWN_TAGS = [
  'a', 'blockquote', 'br', 'code', 'del', 'em', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'hr', 'li', 'ol', 'p', 'pre', 'strong', 'table', 'tbody', 'td', 'th', 'thead', 'tr', 'ul',
];

/** Accepts web/email and same-origin relative links while rejecting active or ambiguous schemes. */
function isSafeMarkdownLink(href: string): boolean {
  if (!href || /[\u0000-\u001f\\]/.test(href) || href.startsWith('//')) return false;
  try {
    return SAFE_LINK_PROTOCOLS.has(new URL(href, 'https://umwelt.invalid').protocol);
  } catch {
    return false;
  }
}

/** Escapes plain markdown image alt text because remote images are intentionally not fetched. */
function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[character] ?? character);
}

/** Creates the customized upstream-derived renderer with HTML, active schemes, and remote images disabled. */
function createMarkdownRenderer(): MarkdownItInstance {
  const markdown = new MarkdownIt({
    html: false,
    linkify: false,
    typographer: true,
    breaks: true,
  });
  markdown.validateLink = isSafeMarkdownLink;
  markdown.renderer.rules.link_open = (tokens, index) => {
    const href = String(tokens[index].attrGet('href') ?? '');
    return `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">`;
  };
  markdown.renderer.rules.link_close = () => '</a>';
  markdown.renderer.rules.image = (tokens, index) => escapeHtml(String(tokens[index].content));
  markdown.renderer.rules.fence = (tokens, index) => {
    const token = tokens[index];
    return `<pre class="my-2 max-w-full overflow-x-auto rounded-md border border-border bg-muted p-3 text-xs"><code>${escapeHtml(token.content)}</code></pre>`;
  };
  markdown.renderer.rules.code_block = (tokens, index) =>
    `<pre class="my-2 max-w-full overflow-x-auto rounded-md border border-border bg-muted p-3 text-xs"><code>${escapeHtml(tokens[index].content)}</code></pre>`;
  markdown.renderer.rules.bullet_list_open = () => '<ul class="my-2 list-disc space-y-1 pl-6">';
  markdown.renderer.rules.ordered_list_open = () => '<ol class="my-2 list-decimal space-y-1 pl-6">';
  markdown.renderer.rules.blockquote_open = () => '<blockquote class="my-2 border-l-2 border-primary pl-3 text-muted-foreground">';
  return markdown;
}

const markdownRenderer = createMarkdownRenderer();

/** Renders adapted AnythingLLM markdown only after the browser sanitizer can initialize safely. */
export function ChatMarkdown({ content }: { content: string }) {
  const [safeHtml, setSafeHtml] = React.useState('');

  React.useEffect(() => {
    // DOMPurify needs the live browser window; keeping creation here avoids touching it during SSR.
    const purifier = DOMPurify(window);
    const rendered = markdownRenderer.render(content);
    // eslint-disable-next-line react-hooks/set-state-in-effect -- sanitizer needs the live window, so state is set after mount
    setSafeHtml(purifier.sanitize(rendered, {
      ALLOWED_TAGS: MARKDOWN_TAGS,
      ALLOWED_ATTR: ['class', 'href', 'rel', 'target'],
      ALLOW_DATA_ATTR: false,
      ALLOW_ARIA_ATTR: false,
      FORBID_TAGS: ['img', 'iframe', 'object', 'script', 'style', 'svg', 'math'],
    }));
  }, [content]);

  return (
    <div
      className="chat-markdown min-w-0 break-words text-sm leading-relaxed text-foreground [&_p]:my-1.5 [&_h1]:my-2 [&_h1]:text-lg [&_h1]:font-semibold [&_h2]:my-2 [&_h2]:text-base [&_h2]:font-semibold [&_h3]:my-2 [&_h3]:font-semibold [&_a]:text-primary [&_a]:underline [&_table]:my-2 [&_table]:border-collapse [&_td]:border [&_td]:border-border [&_td]:p-1 [&_th]:border [&_th]:border-border [&_th]:p-1 [&_th]:font-semibold"
      dangerouslySetInnerHTML={{ __html: safeHtml }}
    />
  );
}
