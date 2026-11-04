'use client';

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Check, Pencil, X } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { renameSource, sourceKeys, type Source } from './api';

/** Longest source name the server accepts (SourcePatch.name). */
export const SOURCE_NAME_MAX = 200;

/**
 * Source name with an inline rename: pencil opens an input, Enter or the check button saves, Escape or the X cancels.
 * Saving sends only `name` (PATCH), so the connector configuration and its revision are never touched.
 * An empty or whitespace-only name cannot be saved and the input stops at 200 characters; failures keep the editor open.
 * Archived sources are shown read-only. `onRenamed` runs after the source caches are invalidated.
 */
export function RenameSource({ source, onRenamed }: { source: Pick<Source, 'id' | 'name' | 'status'>; onRenamed?: () => void }) {
  const t = useTranslations('sources');
  const { csrfToken } = useWorkspaceSession();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  // Callers may hold a stale Source snapshot (the edit dialog); show the saved name until the prop catches up.
  const [saved, setSaved] = useState<{ from: string; to: string } | null>(null);
  const shown = saved && saved.from === source.name ? saved.to : source.name;
  const [draft, setDraft] = useState(shown);
  const trimmed = draft.trim();
  const valid = trimmed.length > 0 && trimmed.length <= SOURCE_NAME_MAX;
  const rename = useMutation({
    mutationFn: () => renameSource(source.id, trimmed, csrfToken),
    onSuccess: () => {
      setSaved({ from: source.name, to: trimmed });
      setEditing(false);
      void queryClient.invalidateQueries({ queryKey: sourceKeys.all });
      onRenamed?.();
    },
  });

  /** Saves when the draft is valid and unchanged names simply close the editor. */
  const save = () => {
    if (!valid || rename.isPending) return;
    if (trimmed === shown) { setEditing(false); return; }
    rename.mutate();
  };
  /** Discards the draft and returns to the read-only name. */
  const cancel = () => { rename.reset(); setDraft(shown); setEditing(false); };

  if (!editing) {
    return <span className="inline-flex items-center gap-1">
      <strong>{shown}</strong>
      {source.status !== 'archived' && <Button type="button" size="icon" variant="ghost" aria-label={t('renameSource', { name: shown })}
        onClick={() => { setDraft(shown); setEditing(true); }}><Pencil size={14} aria-hidden="true" /></Button>}
    </span>;
  }
  return <span className="inline-flex flex-wrap items-center gap-1">
    <Input autoFocus aria-label={t('sourceName')} aria-invalid={!valid} className="h-8 w-56 max-w-full" maxLength={SOURCE_NAME_MAX} value={draft} disabled={rename.isPending}
      onChange={(event) => setDraft(event.target.value)}
      onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); save(); } else if (event.key === 'Escape') { event.stopPropagation(); cancel(); } }} />
    <Button type="button" size="icon" variant="ghost" aria-label={t('renameSave')} disabled={!valid || rename.isPending} onClick={save}><Check size={14} aria-hidden="true" /></Button>
    <Button type="button" size="icon" variant="ghost" aria-label={t('renameCancel')} disabled={rename.isPending} onClick={cancel}><X size={14} aria-hidden="true" /></Button>
    {rename.isError && <span className="text-sm text-destructive" role="alert">{t('renameFailed')}</span>}
  </span>;
}
