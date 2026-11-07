'use client';

import * as React from 'react';
import { Badge } from '@/components/ui/badge';
import { Card } from '@/components/ui/card';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import type { AppLocaleId } from '@/core/i18n';
import { safeHttpUrl } from '@/core/safe-url';
import type { Citation } from '../../api';
import { ChatMarkdown } from '../../chat-markdown';
import { fmt, genUiCopy } from '../copy';
import type { AnswerBlock, AnswerImage } from '../sandbox/schema';

/** Props of {@link RichAnswer}. */
export interface RichAnswerProps {
  /** Possibly partial `answer` payload (blocks may be missing or incomplete while streaming). */
  answer?: { title?: string; blocks?: Array<Partial<AnswerBlock> | null | undefined> } | null;
  /** Message citations; `citations` numbers on blocks are 1-based indexes into this list. */
  citations?: Citation[];
  locale?: AppLocaleId;
}

const TONE_VARIANT = { positive: 'default', neutral: 'secondary', warning: 'outline', negative: 'destructive' } as const;

/** Returns `value` when it is an array, else an empty array, so a half-streamed block cannot crash rendering. */
function list<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

/** `YYYY-MM-DD` -> `dd/MM/yyyy`; any other string is returned unchanged. */
function formatDate(value: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : value;
}

/**
 * Source chips for final citation numbers. A web citation links out (safe http(s) only); anything else renders as a
 * plain number chip.
 *
 * @param props - Citation numbers and the message citation list.
 * @returns Inline chip group, or null when there are no numbers.
 */
function CitationChips({ numbers, citations }: { numbers: unknown; citations?: Citation[] }) {
  const nums = list<number>(numbers).filter((n) => Number.isInteger(n) && n > 0);
  if (nums.length === 0) return null;
  return (
    <span className="ml-1 inline-flex gap-1 align-middle">
      {nums.map((n) => {
        const citation = citations?.[n - 1];
        const href = citation?.sourceType === 'web' ? safeHttpUrl(citation.url) : null;
        const cls = 'inline-flex h-5 min-w-5 items-center justify-center rounded-full border border-border bg-surface px-1 text-[11px] font-medium text-muted-foreground';
        return href
          ? <a key={n} data-cite={n} className={`${cls} hover:text-foreground`} href={href} target="_blank" rel="noopener noreferrer nofollow">{n}</a>
          : <span key={n} data-cite={n} className={cls}>{n}</span>;
      })}
    </span>
  );
}

/**
 * One image tile. Renders only same-origin proxy paths and hides itself when the image fails to load.
 *
 * @param props - Image payload and optional "+N" overlay text.
 * @returns Figure element or null.
 */
function AnswerTile({ image, extra }: { image: AnswerImage; extra?: string }) {
  const [failed, setFailed] = React.useState(false);
  if (failed || typeof image?.src !== 'string' || !image.src.startsWith('/') || image.src.startsWith('//')) return null;
  return (
    <figure className="relative m-0 overflow-hidden rounded-xl bg-muted">
      {/* eslint-disable-next-line @next/next/no-img-element -- signed same-origin media proxy; next/image would re-proxy it */}
      <img src={image.src} alt={image.alt ?? ''} loading="lazy" decoding="async" onError={() => setFailed(true)} className="aspect-[16/10] w-full object-cover" />
      {image.source_host ? <figcaption className="absolute inset-x-0 bottom-0 truncate bg-background/70 px-2 py-0.5 text-[11px] text-muted-foreground">{image.source_host}</figcaption> : null}
      {extra ? <span className="absolute inset-0 flex items-center justify-center bg-background/60 text-lg font-semibold text-foreground">{extra}</span> : null}
    </figure>
  );
}

/**
 * Image strip: the first three safe images in a 3-column grid; the last tile shows "+N" when more images exist.
 *
 * @param props - Images from the payload and the "+N" label formatter.
 * @returns Grid, or null without images.
 */
function ImageStrip({ images, moreLabel }: { images: unknown; moreLabel: (count: number) => string }) {
  // Filter unsafe sources first so "+N" counts only images that can actually render.
  const all = list<AnswerImage>(images).filter((i) => typeof i?.src === 'string' && i.src.startsWith('/') && !i.src.startsWith('//'));
  if (all.length === 0) return null;
  const shown = all.slice(0, 3);
  const more = all.length - shown.length;
  return (
    <div className="grid grid-cols-3 gap-2" data-testid="answer-gallery">
      {shown.map((image, i) => <AnswerTile key={`${image.src}-${i}`} image={image} extra={more > 0 && i === shown.length - 1 ? moreLabel(more) : undefined} />)}
    </div>
  );
}

