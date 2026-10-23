'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertCircle, Layout, Plus, RotateCw } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { useState } from 'react';
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { Button } from '@/components/ui/button';
import { ApiError } from '@/core/api';
import { apiFailureKey } from '@/core/api-failure-key';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import {
  dashboardKeys, deleteGadgetDefinition, evaluateGadgetHighlights, listGadgetDefinitions, type GadgetDefinition,
} from '@/modules/dashboard/api';
import { GadgetEditor, rendererName } from '@/modules/dashboard/gadget-editor';

/** Splits comma-separated text into unique, trimmed, non-empty entries. */
export function parseKeywordList(value: string): string[] {
  return [...new Set(value.split(',').map((item) => item.trim()).filter(Boolean))];
}

/**
 * Gadget library tab: reusable definitions on the left, the shared gadget editor on the right.
 * Saving is revision-guarded; rich highlight rules are edited in their own tab and preserved unless changed in the editor.
 */
export function GadgetLibrary({ onEditRules }: { onEditRules: (definitionId: string) => void }) {
  const t = useTranslations('gadgetSettings');
  const defs = useQuery({ queryKey: dashboardKeys.definitions, queryFn: ({ signal }) => listGadgetDefinitions(100, signal) });
  const [selected, setSelected] = useState<string | 'new' | null>(null);
  const definitions = defs.data ?? [];
  const current = selected && selected !== 'new' ? definitions.find((item) => item.id === selected) ?? null : null;
  return <div className="grid gap-4 md:grid-cols-[minmax(0,18rem)_minmax(0,1fr)]">
    <aside aria-label={t('libraryTitle')} className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-base font-semibold">{t('libraryTitle')}</h2>
        <Button type="button" variant="outline" onClick={() => setSelected('new')}><Plus className="size-4" aria-hidden="true" /> {t('newGadget')}</Button>
      </div>
      <p className="text-muted-foreground">{t('libraryIntro')}</p>
      {defs.isPending && <div className="skeleton h-16" role="status" aria-label={t('loading')} />}
      {defs.isError && <div role="alert" className="space-y-2"><p className="text-destructive">{t(apiFailureKey(defs.error) ?? 'loadFailed')}</p><Button type="button" variant="outline" onClick={() => defs.refetch()}><RotateCw className="size-4" aria-hidden="true" /> {t('retry')}</Button></div>}
      {defs.isSuccess && definitions.length === 0 && <div className="rounded-lg border border-dashed border-border p-4 text-center"><Layout className="mx-auto mb-2 size-6" aria-hidden="true" /><p className="font-semibold">{t('empty')}</p><p className="text-muted-foreground">{t('emptyHelp')}</p></div>}
      <ul className="space-y-2">{definitions.map((def) => <li key={def.id}>
        <button type="button" aria-current={selected === def.id ? 'true' : undefined} onClick={() => setSelected(def.id)} className={`min-h-11 w-full rounded-lg border p-3 text-left ${selected === def.id ? 'border-primary bg-card' : 'border-border'}`}>
          <span className="block truncate font-semibold">{def.name}</span>
          <span className="text-muted-foreground block text-xs">{t('gadgetMeta', { renderer: rendererName(t, def.renderer), revision: def.revision, rules: def.highlight_rules?.length ?? 0 })}</span>
        </button>
      </li>)}</ul>
    </aside>
    <div>
      {selected === null && <p className="text-muted-foreground rounded-lg border border-dashed border-border p-6">{t('selectPrompt')}</p>}
      {selected !== null && (selected === 'new' || current) && <GadgetForm key={current ? `${current.id}:${current.revision}` : 'new'} def={current} onDone={(id) => setSelected(id)} onEditRules={onEditRules} />}
    </div>
  </div>;
}

/**
 * Library detail pane for one gadget definition (new or saved): the shared GadgetEditor plus the saved-gadget extras
 * (highlight-rule shortcut, preview of current matches, guarded delete in the editor footer).
 */
function GadgetForm({ def, onDone, onEditRules }: { def: GadgetDefinition | null; onDone: (id: string | null) => void; onEditRules: (id: string) => void }) {
  const t = useTranslations('gadgetSettings');
  const session = useWorkspaceSession();
  const client = useQueryClient();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const preview = useQuery({ queryKey: [...dashboardKeys.definition(def?.id ?? 'new'), 'highlights', def?.revision], enabled: Boolean(def), queryFn: () => evaluateGadgetHighlights(def!.id) });
  const remove = useMutation({
    mutationFn: () => deleteGadgetDefinition(def!.id, def!.revision, session.csrfToken),
    onSuccess: () => { setConfirmDelete(false); void client.invalidateQueries({ queryKey: dashboardKeys.definitions }); onDone(null); },
  });
  return <div className="flex flex-col gap-5 rounded-lg border border-border bg-card p-5">
    <GadgetEditor definition={def} onSaved={(saved) => onDone(saved.id)} onCancel={() => onDone(def ? def.id : null)}
      footerExtra={def && <Button type="button" variant="destructive" disabled={remove.isPending} onClick={() => setConfirmDelete(true)}>{t('delete')}</Button>} />
    {def && <p className="text-muted-foreground">{t('rulesCount', { count: def.highlight_rules?.length ?? 0 })} <button type="button" className="underline" onClick={() => onEditRules(def.id)}>{t('editRules')}</button></p>}
    <section aria-labelledby="gl-preview" className="space-y-2 rounded-lg border border-border p-3">
      <h3 id="gl-preview" className="font-semibold">{t('preview')}</h3>
      {!def && <p className="text-muted-foreground">{t('previewNew')}</p>}
      {def && preview.isPending && <p className="text-muted-foreground" role="status">{t('previewLoading')}</p>}
      {def && preview.isError && <p className="text-destructive" role="alert"><AlertCircle className="inline size-4" aria-hidden="true" /> {t('previewError')}</p>}
      {def && preview.isSuccess && preview.data.length === 0 && <p className="text-muted-foreground">{t('previewEmpty')}</p>}
      {def && preview.isSuccess && <ul className="space-y-1">{preview.data.map((match) => <li key={`${match.rule_id}:${match.document_version_id}`}><span className="font-medium">{match.title}</span> <span className="text-muted-foreground text-xs">{t('previewMatch', { keywords: match.matched_keywords.join(', ') })}</span></li>)}</ul>}
      {def && <p className="text-muted-foreground text-xs">{t('previewNote')}</p>}
    </section>
    {remove.error && <p className="text-destructive" role="alert">{t(remove.error instanceof ApiError && remove.error.code === 'definition_in_use' ? 'inUse' : apiFailureKey(remove.error) === 'conflict' ? 'conflict' : 'deleteFailed')}</p>}
    <AlertDialog open={confirmDelete} onOpenChange={setConfirmDelete}>
      <AlertDialogContent><AlertDialogHeader><AlertDialogTitle>{t('deleteTitle')}</AlertDialogTitle><AlertDialogDescription>{t('deleteBody')}</AlertDialogDescription></AlertDialogHeader>
        <AlertDialogFooter><AlertDialogCancel>{t('cancel')}</AlertDialogCancel><AlertDialogAction onClick={() => remove.mutate()}>{t('deleteConfirm')}</AlertDialogAction></AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </div>;
}
