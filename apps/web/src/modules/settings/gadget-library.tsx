'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertCircle, Check, Layout, Plus, RotateCw } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { useState } from 'react';
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { ApiError } from '@/core/api';
import { apiFailureKey } from '@/core/api-failure-key';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import {
  createGadgetDefinition, dashboardKeys, deleteGadgetDefinition, evaluateGadgetHighlights, listGadgetDefinitions,
  listGadgetRenderers, patchGadgetDefinition, type GadgetDefinition,
} from '@/modules/dashboard/api';
import { MULTI_SOURCE_RENDERERS, SOURCE_BACKED_RENDERERS, SourceMultiPicker, SourcePicker } from '@/modules/dashboard/source-picker';

/** Splits comma-separated text into unique, trimmed, non-empty entries. */
export function parseKeywordList(value: string): string[] {
  return [...new Set(value.split(',').map((item) => item.trim()).filter(Boolean))];
}

type Draft = { name: string; renderer: string; sourceIds: string[]; keywords: string; excludeKeywords: string; limit: string };

/** Builds an editable draft from a saved definition, or a blank one for a new gadget. */
function makeDraft(def: GadgetDefinition | null): Draft {
  return { name: def?.name ?? '', renderer: def?.renderer ?? '', sourceIds: def?.source_ids ?? [], keywords: (def?.filters?.keywords ?? []).join(', '), excludeKeywords: (def?.filters?.exclude_keywords ?? []).join(', '), limit: String(def?.filters?.limit ?? 25) };
}