/** Shared rendering context for blocks. */
interface BlockContext { citations?: Citation[]; locale?: AppLocaleId; copy: ReturnType<typeof genUiCopy> }

/**
 * Renders one block; unknown or incomplete blocks render nothing.
 *
 * @param block - Possibly partial block.
 * @param i - Position used as React key.
 * @param ctx - Citations, locale and message catalog.
 * @returns Block element or null.
 */
function renderBlock(block: Partial<AnswerBlock>, i: number, ctx: BlockContext): React.ReactNode {
  const { citations, locale, copy } = ctx;
  const moreLabel = (count: number) => fmt(copy.genUiAnswerMoreImages, { count });
  switch (block.type) {
    case 'gallery':
      return <ImageStrip key={i} images={block.images} moreLabel={moreLabel} />;
    case 'status':
      return (
        <div key={i} className="flex flex-wrap items-center gap-2">
          <Badge variant={TONE_VARIANT[block.tone ?? 'neutral'] ?? 'secondary'}>{block.label}</Badge>
          {block.updated_at ? <span className="text-sm text-muted-foreground">{fmt(copy.genUiAnswerUpdated, { date: formatDate(block.updated_at) })}</span> : null}
        </div>
      );
    case 'key_value':
      return (
        <div key={i} className="flex flex-col gap-2">
          <p className="m-0 text-3xl font-semibold text-foreground">{block.value}</p>
          {block.caption ? <div className="text-sm text-muted-foreground"><ChatMarkdown content={block.caption} citations={citations} locale={locale} /></div> : null}
          <CitationChips numbers={block.citations} citations={citations} />
        </div>
      );
    case 'stats':
      return (
        <div key={i} className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {list<{ label?: string; value?: string; citations?: number[] }>(block.items).filter(Boolean).map((item, j) => (
            <Card key={j} className="gap-1 p-4">
              <span className="text-sm text-muted-foreground">{item.label}</span>
              <span className="text-2xl font-semibold text-foreground">{item.value}<CitationChips numbers={item.citations} citations={citations} /></span>
            </Card>
          ))}
        </div>
      );
    case 'table':
      return (
        <div key={i}>
          <Table>
            <TableHeader><TableRow>{list<string>(block.columns).map((c, j) => <TableHead key={j}>{c}</TableHead>)}</TableRow></TableHeader>
            <TableBody>
              {list<string[]>(block.rows).map((row, r) => (
                <TableRow key={r}>{list<string>(row).map((cell, c) => <TableCell key={c}>{cell}</TableCell>)}</TableRow>
              ))}
            </TableBody>
          </Table>
          <CitationChips numbers={block.citations} citations={citations} />
        </div>
      );
    case 'section':
      return (
        <section key={i} className="flex flex-col gap-3">
          {block.heading ? <h3 className="m-0 text-lg font-semibold text-foreground">{block.heading}</h3> : null}
          {block.markdown ? <ChatMarkdown content={block.markdown} citations={citations} locale={locale} /> : null}
          <CitationChips numbers={block.citations} citations={citations} />
          <ImageStrip images={block.images} moreLabel={moreLabel} />
        </section>
      );
    case 'note':
      return block.markdown ? <div key={i} className="text-sm text-muted-foreground"><ChatMarkdown content={block.markdown} citations={citations} locale={locale} /></div> : null;
    default:
      return null;
  }
}

/**
 * ChatGPT-style rich answer (`render_answer`): title, image strips, status badge, key value, stat cards, table,
 * sections and a note. Markdown goes through the sanitizing ChatMarkdown; images render only from same-origin
 * proxy paths. Tolerates partial payloads while streaming (missing fields/blocks render nothing).
 *
 * @param props - Partial or complete answer payload, message citations and locale.
 * @returns Vertical stack of blocks, or null before any content arrived.
 */
export function RichAnswer({ answer, citations, locale }: RichAnswerProps) {
  const copy = genUiCopy(locale);
  const blocks = list<Partial<AnswerBlock>>(answer?.blocks).filter(Boolean);
  if (!answer?.title && blocks.length === 0) return null;
  return (
    <article data-testid="rich-answer" className="flex w-full flex-col gap-5 py-2">
      {answer?.title ? <h2 className="m-0 text-2xl font-semibold text-foreground">{answer.title}</h2> : null}
      {blocks.map((block, i) => renderBlock(block, i, { citations, locale, copy }))}
    </article>
  );
}
