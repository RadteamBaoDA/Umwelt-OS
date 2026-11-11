'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { UploadIcon } from 'lucide-react';

/** Props of {@link ChatDropZone}. */
export interface ChatDropZoneProps {
  /** Full-page surface only: when false the zone is inert and browser drag behavior is untouched. */
  enabled: boolean;
  /** Whether attachments can be added right now (not busy, available, below the limit). */
  accepting: boolean;
  /** Receives dropped files; same sink as the composer's paste handler. */
  onFiles: (files: File[]) => void;
  /** Receives the wrapper element (chat surface) for focus-scoped shortcuts. */
  rootRef?: React.Ref<HTMLDivElement>;
  className?: string;
  children: React.ReactNode;
}

/** True when a drag carries files (text and link drags must not trigger the overlay). */
function carriesFiles(event: React.DragEvent): boolean {
  return Array.from(event.dataTransfer?.types ?? []).includes('Files');
}

/**
 * Wraps the chat surface so files can be dropped anywhere on it, with a visible drop overlay. While enabled, file
 * drags are always claimed (default prevented) so a drop on a busy chat never navigates the tab to the file; files are
 * forwarded only when `accepting`.
 *
 * @param props - See {@link ChatDropZoneProps}.
 * @returns The wrapped surface plus the overlay while a file drag is over it.
 */
export function ChatDropZone({ enabled, accepting, onFiles, rootRef, className, children }: ChatDropZoneProps) {
  const t = useTranslations('chat');
  const [dragging, setDragging] = React.useState(false);
  // dragenter/dragleave fire for every child boundary; the depth counter tells when the pointer truly left the zone.
  const depthRef = React.useRef(0);

  /** Starts (or nests) a file drag over the surface. */
  const handleDragEnter = (event: React.DragEvent) => {
    if (!enabled || !carriesFiles(event)) return;
    event.preventDefault();
    depthRef.current += 1;
    if (accepting) setDragging(true);
  };

  /** Keeps the surface a valid drop target. */
  const handleDragOver = (event: React.DragEvent) => {
    if (!enabled || !carriesFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = accepting ? 'copy' : 'none';
  };

  /** Ends a nested drag; hides the overlay when the pointer left the surface. */
  const handleDragLeave = (event: React.DragEvent) => {
    if (!enabled || !carriesFiles(event)) return;
    depthRef.current = Math.max(0, depthRef.current - 1);
    if (depthRef.current === 0) setDragging(false);
  };

  /** Forwards dropped files to the attachment sink. */
  const handleDrop = (event: React.DragEvent) => {
    if (!enabled || !carriesFiles(event)) return;
    event.preventDefault();
    depthRef.current = 0;
    setDragging(false);
    const files = Array.from(event.dataTransfer.files);
    if (accepting && files.length > 0) onFiles(files);
  };

  return (
    <div
      ref={rootRef}
      className={className}
      onDragEnter={handleDragEnter}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {children}
      {dragging && (
        <div
          data-testid="chat-drop-overlay"
          className="pointer-events-none absolute inset-2 z-30 flex items-center justify-center gap-2 rounded-lg border-2 border-dashed border-data bg-surface text-sm font-medium text-foreground"
        >
          <UploadIcon className="size-4" aria-hidden="true" />
          {t('dropFilesHint')}
        </div>
      )}
    </div>
  );
}
