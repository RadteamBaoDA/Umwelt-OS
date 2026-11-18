import { Loader2Icon } from 'lucide-react';
import { cn } from '@/lib/utils';
import './spinner.css';

/**
 * Loading indicator shared by every spinner in the app. Renders two decorative siblings, both `aria-hidden`:
 * the lucide `Loader2Icon` (default skin) and a braille glyph span whose frames are animated by CSS in the
 * terminal skin. Which one shows is decided by `spinner.css`, so no JS timer runs. Callers keep their own
 * live-region or label semantics; this component carries none.
 *
 * @param props.className Size and colour classes applied to both siblings (e.g. `size-4 text-primary`).
 * @returns Two sibling elements; the icon keeps the `animate-spin` class that route readiness checks look for.
 */
export function Spinner({ className }: { className?: string }) {
  return (
    <>
      <Loader2Icon className={cn('spinner-icon animate-spin', className)} aria-hidden="true" />
      <span className={cn('spinner-glyph', className)} aria-hidden="true" />
    </>
  );
}
