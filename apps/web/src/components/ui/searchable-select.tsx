'use client';

import * as React from 'react';
import { CheckIcon, ChevronDownIcon } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';

type Props = {
  value: string;
  options: string[];
  onValueChange: (value: string) => void;
  placeholder: string;
  searchLabel: string;
  emptyLabel?: string;
  disabled?: boolean;
  /** Offers the typed text itself as a selectable option when it is not already listed. */
  allowCustom?: boolean;
  className?: string;
  'aria-label'?: string;
  'aria-labelledby'?: string;
  'aria-describedby'?: string;
};

/** Selects a unique option from a popover filtered immediately by a case-insensitive substring. */
export function SearchableSelect({ value, options, onValueChange, placeholder, searchLabel, emptyLabel, disabled, allowCustom, className, 'aria-label': ariaLabel, 'aria-labelledby': labelledBy, 'aria-describedby': describedBy }: Props) {
  const [open, setOpen] = React.useState(false);
  const [query, setQuery] = React.useState('');
  const listId = React.useId();
  const needle = query.trim().toLowerCase();
  // Duplicate IDs would share React keys and leave stale rows behind when the filter changes.
  const uniqueOptions = [...new Set(options)];
  const shown = needle ? uniqueOptions.filter((id) => id.toLowerCase().includes(needle)) : uniqueOptions;
  // Free text is offered as one extra row so open-ended catalogs can take ids that no list enumerates.
  const custom = allowCustom && query.trim() && !uniqueOptions.some((id) => id.toLowerCase() === needle) ? query.trim() : null;
  const exact = needle ? uniqueOptions.find((id) => id.toLowerCase() === needle) : undefined;
  const rows = custom ? [...shown, custom] : shown;
  return (
    <Popover open={open} onOpenChange={(next) => { setOpen(next); if (!next) setQuery(''); }}>
      <PopoverTrigger asChild>
        <button type="button" role="combobox" aria-expanded={open} aria-controls={open ? listId : undefined} aria-label={ariaLabel} aria-labelledby={labelledBy} aria-describedby={describedBy} disabled={disabled} className={`input flex items-center justify-between gap-2 text-start ${className ?? ''}`}>
          <span className={`min-w-0 flex-1 truncate ${value ? '' : 'text-muted-foreground'}`}>{value || placeholder}</span>
          <ChevronDownIcon size={16} className="shrink-0 text-muted-foreground" aria-hidden="true" />
        </button>
      </PopoverTrigger>
      <PopoverContent className="flex w-(--radix-popover-trigger-width) flex-col gap-2">
        <Input autoFocus value={query} aria-label={searchLabel} placeholder={searchLabel} onChange={(event) => setQuery(event.target.value)}
          // Enter commits the listed option the text exactly matches (any case), else typed free text when custom ids are allowed.
          onKeyDown={(event) => { const commit = exact ?? custom; if (event.key === 'Enter' && commit) { event.preventDefault(); onValueChange(commit); setOpen(false); setQuery(''); } }} />
        <ul id={listId} role="listbox" className="m-0 max-h-60 overflow-auto p-0">
          {rows.length === 0 && <li className="list-none p-2 text-sm text-muted-foreground">{emptyLabel}</li>}
          {rows.map((id) => (
            <li key={id} role="option" aria-selected={id === value} className="select-item list-none break-all" tabIndex={0}
              onClick={() => { onValueChange(id); setOpen(false); setQuery(''); }}
              onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onValueChange(id); setOpen(false); setQuery(''); } }}>
              {id}{id === value && <CheckIcon size={14} aria-hidden="true" />}
            </li>
          ))}
        </ul>
      </PopoverContent>
    </Popover>
  );
}
