'use client';

import { useId, useState, type ReactNode } from 'react';

/**
 * Collapses a filter block behind a toggle below 720px (CSS-driven); on wider screens the body is always shown
 * and the toggle is hidden. The body stays mounted so form controls keep their state and labels.
 * @param label Localized toggle text. @param count Number of active filters, appended as "(n)" when positive.
 * @param forceOpen Keeps the body open while a field inside it shows a validation error, so the error is never hidden.
 */
export function FiltersDisclosure({ label, count, forceOpen = false, children }: { label: string; count: number; forceOpen?: boolean; children: ReactNode }) {
  const [userOpen, setOpen] = useState(false);
  const open = userOpen || forceOpen;
  const bodyId = useId();
  return <div className="filters-disclosure" data-open={open}>
    <button type="button" className="filters-toggle" aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen(!open)}>{count > 0 ? `${label} (${count})` : label}</button>
    <div id={bodyId} className="filters-body">{children}</div>
  </div>;
}
