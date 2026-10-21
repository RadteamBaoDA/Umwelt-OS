'use client';

import * as React from 'react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import type { AppLocaleId } from '@/core/i18n';
import { genUiCopy, resolveWidgetLocale } from '../copy';
import { GALLERY_DEMOS, STARTER_DEMO_IDS } from './gallery-demos';
import { TemplateChip } from './template-chip';

const TIP_KEY = 'umwelt.genui.desktopTipDismissed';

/** Reads the dismissal flag; sessionStorage can throw in private or blocked contexts. */
function tipDismissed(): boolean {
  try {
    return window.sessionStorage.getItem(TIP_KEY) === '1';
  } catch {
    return false;
  }
}

/**
 * One-time-per-session modal on viewports narrower than the sm breakpoint (640px) suggesting a
 * larger screen. Dismissal is stored in sessionStorage; storage failures just re-show next load.
 *
 * @param props - Optional locale.
 * @returns Dialog, closed when not applicable.
 */
export function DesktopTipModal({ locale }: { locale?: AppLocaleId }) {
  const copy = genUiCopy(resolveWidgetLocale(locale));
  const [open, setOpen] = React.useState(false);

  // Evaluated after mount: viewport and storage are unavailable during SSR.
  React.useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- browser-only check after mount
    if (window.matchMedia('(max-width: 639px)').matches && !tipDismissed()) setOpen(true);
  }, []);

  /** Closes the modal and remembers the dismissal for this tab session. */
  const dismiss = () => {
    try {
      window.sessionStorage.setItem(TIP_KEY, '1');
    } catch {
      /* storage blocked: the tip may reappear on reload, which is harmless */
    }
    setOpen(false);
  };

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) dismiss(); }}>
      <DialogContent data-testid="desktop-tip-modal">
        <DialogHeader>
          <DialogTitle>{copy.shellTipTitle}</DialogTitle>
          <DialogDescription>{copy.shellTipBody}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button type="button" onClick={dismiss}>{copy.shellTipDismiss}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Four starter suggestion chips (plane, road trip, Monty Hall, bill splitter) for an empty
 * conversation; selecting one sends its prompt.
 *
 * @param props - Send callback and optional locale.
 * @returns Labelled chip row.
 */
export function StarterSuggestions({ onSendPrompt, locale }: { onSendPrompt: (prompt: string) => void; locale?: AppLocaleId }) {
  const copy = genUiCopy(resolveWidgetLocale(locale));
  return (
    <div data-testid="starter-suggestions" role="group" aria-label={copy.shellSuggestionsLabel} className="flex flex-wrap justify-center gap-2">
      {STARTER_DEMO_IDS.map((id) => {
        const demo = GALLERY_DEMOS.find((d) => d.id === id)!;
        return <TemplateChip key={id} label={copy[demo.titleKey]} prompt={copy[demo.promptKey]} locale={locale} onSelectPrompt={onSendPrompt} />;
      })}
    </div>
  );
}

/**
 * Toolbar row for the full Chat page; currently only the small-screen desktop tip.
 * Template library and demo gallery were removed from the chat surface.
 *
 * @param props - Optional locale.
 * @returns Toolbar row.
 */
export function ChatShellTools({ locale }: { locale?: AppLocaleId }) {
  return (
    <div className="flex items-center justify-end gap-2" data-testid="chat-shell-tools">
      <DesktopTipModal locale={locale} />
    </div>
  );
}
