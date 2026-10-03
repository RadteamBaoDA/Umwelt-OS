"use client"

import * as React from "react"
import { cn } from "cn"
import { XIcon } from "lucide-react"
import { Dialog as DialogPrimitive } from "radix-ui"

/**
 * Root Sheet component wrapping the accessible Radix Dialog primitive.
 * Controls the open/closed state of the sliding overlay drawer.
 *
 * @param props - Dialog primitive root properties including open, onOpenChange, and modal.
 * @returns Accessible Sheet root provider element.
 */
function Sheet({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Root>) {
  return <DialogPrimitive.Root data-slot="sheet" {...props} />
}

/**
 * Trigger element that opens the Sheet drawer upon user interaction.
 *
 * @param props - Dialog primitive trigger properties.
 * @returns Trigger button or forwarded child control.
 */
function SheetTrigger({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Trigger>) {
  return <DialogPrimitive.Trigger data-slot="sheet-trigger" {...props} />
}

/**
 * Close element that dismisses the Sheet drawer.
 *
 * @param props - Dialog primitive close properties.
 * @returns Close button or forwarded child control.
 */
function SheetClose({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Close>) {
  return <DialogPrimitive.Close data-slot="sheet-close" {...props} />
}

/**
 * Portal element rendering the Sheet drawer into a detached DOM node.
 *
 * @param props - Dialog primitive portal properties.
 * @returns Portal wrapper rendering into document body.
 */
function SheetPortal({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Portal>) {
  return <DialogPrimitive.Portal data-slot="sheet-portal" {...props} />
}

/**
 * Translucent backdrop overlay behind the Sheet drawer.
 *
 * @param className - Optional CSS classes for overlay styling.
 * @param props - Additional Dialog primitive overlay properties.
 * @returns Fixed overlay backdrop element.
 */
function SheetOverlay({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Overlay>) {
  return (
    <DialogPrimitive.Overlay
      data-slot="sheet-overlay"
      className={cn(
        "fixed inset-0 z-50 bg-foreground/40 backdrop-blur-[2px] transition-opacity duration-300 data-[state=closed]:opacity-0 data-[state=open]:opacity-100",
        className
      )}
      {...props}
    />
  )
}

/**
 * Slide direction for the Sheet drawer.
 */
export type SheetSide = "top" | "bottom" | "left" | "right"

interface SheetContentProps extends React.ComponentProps<typeof DialogPrimitive.Content> {
  side?: SheetSide
  showCloseButton?: boolean
  closeLabel?: string
}

/**
 * Accessible Sheet content container with responsive sliding animation.
 * Defaults to a right-side drawer that occupies full viewport on mobile
 * and large responsive width on desktop (up to max-w-4xl).
 * Traps focus, handles Escape key, and restores focus on dismissal.
 *
 * @param side - Position from which the drawer slides in (default: 'right').
 * @param showCloseButton - Whether to display the default close button icon.
 * @param closeLabel - Accessible label for the close button.
 * @param className - Additional class names for custom layout.
 * @param children - Drawer content elements.
 * @param props - Additional Dialog primitive content properties.
 * @returns Formatted modal sheet container.
 */
function SheetContent({
  side = "right",
  className,
  children,
  showCloseButton = true,
  closeLabel = "Close",
  ...props
}: SheetContentProps) {
  const sideClasses: Record<SheetSide, string> = {
    top: "inset-x-0 top-0 border-b data-[state=closed]:-translate-y-full data-[state=open]:translate-y-0",
    bottom: "inset-x-0 bottom-0 border-t data-[state=closed]:translate-y-full data-[state=open]:translate-y-0",
    left: "inset-y-0 left-0 h-full border-r data-[state=closed]:-translate-x-full data-[state=open]:translate-x-0",
    right: "inset-y-0 right-0 h-full border-l data-[state=closed]:translate-x-full data-[state=open]:translate-x-0",
  }

  return (
    <SheetPortal data-slot="sheet-portal">
      <SheetOverlay />
      <DialogPrimitive.Content
        data-slot="sheet-content"
        className={cn(
          "fixed z-50 flex flex-col bg-background shadow-2xl transition-transform duration-300 ease-in-out outline-none",
          sideClasses[side],
          side === "right" && "w-full sm:max-w-xl md:max-w-2xl lg:max-w-3xl xl:max-w-4xl",
          className
        )}
        {...props}
      >
        {children}
        {showCloseButton && (
          <DialogPrimitive.Close
            data-slot="sheet-close"
            className="absolute top-4 right-4 z-10 rounded-md p-1.5 opacity-70 transition-opacity hover:opacity-100 focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 disabled:pointer-events-none"
            aria-label={closeLabel}
          >
            <XIcon className="size-5" />
            <span className="sr-only">{closeLabel}</span>
          </DialogPrimitive.Close>
        )}
      </DialogPrimitive.Content>
    </SheetPortal>
  )
}

/**
 * Header section for the Sheet drawer with standardized spacing.
 *
 * @param className - Optional CSS classes for header container.
 * @param props - Additional div container properties.
 * @returns Header container element.
 */
function SheetHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sheet-header"
      className={cn("flex flex-col gap-1.5 p-4 border-b border-border shrink-0", className)}
      {...props}
    />
  )
}

/**
 * Footer section for the Sheet drawer with standardized spacing.
 *
 * @param className - Optional CSS classes for footer container.
 * @param props - Additional div container properties.
 * @returns Footer container element.
 */
function SheetFooter({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sheet-footer"
      className={cn("mt-auto flex flex-col-reverse sm:flex-row sm:justify-end gap-2 p-4 border-t border-border shrink-0", className)}
      {...props}
    />
  )
}

/**
 * Accessible title component for the Sheet drawer.
 *
 * @param className - Optional CSS classes for title styling.
 * @param props - Additional Dialog primitive title properties.
 * @returns Accessible heading element.
 */
function SheetTitle({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Title>) {
  return (
    <DialogPrimitive.Title
      data-slot="sheet-title"
      className={cn("text-lg font-semibold tracking-tight text-foreground", className)}
      {...props}
    />
  )
}

/**
 * Accessible description component for the Sheet drawer.
 *
 * @param className - Optional CSS classes for description styling.
 * @param props - Additional Dialog primitive description properties.
 * @returns Accessible subtitle description element.
 */
function SheetDescription({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Description>) {
  return (
    <DialogPrimitive.Description
      data-slot="sheet-description"
      className={cn("text-sm text-muted-foreground", className)}
      {...props}
    />
  )
}

export {
  Sheet,
  SheetTrigger,
  SheetClose,
  SheetContent,
  SheetHeader,
  SheetFooter,
  SheetTitle,
  SheetDescription,
}
