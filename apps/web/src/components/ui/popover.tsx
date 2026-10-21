'use client';

import * as React from 'react';
import { Popover as PopoverPrimitive } from 'radix-ui';

/** Owns open state for a popover. */
export function Popover({ ...props }: React.ComponentProps<typeof PopoverPrimitive.Root>) {
  return <PopoverPrimitive.Root data-slot="popover" {...props} />;
}

/** Wraps the control that toggles the popover. */
export function PopoverTrigger({ ...props }: React.ComponentProps<typeof PopoverPrimitive.Trigger>) {
  return <PopoverPrimitive.Trigger data-slot="popover-trigger" {...props} />;
}

/** Portals themed popover content below its trigger; reuses the select-content surface tokens and matches the trigger width. */
export function PopoverContent({ className, align = 'start', sideOffset = 4, ...props }: React.ComponentProps<typeof PopoverPrimitive.Content>) {
  return (
    <PopoverPrimitive.Portal>
      <PopoverPrimitive.Content data-slot="popover-content" align={align} sideOffset={sideOffset} className={`select-content ${className ?? ''}`} {...props} />
    </PopoverPrimitive.Portal>
  );
}
