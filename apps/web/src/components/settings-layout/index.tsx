import * as React from 'react';
import { Checkbox } from '@/components/ui/checkbox';
import { PasswordInput } from '@/components/ui/password-input';

/**
 * True when a status node renders no text: a bare `<p>` whose children are empty strings, null or false.
 * Lets pages keep passing one status element while the header omits it until there is something to say.
 */
function isEmptyStatus(status: React.ReactNode): boolean {
  if (!React.isValidElement<{ children?: React.ReactNode }>(status)) return false;
  const children = React.Children.toArray(status.props.children);
  return children.every((child) => typeof child === 'string' && child.trim() === '');
}

/**
 * Page header for a settings screen: h1 title (24/600), optional description, plus an optional right-aligned status slot.
 * `eyebrow` is accepted for call-site compatibility but intentionally not rendered (ledger design: title only).
 * A status element with no text is omitted, so an idle save line never leaves an empty row.
 */
export function SettingsPageHeader({ title, description, status }: { eyebrow?: React.ReactNode; title: React.ReactNode; description?: React.ReactNode; status?: React.ReactNode }) {
  return (
    <header className="flex flex-wrap items-start justify-between gap-4">
      <div className="min-w-0">
        <h1 className="mt-0 mb-0 text-2xl font-semibold">{title}</h1>
        {description ? <p className="mt-2 mb-0 text-muted-foreground">{description}</p> : null}
      </div>
      {/* A status node with no text (e.g. a save line before any save) would wrap onto its own row and leave a gap. */}
      {status && !isEmptyStatus(status) ? <div className="shrink-0">{status}</div> : null}
    </header>
  );
}

/**
 * Settings row: label and muted 14px description in a ~240px left column beside the controls on the right.
 * One top rule separates rows (no card border); below 720px the label stacks above the controls.
 */
export function SettingsSection({ title, description, children }: { title: React.ReactNode; description?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="grid grid-cols-1 gap-4 border-t border-border pt-6 min-[901px]:grid-cols-[240px_minmax(0,1fr)] min-[901px]:gap-8">
      <div className="flex flex-col gap-1">
        <h2 className="m-0 text-base font-semibold">{title}</h2>
        {description ? <p className="m-0 text-sm text-muted-foreground">{description}</p> : null}
      </div>
      <div className="flex min-w-0 flex-col gap-4">{children}</div>
    </section>
  );
}

/** Two-column field grid with a 16px gap that stacks to one column below 720px. */
export function FieldGrid({ children }: { children: React.ReactNode }) {
  return <div className="grid grid-cols-1 gap-4 min-[720px]:grid-cols-2">{children}</div>;
}

/** Checkbox row: control and label side by side in a flex row with a 12px gap (use instead of `.field`). */
export function CheckboxField({ children }: { children: React.ReactNode }) {
  return <div className="flex items-center gap-3">{children}</div>;
}

/** Right-aligned Cancel/Save footer with a top border, in normal document flow (never sticky or overlaying fields). */
export function SettingsActions({ children }: { children: React.ReactNode }) {
  return <div className="flex flex-wrap justify-end gap-2 border-t border-border pt-4">{children}</div>;
}

/** Write-only secret field: never shows the stored value; typing replaces it, the checkbox removes it. `hideLabel` keeps the label as the accessible name only (the surrounding section title already shows it). */
export function SecretField({ id, label, value, onValue, removed, onRemoved, configured, disabled, placeholder, removeLabel, status, hideLabel }: { id: string; label: string; value: string; onValue: (value: string) => void; removed: boolean; onRemoved: (removed: boolean) => void; configured: boolean; disabled?: boolean; placeholder: string; removeLabel: string; status: React.ReactNode; hideLabel?: boolean }) {
  return <div className="flex flex-col gap-2">
    <label className="field" htmlFor={id}><span className={hideLabel ? 'label sr-only' : 'label'}>{label}</span><PasswordInput id={id} disabled={disabled} autoComplete="new-password" value={value} onChange={(event) => onValue(event.target.value)} placeholder={placeholder} /></label>
    <p className="m-0 text-sm text-muted-foreground">{status}</p>
    {configured ? <CheckboxField><Checkbox id={`${id}-remove`} disabled={disabled} checked={removed} onCheckedChange={(checked) => onRemoved(checked === true)} /><label htmlFor={`${id}-remove`}>{removeLabel}</label></CheckboxField> : null}
  </div>;
}
