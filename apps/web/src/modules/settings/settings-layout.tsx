import * as React from 'react';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';

/** Page header for a settings screen: eyebrow, h1 title, muted description and an optional right-aligned status slot. */
export function SettingsPageHeader({ eyebrow, title, description, status }: { eyebrow: React.ReactNode; title: React.ReactNode; description?: React.ReactNode; status?: React.ReactNode }) {
  return (
    <header className="flex flex-wrap items-start justify-between gap-4">
      <div className="min-w-0">
        <p className="text-[13px] font-bold uppercase tracking-[.12em] text-primary">{eyebrow}</p>
        <h1 className="mt-2 mb-0 text-[30px] font-bold tracking-[-.04em]">{title}</h1>
        {description ? <p className="mt-2 mb-0 text-muted-foreground">{description}</p> : null}
      </div>
      {status ? <div className="shrink-0">{status}</div> : null}
    </header>
  );
}

/** Bordered section card with an h2 title and a 16px-gap body; replaces fieldset/legend groupings. */
export function SettingsSection({ title, children }: { title: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-4 rounded-lg border border-border bg-card p-5">
      <h2 className="m-0 text-lg font-bold">{title}</h2>
      {children}
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

/** Write-only secret field: never shows the stored value; typing replaces it, the checkbox removes it. */
export function SecretField({ id, label, value, onValue, removed, onRemoved, configured, disabled, placeholder, removeLabel, status }: { id: string; label: string; value: string; onValue: (value: string) => void; removed: boolean; onRemoved: (removed: boolean) => void; configured: boolean; disabled?: boolean; placeholder: string; removeLabel: string; status: React.ReactNode }) {
  return <div className="flex flex-col gap-2">
    <label className="field" htmlFor={id}><span className="label">{label}</span><Input id={id} type="password" disabled={disabled} autoComplete="new-password" value={value} onChange={(event) => onValue(event.target.value)} placeholder={placeholder} /></label>
    <p className="m-0 text-xs text-muted-foreground">{status}</p>
    {configured ? <CheckboxField><Checkbox id={`${id}-remove`} disabled={disabled} checked={removed} onCheckedChange={(checked) => onRemoved(checked === true)} /><label htmlFor={`${id}-remove`}>{removeLabel}</label></CheckboxField> : null}
  </div>;
}
