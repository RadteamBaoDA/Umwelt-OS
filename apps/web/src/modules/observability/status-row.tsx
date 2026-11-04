import type { ReactNode } from 'react';
import type { Tone } from './system-status-model';

const DOT: Record<Tone, string> = {
  success: 'bg-[var(--status-success)]',
  warning: 'bg-[var(--status-warning)]',
  danger: 'bg-[var(--danger)]',
  muted: 'bg-[var(--chart-4)]',
};

/** Small status dot coloured by semantic tone; decorative, so the adjacent text carries the meaning. */
export function StatusDot({ tone }: { tone: Tone }) {
  return <span aria-hidden="true" data-tone={tone} className={`inline-block size-2 shrink-0 rounded-full ${DOT[tone]}`} />;
}

/**
 * One flat status row: dot, name, status text, then optional trailing content (uptime strip, action).
 * Shared by the System status service list and the Global settings deployment list.
 */
export function StatusRow({ tone, name, status, children }: { tone: Tone; name: ReactNode; status: ReactNode; children?: ReactNode }) {
  return <li className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border py-2.5 last:border-0">
    <StatusDot tone={tone} />
    <span className="min-w-28 text-sm font-medium">{name}</span>
    <span className="text-sm text-muted-foreground">{status}</span>
    {children}
  </li>;
}