/**
 * Gadget library tab: reusable definitions on the left, a four-step editor on the right.
 * Saving is revision-guarded; highlight rules are edited in their own tab and preserved here.
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
        <Button type="button" className="secondary" onClick={() => setSelected('new')}><Plus className="size-4" aria-hidden="true" /> {t('newGadget')}</Button>
      </div>
      <p className="muted">{t('libraryIntro')}</p>
      {defs.isPending && <div className="skeleton h-16" role="status" aria-label={t('loading')} />}
      {defs.isError && <div role="alert" className="space-y-2"><p className="error">{t(apiFailureKey(defs.error) ?? 'loadFailed')}</p><Button type="button" className="secondary" onClick={() => defs.refetch()}><RotateCw className="size-4" aria-hidden="true" /> {t('retry')}</Button></div>}
      {defs.isSuccess && definitions.length === 0 && <div className="rounded-lg border border-dashed border-border p-4 text-center"><Layout className="mx-auto mb-2 size-6" aria-hidden="true" /><p className="font-semibold">{t('empty')}</p><p className="muted">{t('emptyHelp')}</p></div>}
      <ul className="space-y-2">{definitions.map((def) => <li key={def.id}>
        <button type="button" aria-current={selected === def.id ? 'true' : undefined} onClick={() => setSelected(def.id)} className={`min-h-11 w-full rounded-lg border p-3 text-left ${selected === def.id ? 'border-primary bg-card' : 'border-border'}`}>
          <span className="block truncate font-semibold">{def.name}</span>
          <span className="muted block text-xs">{t('gadgetMeta', { renderer: def.renderer, revision: def.revision, rules: def.highlight_rules?.length ?? 0 })}</span>
        </button>
      </li>)}</ul>
    </aside>
    <div>
      {selected === null && <p className="muted rounded-lg border border-dashed border-border p-6">{t('selectPrompt')}</p>}
      {selected !== null && (selected === 'new' || current) && <GadgetForm key={current ? `${current.id}:${current.revision}` : 'new'} def={current} onDone={(id) => setSelected(id)} onEditRules={onEditRules} />}
    </div>
  </div>;
}

/** Four-step editor for one gadget definition (new or saved). */
function GadgetForm({ def, onDone, onEditRules }: { def: GadgetDefinition | null; onDone: (id: string | null) => void; onEditRules: (id: string) => void }) {
  const t = useTranslations('gadgetSettings');
  const session = useWorkspaceSession();
  const client = useQueryClient();
  const [draft, setDraft] = useState<Draft>(() => makeDraft(def));
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [touched, setTouched] = useState(false);
  const renderers = useQuery({ queryKey: dashboardKeys.renderers, queryFn: ({ signal }) => listGadgetRenderers(signal) });
  const preview = useQuery({ queryKey: [...dashboardKeys.definition(def?.id ?? 'new'), 'highlights', def?.revision], enabled: Boolean(def), queryFn: () => evaluateGadgetHighlights(def!.id) });
  const dirty = JSON.stringify(draft) !== JSON.stringify(makeDraft(def));
  const keywords = parseKeywordList(draft.keywords);
  const excludeKeywords = parseKeywordList(draft.excludeKeywords);
  const limit = Number(draft.limit);
  const limitValid = Number.isInteger(limit) && limit >= 1 && limit <= 100;
  const valid = draft.name.trim().length > 0 && draft.renderer !== '' && limitValid && keywords.length <= 32 && excludeKeywords.length <= 32;
  const provider = SOURCE_BACKED_RENDERERS[draft.renderer]?.provider;
  const multi = MULTI_SOURCE_RENDERERS.has(draft.renderer);
  const set = (patch: Partial<Draft>) => setDraft((value) => ({ ...value, ...patch }));
  const save = useMutation({
    mutationFn: () => {
      const filters = { ...(def?.filters ?? {}), keywords, exclude_keywords: excludeKeywords, limit };
      return def
        ? patchGadgetDefinition(def.id, { expected_revision: def.revision, name: draft.name.trim(), source_ids: draft.sourceIds, filters }, session.csrfToken)
        : createGadgetDefinition({ name: draft.name.trim(), renderer: draft.renderer, source_ids: draft.sourceIds, filters }, session.csrfToken);
    },
    onSuccess: (saved) => { void client.invalidateQueries({ queryKey: dashboardKeys.definitions }); onDone(saved.id); },
  });
  const remove = useMutation({
    mutationFn: () => deleteGadgetDefinition(def!.id, def!.revision, session.csrfToken),
    onSuccess: () => { setConfirmDelete(false); void client.invalidateQueries({ queryKey: dashboardKeys.definitions }); onDone(null); },
  });
  return <form className="space-y-5" onSubmit={(event) => { event.preventDefault(); setTouched(true); if (valid) save.mutate(); }}>
    <fieldset disabled={save.isPending} className="space-y-5">
      <label className="field"><span className="label">{t('name')}</span><Input value={draft.name} maxLength={120} aria-invalid={touched && !draft.name.trim()} onChange={(event) => set({ name: event.target.value })} />{touched && !draft.name.trim() && <span className="error" role="alert">{t('nameRequired')}</span>}</label>
      <section aria-labelledby="gl-step1" className="space-y-2">
        <h3 id="gl-step1" className="font-semibold">1. {t('step1')}</h3>
        {def && <p className="muted">{t('rendererLocked')}</p>}
        {renderers.isPending && <div className="skeleton h-12" role="status" aria-label={t('loading')} />}
        {renderers.isError && <p className="error" role="alert">{t(apiFailureKey(renderers.error) ?? 'loadFailed')} <Button type="button" className="secondary" onClick={() => renderers.refetch()}>{t('retry')}</Button></p>}
        <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{(renderers.data ?? []).map((renderer) => {
          const unavailable = renderer.runtime_state !== 'available';
          const disabled = unavailable || Boolean(def);
          const active = draft.renderer === renderer.id;
          return <li key={renderer.id}><button type="button" disabled={disabled && !active} aria-pressed={active} aria-describedby={unavailable ? `gl-r-${renderer.id}` : undefined} onClick={() => set({ renderer: renderer.id, sourceIds: [] })} className={`min-h-11 w-full rounded-lg border p-3 text-left disabled:opacity-60 ${active ? 'border-primary bg-card' : 'border-border'}`}>
            <span className="flex items-center gap-1 font-semibold">{active && <Check className="size-4" aria-hidden="true" />}{renderer.id}</span>
            {unavailable && <span id={`gl-r-${renderer.id}`} className="muted block text-xs">{t('rendererUnavailable')}</span>}
          </button></li>;
        })}</ul>
      </section>
      <section aria-labelledby="gl-step2" className="space-y-2">
        <h3 id="gl-step2" className="font-semibold">2. {t('step2')}</h3>
        {draft.renderer === '' && <p className="muted">{t('sourceNone')}</p>}
        {draft.renderer !== '' && provider && <><p className="muted">{t('sourceBacked')}</p><SourcePicker id="gl-source" provider={provider} value={draft.sourceIds[0] ?? null} onChange={(id) => set({ sourceIds: id ? [id] : [] })} /></>}
        {draft.renderer !== '' && !provider && multi && <><p className="muted">{t('sourceMulti')}</p><SourceMultiPicker id="gl-sources" value={draft.sourceIds} onChange={(ids) => set({ sourceIds: ids })} /></>}
        {draft.renderer !== '' && !provider && !multi && <p className="muted">{t('sourceNone')}</p>}
      </section>
      <section aria-labelledby="gl-step3" className="space-y-2">
        <h3 id="gl-step3" className="font-semibold">3. {t('step3')}</h3>
        <label className="field"><span className="label">{t('keywords')}</span><Input value={draft.keywords} onChange={(event) => set({ keywords: event.target.value })} /><span className="muted text-xs">{t('keywordsHelp')}</span></label>
        <label className="field"><span className="label">{t('excludeKeywords')}</span><Input value={draft.excludeKeywords} onChange={(event) => set({ excludeKeywords: event.target.value })} /></label>
      </section>
      <section aria-labelledby="gl-step4" className="space-y-2">
        <h3 id="gl-step4" className="font-semibold">4. {t('step4')}</h3>
        <label className="field"><span className="label">{t('limit')}</span><Input type="number" min={1} max={100} value={draft.limit} aria-invalid={!limitValid} onChange={(event) => set({ limit: event.target.value })} /><span className={limitValid ? 'muted text-xs' : 'error text-xs'}>{t('limitHelp')}</span></label>
        {def && <p className="muted">{t('rulesCount', { count: def.highlight_rules?.length ?? 0 })} <button type="button" className="underline" onClick={() => onEditRules(def.id)}>{t('editRules')}</button></p>}
      </section>
      <section aria-labelledby="gl-preview" className="space-y-2 rounded-lg border border-border p-3">
        <h3 id="gl-preview" className="font-semibold">{t('preview')}</h3>
        {!def && <p className="muted">{t('previewNew')}</p>}
        {def && preview.isPending && <p className="muted" role="status">{t('previewLoading')}</p>}
        {def && preview.isError && <p className="error" role="alert"><AlertCircle className="inline size-4" aria-hidden="true" /> {t('previewError')}</p>}
        {def && preview.isSuccess && preview.data.length === 0 && <p className="muted">{t('previewEmpty')}</p>}
        {def && preview.isSuccess && <ul className="space-y-1">{preview.data.map((match) => <li key={`${match.rule_id}:${match.document_version_id}`}><span className="font-medium">{match.title}</span> <span className="muted text-xs">{t('previewMatch', { keywords: match.matched_keywords.join(', ') })}</span></li>)}</ul>}
        {def && <p className="muted text-xs">{t('previewNote')}</p>}
      </section>
    </fieldset>
    {save.error && <p className="error" role="alert">{t(apiFailureKey(save.error) ?? 'saveFailed')}</p>}
    {remove.error && <p className="error" role="alert">{t(remove.error instanceof ApiError && remove.error.code === 'definition_in_use' ? 'inUse' : apiFailureKey(remove.error) === 'conflict' ? 'conflict' : 'deleteFailed')}</p>}
    <div className="form-actions sticky bottom-0 z-10 flex-wrap items-center gap-2 border-t border-border bg-background py-3">
      <p className="muted me-auto text-xs" role="status">{dirty ? t('unsaved') : def ? t('saveRevision', { revision: def.revision + 1 }) : t('saveNew')}</p>
      {def && <Button type="button" variant="destructive" disabled={save.isPending || remove.isPending} onClick={() => setConfirmDelete(true)}>{t('delete')}</Button>}
      <Button type="button" className="secondary" disabled={save.isPending} onClick={() => { setDraft(makeDraft(def)); setTouched(false); if (!def) onDone(null); }}>{t('cancel')}</Button>
      <Button type="submit" disabled={save.isPending || (Boolean(def) && !dirty)}>{save.isPending ? t('saving') : t('save')}</Button>
    </div>
    <AlertDialog open={confirmDelete} onOpenChange={setConfirmDelete}>
      <AlertDialogContent><AlertDialogHeader><AlertDialogTitle>{t('deleteTitle')}</AlertDialogTitle><AlertDialogDescription>{t('deleteBody')}</AlertDialogDescription></AlertDialogHeader>
        <AlertDialogFooter><AlertDialogCancel>{t('cancel')}</AlertDialogCancel><AlertDialogAction onClick={() => remove.mutate()}>{t('deleteConfirm')}</AlertDialogAction></AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </form>;
}
