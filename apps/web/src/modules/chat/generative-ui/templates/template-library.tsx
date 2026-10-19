'use client';

import * as React from 'react';
import { BookmarkIcon, BookmarkPlusIcon, Trash2Icon } from 'lucide-react';
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
import { fmt, genUiCopy, resolveWidgetLocale } from '../copy';
import { SEED_HTML_TEMPLATES } from './seed-html-templates';
import { deleteUserTemplate, saveUserTemplate, useUserTemplates } from './template-store';

/** Props for {@link TemplateLibrary}. */
export interface TemplateLibraryProps {
  /** Sends the composed prompt through the chat send flow (the transcript onSendPrompt). */
  onSendPrompt: (prompt: string) => void;
  /** Application locale. */
  locale?: AppLocaleId;
}

/**
 * Drawer listing built-in and user-saved widget templates. "Use template" composes a prompt
 * carrying the template JSON and dispatches it through the existing send flow, then closes.
 *
 * @param props - Send callback and locale.
 * @returns Sheet with a trigger button.
 */
export function TemplateLibrary({ onSendPrompt, locale }: TemplateLibraryProps) {
  const copy = genUiCopy(resolveWidgetLocale(locale));
  const mine = useUserTemplates();
  const [open, setOpen] = React.useState(false);

  /** Dispatches the apply prompt and closes the drawer. */
  const apply = (title: string, content: string) => {
    onSendPrompt(fmt(copy.shellTemplatesApplyPrompt, { title, template: content }));
    setOpen(false);
  };

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger asChild>
        <Button type="button" variant="outline" size="sm" data-testid="template-library-open">
          <BookmarkIcon className="size-3.5" aria-hidden="true" />
          {copy.shellTemplatesOpen}
        </Button>
      </SheetTrigger>
      <SheetContent side="right" className="w-full overflow-y-auto sm:max-w-md" data-testid="template-library">
        <SheetHeader>
          <SheetTitle>{copy.shellTemplatesTitle}</SheetTitle>
          <SheetDescription>{copy.shellTemplatesDescription}</SheetDescription>
        </SheetHeader>
        <div className="flex flex-col gap-4 px-4 pb-4">
          <section aria-label={copy.shellTemplatesBuiltIn} className="flex flex-col gap-2">
            <h3 className="text-xs font-semibold text-muted-foreground">{copy.shellTemplatesBuiltIn}</h3>
            {SEED_HTML_TEMPLATES.map((tpl) => (
              <div key={tpl.id} data-testid={`seed-template-${tpl.id}`} className="flex items-center justify-between gap-2 rounded-lg border border-border bg-surface p-3">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-foreground">{copy[tpl.titleKey]}</p>
                  <p className="text-xs text-muted-foreground">{copy[tpl.descKey]}</p>
                </div>
                <Button type="button" size="sm" variant="secondary" onClick={() => apply(copy[tpl.titleKey], JSON.stringify(tpl.sandbox))}>
                  {copy.shellTemplatesApply}
                </Button>
              </div>
            ))}
          </section>
          <section aria-label={copy.shellTemplatesMine} className="flex flex-col gap-2">
            <h3 className="text-xs font-semibold text-muted-foreground">{copy.shellTemplatesMine}</h3>
            {mine.length === 0 && <p className="text-xs text-muted-foreground">{copy.shellTemplatesEmpty}</p>}
            {mine.map((tpl) => (
              <div key={tpl.id} data-testid={`user-template-${tpl.id}`} className="flex items-center justify-between gap-2 rounded-lg border border-border bg-surface p-3">
                <p className="min-w-0 truncate text-sm font-medium text-foreground">{tpl.title}</p>
                <div className="flex shrink-0 gap-1">
                  <Button type="button" size="sm" variant="secondary" onClick={() => apply(tpl.title, tpl.content)}>
                    {copy.shellTemplatesApply}
                  </Button>
                  <Button
                    type="button"
                    size="icon"
                    variant="ghost"
                    aria-label={fmt(copy.shellTemplatesDelete, { title: tpl.title })}
                    onClick={() => deleteUserTemplate(tpl.id)}
                  >
                    <Trash2Icon className="size-4" aria-hidden="true" />
                  </Button>
                </div>
              </div>
            ))}
          </section>
        </div>
      </SheetContent>
    </Sheet>
  );
}

/** Props for {@link SaveTemplateButton}. */
export interface SaveTemplateButtonProps {
  /** Sandbox payload of the rendered widget being saved. */
  sandbox: Record<string, unknown>;
  /** Widget title used as the template name. */
  title?: string;
  /** Application locale. */
  locale?: AppLocaleId;
}

/**
 * "Save as template" action shown under a rendered sandbox widget. Reports success or a
 * storage/size failure in an announced status line.
 *
 * @param props - Sandbox payload, title and locale.
 * @returns Button with an inline status.
 */
export function SaveTemplateButton({ sandbox, title, locale }: SaveTemplateButtonProps) {
  const copy = genUiCopy(resolveWidgetLocale(locale));
  const [status, setStatus] = React.useState<'idle' | 'saved' | 'failed'>('idle');
  return (
    <div className="flex items-center gap-2">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        data-testid="save-template"
        onClick={() => setStatus(saveUserTemplate(title ?? '', JSON.stringify(sandbox)) ? 'saved' : 'failed')}
      >
        <BookmarkPlusIcon className="size-3.5" aria-hidden="true" />
        {copy.shellTemplatesSave}
      </Button>
      <span role="status" className={`text-xs ${status === 'failed' ? 'text-destructive' : 'text-muted-foreground'}`}>
        {status === 'saved' ? copy.shellTemplatesSaved : status === 'failed' ? copy.shellTemplatesSaveFailed : ''}
      </span>
    </div>
  );
}
