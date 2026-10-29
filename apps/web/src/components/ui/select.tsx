"use client";

import * as React from 'react';
import { CheckIcon, ChevronDownIcon } from 'lucide-react';
import { Select as SelectPrimitive } from 'radix-ui';
import { titleIfClipped } from './tooltip';

export const Select = SelectPrimitive.Root;
export const SelectGroup = SelectPrimitive.Group;
export const SelectValue = SelectPrimitive.Value;
/** Renders the select trigger as a start-aligned flex row with a trailing muted chevron; width comes from caller utilities. */
export function SelectTrigger({ children, className, ...props }: React.ComponentProps<typeof SelectPrimitive.Trigger>) {
  return <SelectPrimitive.Trigger data-slot="select-trigger" onPointerEnter={titleIfClipped} {...props} className={`input flex items-center justify-between gap-2 text-start ${className ?? ''}`}>{children}<SelectPrimitive.Icon asChild><ChevronDownIcon size={16} className="shrink-0 text-muted-foreground" aria-hidden="true" /></SelectPrimitive.Icon></SelectPrimitive.Trigger>;
}
/** Renders the select content UI wrapper and forwards its typed props to the underlying control. */
export function SelectContent({ children, ...props }: React.ComponentProps<typeof SelectPrimitive.Content>) {
  return <SelectPrimitive.Portal><SelectPrimitive.Content data-slot="select-content" {...props} className={`select-content ${props.className ?? ''}`}><SelectPrimitive.Viewport>{children}</SelectPrimitive.Viewport></SelectPrimitive.Content></SelectPrimitive.Portal>;
}
/** Renders the select item UI wrapper and forwards its typed props to the underlying control. */
export function SelectItem({ children, ...props }: React.ComponentProps<typeof SelectPrimitive.Item>) {
  return <SelectPrimitive.Item data-slot="select-item" onPointerEnter={titleIfClipped} {...props} className={`select-item ${props.className ?? ''}`}><SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText><SelectPrimitive.ItemIndicator className="select-item-indicator"><CheckIcon size={14} /></SelectPrimitive.ItemIndicator></SelectPrimitive.Item>;
}
