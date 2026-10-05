'use client';

import { FileText, Sparkles } from 'lucide-react';
import React from 'react';
import type { GadgetInstance } from '../api';

/** Props for the TextPanel gadget component. */
export interface TextPanelProps {
  /** Gadget instance configuration projection. */
  instance: GadgetInstance;
  /** Optional markdown or plain text content. */
  content?: string;
}

/**
 * Standard Text Panel gadget template.
 * Displays formatted notes, intelligence memos, or document summaries with sanitized text rendering.
 *
 * @param props Gadget instance configuration and optional text content.
 * @returns Accessible text panel component.
 */
export function TextPanel({ instance, content }: TextPanelProps) {
  const definition = instance.definition;
  const defaultText =
    content ||
    `### Intelligence Summary\n\nAll verified knowledge links remain synchronized with persistent storage. No conflict anomalies detected in active projections.`;

  return (
    <div className="flex flex-col h-full bg-card text-card-foreground p-3.5 space-y-3 overflow-y-auto">
      <div className="flex items-center justify-between text-xs text-muted-foreground border-b border-border pb-2">
        <div className="flex items-center gap-1.5 font-medium">
          <FileText className="w-3.5 h-3.5 text-primary" />
          <span>Note & Memo</span>
        </div>
        <span className="font-mono text-[10px]">rev.{definition.revision}</span>
      </div>

      <div className="text-xs leading-relaxed text-foreground/90 whitespace-pre-wrap font-sans">
        {defaultText}
      </div>
    </div>
  );
}
