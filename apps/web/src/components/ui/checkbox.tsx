"use client";

import * as React from 'react';
import { CheckIcon } from 'lucide-react';
import { Checkbox as CheckboxPrimitive } from 'radix-ui';

/** Renders the checkbox UI wrapper and forwards its typed props to the underlying control. */
export function Checkbox(props: React.ComponentProps<typeof CheckboxPrimitive.Root>) {
  return <CheckboxPrimitive.Root data-slot="checkbox" {...props} className={`checkbox disabled:cursor-not-allowed disabled:opacity-50 ${props.className ?? ''}`}><CheckboxPrimitive.Indicator><CheckIcon size={14} /></CheckboxPrimitive.Indicator></CheckboxPrimitive.Root>;
}
