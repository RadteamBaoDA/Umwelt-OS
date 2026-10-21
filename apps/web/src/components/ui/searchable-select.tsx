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
  className?: string;
  'aria-label'?: string;
};

/** Select-like control whose popover has a search box that filters a scrollable option list (case-insensitive substring). */
export function SearchableSelect({ value, options, onValueChange, placeholder, searchLabel, emptyLabel, disabled, className, 'aria-label': ariaLabel }: Props) {
  const [open, setOpen] = React.useState(false);
  const [query, setQuery] = React.useState('');
  const needle = query.trim().toLowerCase();
  const shown = needle ? options.filter((id) => id.toLowerCase().includes(needle)) : options;
  return (
    <Popover open={open} onOpenChange={(next) => { setOpen(next); if (!next) setQuery(''); }}>
      <PopoverTrigger asChild>
        <button type="button" role="combobox" aria-expanded={open} aria-label={ariaLabel} disabled={disabled} className={`input flex items-center justify-between gap-2 text-start ${className ?? ''}`}>
          <span className={`min-w-0 flex-1 truncate ${value ? '' : 'text-muted-foreground'}`}>{value || placeholder}</span>
          <ChevronDownIcon size={16} className="shrink-0 text-muted-foreground" aria-hidden="true" />
        </button>
      </PopoverTrigger>
      <PopoverContent className="flex w-(--radix-popover-trigger-width) flex-col gap-2">
        <Input autoFocus value={query} aria-label={searchLabel} placeholder={searchLabel} onChange={(event) => setQuery(event.target.value)} />
        <ul role="listbox" className="m-0 max-h-60 overflow-auto p-0">
          {shown.length === 0 && <li className="list-none p-2 text-sm text-muted-foreground">{emptyLabel}</li>}
          {shown.map((id) => (
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
