'use client';

import * as React from 'react';
import { AlertDialog as AlertDialogPrimitive } from 'radix-ui';
import { Button } from '@/components/ui/button';
import { cn } from 'cn';

export const AlertDialog = AlertDialogPrimitive.Root;
export const AlertDialogTrigger = AlertDialogPrimitive.Trigger;

/** Renders alert-dialog content in its portal while forwarding primitive props. */
export function AlertDialogContent({ children, className, ...props }: React.ComponentProps<typeof AlertDialogPrimitive.Content>) {
  return <AlertDialogPrimitive.Portal><AlertDialogPrimitive.Overlay className="fixed inset-0 z-50 bg-foreground/50" /><AlertDialogPrimitive.Content {...props} className={cn('fixed left-1/2 top-1/2 z-50 grid max-h-[calc(100dvh-2rem-env(safe-area-inset-top)-env(safe-area-inset-bottom))] w-full max-w-[calc(100%-2rem)] -translate-x-1/2 -translate-y-1/2 gap-4 overflow-y-auto overscroll-contain rounded-lg border bg-background p-6 shadow-lg outline-none sm:max-w-lg', className)}>{children}</AlertDialogPrimitive.Content></AlertDialogPrimitive.Portal>;
}

/** Groups the alert-dialog title and description using the shared header layout. */
export function AlertDialogHeader({ children, className, ...props }: React.ComponentProps<'div'>) {
  return <div {...props} className={cn('flex flex-col gap-2 text-left', className)}>{children}</div>;
}

/** Groups alert-dialog actions using the shared responsive footer layout. */
export function AlertDialogFooter({ children, className, ...props }: React.ComponentProps<'div'>) {
  return <div {...props} className={cn('flex flex-col-reverse gap-2 sm:flex-row sm:justify-end', className)}>{children}</div>;
}

export const AlertDialogTitle = AlertDialogPrimitive.Title;
export const AlertDialogDescription = AlertDialogPrimitive.Description;

/** Renders the primitive cancel action with the shared secondary button style. */
export function AlertDialogCancel(props: React.ComponentProps<typeof AlertDialogPrimitive.Cancel>) {
  return <AlertDialogPrimitive.Cancel asChild><Button className="secondary" {...props} /></AlertDialogPrimitive.Cancel>;
}

/** Renders the primitive confirm action with the shared primary button style. */
export function AlertDialogAction(props: React.ComponentProps<typeof AlertDialogPrimitive.Action>) {
  return <AlertDialogPrimitive.Action asChild><Button {...props} /></AlertDialogPrimitive.Action>;
}

