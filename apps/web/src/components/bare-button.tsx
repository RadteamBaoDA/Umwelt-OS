import { forwardRef } from 'react';
import { Button, type ButtonProps } from '@/components/ui/button';

/**
 * Renders the shared `Button` primitive with its `bare` variant, which emits no styling classes. The result is a plain
 * native button (inline-block, no chrome) so feature modules can restyle list rows, cards, link-style and icon toggles
 * with their own classes, including component-layer CSS classes such as `.avatar-button`, `.filters-toggle` and
 * `.text-button`, without Tailwind utilities overriding them.
 * It forwards the ref and every native button prop, so DOM semantics (type, aria-*, disabled, data-testid) are unchanged.
 * @param props Standard `Button` props; `className` is applied as-is. `variant` is fixed to `bare`.
 */
export const BareButton = forwardRef<HTMLButtonElement, ButtonProps>(function BareButton({ className, ...props }, ref) {
  return <Button ref={ref} {...props} variant="bare" className={className} />;
});
