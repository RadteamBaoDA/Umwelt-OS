import { cn } from 'cn';

/**
 * Umwelt-OS functional-circle mark: two open arcs (perception in, action out) and a centre dot (the self).
 * Draws with `currentColor`, so wrap it in a token colour class (default `text-primary`, the accent token). Minimum size 16px.
 * Decorative by default; pass `title` to expose it as an image.
 */
export function UmweltMark({ size = 24, title, className }: { size?: number; title?: string; className?: string }) {
  const thick = size <= 32 ? 2.8 : 2.4;
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" className={cn('shrink-0 text-primary', className)}
    role={title ? 'img' : undefined} aria-label={title} aria-hidden={title ? undefined : true} focusable="false">
    <g transform="rotate(-45 12 12)" stroke="currentColor" strokeWidth={thick} strokeLinecap="round">
      <path d="M3.54 8.92A9 9 0 0 1 20.46 8.92" />
      <path d="M20.46 15.08A9 9 0 0 1 3.54 15.08" />
    </g>
    <circle cx="12" cy="12" r="3.2" fill="currentColor" />
  </svg>;
}

/** Mark plus "Umwelt" wordmark with muted "-OS" (Inter 800, -0.03em). Min lockup width 96px. Renders inline content; wrap in a link when needed. */
export function UmweltLogo({ markSize = 24, className }: { markSize?: number; className?: string }) {
  return <span className={cn('inline-flex items-center gap-2 font-extrabold tracking-[-0.03em] text-foreground', className)}>
    <UmweltMark size={markSize} />
    <span>Umwelt<span className="font-bold text-muted-foreground">-OS</span></span>
  </span>;
}
