'use client';

import * as React from 'react';
import { LayoutGridIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet';
import type { AppLocaleId } from '@/core/i18n';
import { genUiCopy, resolveWidgetLocale } from '../copy';
import { GALLERY_CATEGORIES, GALLERY_DEMOS, type GalleryCategory } from './gallery-demos';

/** Props for {@link DemoGallery}. */
export interface DemoGalleryProps {
  /** Sends the chosen example prompt through the chat send flow. */
  onSendPrompt: (prompt: string) => void;
  /** Application locale. */
  locale?: AppLocaleId;
}

/**
 * Drawer of seven example prompts with Understand / Compare / Make a tool filters.
 * Escape and overlay click close it (Radix dialog semantics); "Try" sends the prompt and closes.
 *
 * @param props - Send callback and locale.
 * @returns Sheet with a trigger button.
 */
export function DemoGallery({ onSendPrompt, locale }: DemoGalleryProps) {
  const copy = genUiCopy(resolveWidgetLocale(locale));
  const [open, setOpen] = React.useState(false);
  const [category, setCategory] = React.useState<GalleryCategory | null>(null);
  const demos = GALLERY_DEMOS.filter((d) => !category || d.category === category);
  const pills: Array<{ id: GalleryCategory | null; label: string }> = [
    { id: null, label: copy.shellGalleryCatAll },
    ...GALLERY_CATEGORIES.map((c) => ({ id: c.id, label: copy[c.labelKey] })),
  ];

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger asChild>
        <Button type="button" variant="outline" size="sm" data-testid="demo-gallery-open">
          <LayoutGridIcon className="size-3.5" aria-hidden="true" />
          {copy.shellGalleryOpen}
        </Button>
      </SheetTrigger>
      <SheetContent side="right" className="w-full overflow-y-auto sm:max-w-md" data-testid="demo-gallery">
        <SheetHeader>
          <SheetTitle>{copy.shellGalleryTitle}</SheetTitle>
          <SheetDescription>{copy.shellGalleryDescription}</SheetDescription>
        </SheetHeader>
        <div className="flex flex-col gap-3 px-4 pb-4">
          <div role="group" aria-label={copy.genUiFilterGroup} className="flex flex-wrap gap-1.5">
            {pills.map((p) => (
              <Button
                key={p.id ?? 'all'}
                type="button"
                size="sm"
                variant={p.id === category ? 'default' : 'outline'}
                aria-pressed={p.id === category}
                data-testid={`gallery-filter-${p.id ?? 'all'}`}
                onClick={() => setCategory(p.id)}
                className="rounded-full text-xs"
              >
                {p.label}
              </Button>
            ))}
          </div>
          {demos.map((d) => (
            <div key={d.id} data-testid={`gallery-demo-${d.id}`} className="flex items-center justify-between gap-2 rounded-lg border border-border bg-surface p-3">
              <div className="min-w-0">
                <p className="text-sm font-medium text-foreground">{copy[d.titleKey]}</p>
                <p className="text-xs text-muted-foreground">{copy[d.descKey]}</p>
              </div>
              <Button
                type="button"
                size="sm"
                variant="secondary"
                data-testid={`gallery-try-${d.id}`}
                onClick={() => {
                  onSendPrompt(copy[d.promptKey]);
                  setOpen(false);
                }}
              >
                {copy.shellGalleryTry}
              </Button>
            </div>
          ))}
        </div>
      </SheetContent>
    </Sheet>
  );
}
