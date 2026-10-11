'use client';

import * as React from 'react';
import { Tooltip as TooltipPrimitive } from 'radix-ui';

/** Provides shared hover and focus behavior for one or more tooltip triggers. */
export function TooltipProvider({ ...props }: React.ComponentProps<typeof TooltipPrimitive.Provider>) {
  return <TooltipPrimitive.Provider data-slot="tooltip-provider" {...props} />;
}

/** Owns open state and timing for a tooltip. */
export function Tooltip({ ...props }: React.ComponentProps<typeof TooltipPrimitive.Root>) {
  return <TooltipPrimitive.Root data-slot="tooltip" {...props} />;
}

/** Wraps the control that reveals its tooltip on hover, focus, or touch. */
export function TooltipTrigger({ ...props }: React.ComponentProps<typeof TooltipPrimitive.Trigger>) {
  return <TooltipPrimitive.Trigger data-slot="tooltip-trigger" {...props} />;
}

/** Portals a theme-aware tooltip above its trigger while preserving Radix dismissal and focus behavior. */
export function TooltipContent({ className, side = 'top', sideOffset = 8, ...props }: React.ComponentProps<typeof TooltipPrimitive.Content>) {
  return (
    <TooltipPrimitive.Portal>
      <TooltipPrimitive.Content
        data-slot="tooltip-content"
        side={side}
        sideOffset={sideOffset}
        className={`ui-tooltip ${className ?? ''}`}
        {...props}
      />
    </TooltipPrimitive.Portal>
  );
}
