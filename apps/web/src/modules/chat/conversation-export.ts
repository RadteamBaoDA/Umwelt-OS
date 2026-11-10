import * as React from 'react';
import { useTranslations } from 'next-intl';
import type { ChatMessage, Citation } from './api';
import { safeHttpUrl } from '@/core/safe-url';

/** Localized labels used while building the export (resolved by the caller from the `chat` catalog). */
export interface ExportLabels {
  /** Heading of the user's turns. */
  user: string;
  /** Heading of assistant turns when a message carries no model identity. */
  assistant: string;
  /** Heading of an answer's numbered source list. */
  sources: string;
  /** Document title when the conversation has none. */
  untitled: string;
}

/** Escapes characters that would break a markdown link label. */
function linkLabel(text: string): string {
  return text
    .replace(/[[\]\\]/g, '\\$&')
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * One numbered line of an answer's source list. Web results link to their (validated) URL; documents have no stable
 * public URL outside the app, so they are listed by title only.
 *
 * @param citation - Grounded citation attached to the answer.
 * @param position - 1-based position, matching the `[n]` markers in the answer text.
 */
function citationLine(citation: Citation, position: number): string {
  const title = linkLabel(citation.title || 'source');
  const href = citation.sourceType === 'web' ? safeHttpUrl(citation.url) : null;
  return `${position}. ${href ? `[${title}](${href})` : title}`;
}

/**
 * Builds a Markdown transcript from already-loaded messages: user and assistant turns in order, each answer followed
 * by its citations as a numbered list. System messages and empty turns are skipped. Pure and client-side; no request
 * is made and nothing beyond what the reader already sees is included.
 *
 * @param title - Conversation title (may be empty).
 * @param messages - Loaded transcript in display order.
 * @param labels - Localized headings.
 * @returns Markdown document text.
 */
export function conversationToMarkdown(title: string, messages: readonly ChatMessage[], labels: ExportLabels): string {
  const blocks: string[] = [`# ${title.trim() || labels.untitled}`];
  for (const message of messages) {
    if (message.role === 'system' || !message.content.trim()) continue;
    const heading = message.role === 'user' ? labels.user : message.model_identity || labels.assistant;
    const parts = [`## ${heading}`, message.content.trim()];
    if (message.role === 'assistant' && message.citations?.length) {
      parts.push(`**${labels.sources}**`, message.citations.map((citation, index) => citationLine(citation, index + 1)).join('\n'));
    }
    blocks.push(parts.join('\n\n'));
  }
  return `${blocks.join('\n\n')}\n`;
}

/**
 * File name for the export: the title with characters that are invalid in file names removed, whitespace collapsed
 * and length bounded, plus `.md`. Non-Latin letters (e.g. Vietnamese) are kept.
 *
 * @param title - Conversation title (may be empty).
 * @returns A safe `*.md` file name.
 */
export function exportFilename(title: string): string {
  const cleaned = title
    .replace(/[\\/:*?"<>|\u0000-\u001f]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/^\.+/, '')
    .slice(0, 80)
    .trim();
  return `${cleaned || 'conversation'}.md`;
}

/**
 * Downloads text as a UTF-8 Markdown file through a temporary object URL.
 *
 * @param filename - Target file name.
 * @param markdown - File content.
 */
export function downloadMarkdown(filename: string, markdown: string): void {
  const url = URL.createObjectURL(new Blob([markdown], { type: 'text/markdown;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

/**
 * Export action for the session header: builds the Markdown from the loaded transcript with localized headings and
 * downloads it. Returns undefined (no menu entry) outside full mode or while the transcript is empty.
 *
 * @param title - Conversation title (may be empty).
 * @param messages - Loaded transcript.
 * @param full - Whether the full Chat page hosts the session.
 * @returns Stable click handler, or undefined when there is nothing to export.
 */
export function useConversationExport(title: string, messages: readonly ChatMessage[], full: boolean): (() => void) | undefined {
  const t = useTranslations('chat');
  const handler = React.useCallback(() => {
    const markdown = conversationToMarkdown(title, messages, {
      user: t('userSpeaker'), assistant: t('assistantFallback'), sources: t('exportAnswerSources'), untitled: t('title'),
    });
    downloadMarkdown(exportFilename(title), markdown);
  }, [title, messages, t]);
  return full && messages.length > 0 ? handler : undefined;
}
