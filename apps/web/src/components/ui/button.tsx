import { cn } from 'cn';
import { cva, type VariantProps } from 'class-variance-authority';
import { Slot } from 'radix-ui';
import { forwardRef, type ButtonHTMLAttributes } from 'react';

/**
 * Build the shared button classes for the New York shadcn variant and size contract.
 * The literal `button` class preserves existing selectors; semantic tokens own its colors,
 * and every size retains the 44px minimum touch target.
 */
export const buttonVariants = cva(
  'button inline-flex min-h-11 cursor-pointer items-center justify-center gap-2 whitespace-nowrap rounded-[9px] border-0 text-[15px] font-bold transition-colors disabled:cursor-progress disabled:opacity-[0.65] [&_svg]:shrink-0',
  {
    variants: {
      variant: {
        default: 'bg-primary text-primary-foreground hover:bg-primary/90',
        destructive: 'bg-destructive text-destructive-foreground hover:bg-destructive/90',
        outline: 'border border-input bg-transparent text-foreground hover:bg-secondary hover:text-secondary-foreground',
        secondary: 'bg-secondary text-secondary-foreground hover:bg-secondary/80',
        ghost: 'bg-transparent text-foreground hover:bg-secondary hover:text-secondary-foreground',
        link: 'bg-transparent text-primary underline-offset-4 hover:underline',
      },
      size: {
        default: 'px-4 py-2.5',
        sm: 'px-3 py-2 text-sm',
        lg: 'px-6 py-3',
        icon: 'min-w-11 min-h-11 p-0',
      },
    },
    defaultVariants: {
      variant: 'default',
      size: 'default',
    },
  },
);

/** Props accepted by the shared New York shadcn button, including its Radix Slot mode. */
export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> &
  VariantProps<typeof buttonVariants> & { asChild?: boolean };

/**
 * Render a native or slotted button with variant classes and forward its ref and native props.
 * Explicit variants win; the standalone legacy `secondary` class maps to `outline` when omitted.
 * Native disabled behavior is preserved, while Slot mode transfers props to its single child.
 * @param props Native button props plus variant, size, className, and optional asChild selection.
 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    // Preserve the old `.secondary` appearance only for callers that have no explicit variant.
    const effectiveVariant = variant ?? (className?.split(/\s+/).includes('secondary') ? 'outline' : 'default');
    const Component = asChild ? Slot.Root : 'button';

    return (
      <Component
        {...props}
        ref={ref}
        data-slot="button"
        data-variant={effectiveVariant}
        data-size={size ?? 'default'}
        className={cn(buttonVariants({ variant: effectiveVariant, size, className }))}
      />
    );
  },
);

Button.displayName = 'Button';
