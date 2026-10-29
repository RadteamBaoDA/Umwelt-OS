'use client';

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useRef, useState, type ReactNode } from 'react';
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { archiveSource, createManualSource, sourceKeys, type Source } from './api';
import { ConnectorEditor, type EditorActions, type EditorSnapshot, type EditorStep } from './connector-editor';
import { GitHubWebhookStatus } from './github-summary';
import { SettingsSection } from '@/modules/settings/settings-layout';
import { SourceAiToggles, type SourceAiOptions } from './source-ai-toggles';
import { Upload } from '@/modules/ingestion/upload';

/** One wizard step: stable id, display title, and whether the owner may leave it forward in add mode. */
export type WizardStepDef = { id: string; title: string; valid: boolean };

/**
 * Full-screen step dialog shared by every add/edit flow.
 * Add mode only moves forward through Next (gated by the current step's `valid`) and may step Back;
 * edit mode lets the owner jump to any step. Closing while `discard.needed` asks for confirmation and
 * then runs `discard.onConfirm` (which owns archiving the half-created source and closing).
 *
 * @param onNext Optional async hook run before advancing; resolve false to stay on the step.
 * @param final Action shown instead of Next on the last step.
 */
export function WizardShell({ title, mode, steps, stepIndex, onStepChange, busy, onNext, final, onRequestClose, discard, children }: {
  title: string;
  mode: 'add' | 'edit';
  steps: WizardStepDef[];
  stepIndex: number;
  onStepChange: (index: number) => void;
  busy: boolean;
  onNext?: () => Promise<boolean>;
  final: { label: string; disabled?: boolean; onClick: () => void };
  onRequestClose: () => void;
  discard?: { needed: boolean; onConfirm: () => Promise<void> };
  children: ReactNode;
}) {
  const t = useTranslations('sources');
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [discarding, setDiscarding] = useState(false);
  const [discardFailed, setDiscardFailed] = useState(false);
  const [advancing, setAdvancing] = useState(false);
  const current = steps[stepIndex];
  const last = stepIndex === steps.length - 1;
  const locked = busy || advancing;

  /** Advances one step after the optional pre-advance hook allows it; the guard prevents double submission. */
  async function next() {
    if (locked || !current) return;
    setAdvancing(true);
    try {
      const allowed = onNext ? await onNext() : true;
      if (allowed) onStepChange(Math.min(stepIndex + 1, steps.length - 1));
    } finally {
      setAdvancing(false);
    }
  }

  /** Routes every close gesture (X, Escape, overlay) through the discard confirmation when a created source is unverified. */
  function requestClose() {
    if (discard?.needed) { setDiscardFailed(false); setConfirmOpen(true); return; }
    onRequestClose();
  }

  /** Runs the owner-confirmed discard; a failure keeps the dialog open so the orphan is not silently left behind. */
  async function confirmDiscard() {
    if (!discard) return;
    setDiscarding(true);
    setDiscardFailed(false);
    try {
      await discard.onConfirm();
      setConfirmOpen(false);
    } catch {
      setDiscardFailed(true);
    } finally {
      setDiscarding(false);
    }
  }

  return <>
    <Dialog open onOpenChange={(open) => { if (!open) requestClose(); }}>
      <DialogContent size="full" closeLabel={t('close')} className="grid-rows-[auto_1fr_auto] content-start">
        <DialogHeader className="text-left">
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{current?.title}</DialogDescription>
        </DialogHeader>
        <div className="grid min-h-0 grid-cols-1 auto-rows-max content-start gap-4 overflow-y-auto min-[900px]:grid-cols-[220px_1fr] min-[900px]:items-start">
          <ol aria-label={t('wizardStepsLabel')} className="m-0 grid list-none auto-cols-max grid-flow-col gap-2 p-0 pb-1 max-[899px]:overflow-x-auto min-[900px]:sticky min-[900px]:top-0 min-[900px]:grid-flow-row min-[900px]:auto-cols-auto">
            {steps.map((step, index) => {
              // Add mode never lets the owner skip ahead of validated steps; edit mode is free navigation.
              const reachable = mode === 'edit' || index <= stepIndex;
              return <li key={step.id}>
                <button type="button" disabled={!reachable || locked} aria-current={index === stepIndex ? 'step' : undefined} onClick={() => onStepChange(index)}
                  className={`flex min-h-11 w-full items-center gap-3 rounded-lg border p-3 text-left disabled:cursor-not-allowed disabled:opacity-60 ${index === stepIndex ? 'border-primary bg-card' : 'border-border bg-transparent'}`}>
                  <span className={`flex size-6 shrink-0 items-center justify-center rounded-full text-xs ${index === stepIndex ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'}`}>{index + 1}</span>
                  <strong className="whitespace-nowrap text-sm">{step.title}</strong>
                </button>
              </li>;
            })}
          </ol>
          <div className="flex min-w-0 flex-col gap-4">{children}</div>
        </div>
        <DialogFooter className="sticky bottom-0 border-t border-border bg-background pt-3">
          <Button type="button" variant="outline" disabled={locked || stepIndex === 0} onClick={() => onStepChange(stepIndex - 1)}>{t('back')}</Button>
          {last
            ? <Button type="button" disabled={locked || final.disabled} onClick={final.onClick}>{final.label}</Button>
            : <Button type="button" disabled={locked || (mode === 'add' && !current?.valid)} onClick={() => void next()}>{t('next')}</Button>}
        </DialogFooter>
      </DialogContent>
    </Dialog>
    <AlertDialog open={confirmOpen} onOpenChange={(open) => { if (!discarding) setConfirmOpen(open); }}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{t('discardNewSourceTitle')}</AlertDialogTitle>
          <AlertDialogDescription>{t('discardNewSourceBody')}</AlertDialogDescription>
        </AlertDialogHeader>
        {discardFailed && <p className="text-destructive" role="alert">{t('actionFailed')}</p>}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={discarding}>{t('discardNewSourceKeep')}</AlertDialogCancel>
          <AlertDialogAction disabled={discarding} onClick={(event) => { event.preventDefault(); void confirmDiscard(); }}>{t('discardNewSourceConfirm')}</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </>;
}

type ConnectorWizardProps = {
  source: Source | null;
  onClose: () => void;
  onChanged: () => void;
  registerTransitionGuard: (guard: (() => boolean) | null) => void;
};

/**
 * Step wizard over the shared connector editor.
 * Add: provider and name (creates the source) -> connect -> choose data -> schedule -> save and test fetch.
 * Edit (`source` set): the same steps without the provider step, freely navigable.
 * MCP collapses connect/choose/schedule into a single configure step. A source created here and not yet
 * verified is archived if the owner discards it; verified or edited sources are never archived by closing.
 */
function ConnectorWizard({ source, onClose, onChanged, registerTransitionGuard }: ConnectorWizardProps) {
  const t = useTranslations('sources');
  const { csrfToken } = useWorkspaceSession();
  const queryClient = useQueryClient();
  const edit = source !== null;
  const actionsRef = useRef<EditorActions | null>(null);
  const [snapshot, setSnapshot] = useState<EditorSnapshot>({ sourceId: source?.id ?? '', provider: source?.provider ?? '', busy: false, providerOk: false, connectOk: true, chooseOk: true, verify: { phase: 'idle' } });
  const [index, setIndex] = useState(0);
  const mcp = snapshot.provider === 'mcp';
  const google = ['google_mail', 'google_calendar', 'google_drive'].includes(snapshot.provider);
  const ids: EditorStep[] = mcp ? ['connect', 'verify'] : google ? ['connect', 'choose', 'collect', 'verify'] : ['connect', 'choose', 'collect', 'verify'];
  if (!edit) ids.unshift('provider');
  const titles: Record<EditorStep, string> = { provider: t('stepProviderName'), connect: mcp ? t('stepMcpConfigure') : t('wizConnect'), choose: t('wizChooseData'), collect: t('stepSchedule'), verify: t('stepVerify') };
  const valid: Record<EditorStep, boolean> = { provider: snapshot.providerOk || Boolean(snapshot.sourceId), connect: snapshot.connectOk, choose: snapshot.chooseOk, collect: true, verify: true };
  const steps: WizardStepDef[] = ids.map((id) => ({ id, title: titles[id], valid: valid[id] }));
  const step = ids[Math.min(index, ids.length - 1)];
  const verified = snapshot.verify.phase === 'succeeded' || snapshot.verify.phase === 'no_changes';
  const createdId = !edit ? snapshot.sourceId : '';

  /** Creates the source on leaving the provider step and runs per-step preparation (GitHub saves its draft before OAuth). */
  async function onNext(): Promise<boolean> {
    const actions = actionsRef.current;
    if (!actions) return false;
    if (step === 'provider' && !snapshot.sourceId) return (await actions.createSource()) !== null;
    return actions.prepareNext(step);
  }

  /** Archives the unverified source this wizard created, refreshes the table, and closes without the dirty-draft prompt (the owner already confirmed). */
  async function discard() {
    await archiveSource(createdId, csrfToken);
    await queryClient.invalidateQueries({ queryKey: sourceKeys.all });
    registerTransitionGuard(null);
    onChanged();
    onClose();
  }

  return <WizardShell title={edit ? t('wizardTitleEdit') : t('wizardTitleConnector')} mode={edit ? 'edit' : 'add'} steps={steps} stepIndex={Math.min(index, ids.length - 1)}
    onStepChange={setIndex} busy={snapshot.busy} onNext={onNext} onRequestClose={onClose}
    final={{ label: t('done'), disabled: !edit && !verified, onClick: onClose }}
    discard={createdId ? { needed: !verified, onConfirm: discard } : undefined}>
    {snapshot.provider === 'github' && <GitHubWebhookStatus />}
    <ConnectorEditor source={source} step={step} actionsRef={actionsRef} onSnapshot={setSnapshot} registerTransitionGuard={registerTransitionGuard} onChanged={onChanged} onClose={onClose} />
  </WizardShell>;
}

/** One-step wizard for manual sources: a name only, since manual sources never fetch data. */
function ManualWizard({ onClose, onChanged, purpose = null }: {
  onClose: () => void;
  onChanged: () => void;
  purpose?: 'notes' | 'personal_finance' | null;
}) {
  const t = useTranslations('sources');
  const { csrfToken } = useWorkspaceSession();
  const queryClient = useQueryClient();
  const [name, setName] = useState(() => purpose === 'notes' ? t('notesSourceName') : purpose === 'personal_finance' ? t('financeSourceName') : '');
  const [createdSourceId, setCreatedSourceId] = useState('');
  const [aiOptions, setAiOptions] = useState<SourceAiOptions>({ graph_enabled: true, rag_index_enabled: true });
  const trimmed = name.trim();
  const create = useMutation({
    mutationFn: () => createManualSource(trimmed, csrfToken, aiOptions),
    onSuccess: async (source) => {
      await queryClient.invalidateQueries({ queryKey: sourceKeys.all });
      if (purpose) { setCreatedSourceId(source.id); onChanged(); }
      else { onChanged(); onClose(); }
    },
  });
  /** Creates the local-only manual source before showing the existing durable upload flow. */
  async function createBeforeImport(): Promise<boolean> {
    if (!purpose || !trimmed || create.isPending) return false;
    try { await create.mutateAsync(); return true; } catch { return false; }
  }
  const steps: WizardStepDef[] = purpose
    ? [{ id: 'name', title: t('stepManualName'), valid: Boolean(trimmed) }, { id: 'import', title: t('stepManualImport'), valid: true }]
    : [{ id: 'name', title: t('stepManualName'), valid: Boolean(trimmed) }];
  const stepIndex = purpose && createdSourceId ? 1 : 0;
  const description = purpose === 'notes' ? t('manualNotesHint') : purpose === 'personal_finance' ? t('manualFinanceHint') : t('manualNoFetch');
  return <WizardShell title={purpose ? t(purpose === 'notes' ? 'wizardTitleNotes' : 'wizardTitleFinance') : t('wizardTitleManual')}
    mode="add" steps={steps} stepIndex={stepIndex} onStepChange={() => undefined} busy={create.isPending}
    onNext={purpose && !createdSourceId ? createBeforeImport : undefined} onRequestClose={onClose}
    final={{ label: purpose ? t('done') : create.isPending ? t('saving') : t('saveSource'), disabled: purpose ? !createdSourceId : !trimmed, onClick: () => { if (!purpose && trimmed) create.mutate(); else onClose(); } }}>
    {stepIndex === 0 ? <form className="flex flex-col gap-4" onSubmit={(event) => { event.preventDefault(); if (purpose) void createBeforeImport(); else if (trimmed && !create.isPending) create.mutate(); }}>
      <SettingsSection title={t('panelIdentity')} description={description}>
        <div className="field"><Label htmlFor="source-name">{t('sourceName')}</Label><Input id="source-name" autoFocus maxLength={200} value={name} onChange={(event) => setName(event.target.value)} /></div>
      </SettingsSection>
      <SettingsSection title={t('aiOptionsTitle')}>
        <SourceAiToggles idPrefix="manual-source" value={aiOptions} onChange={setAiOptions} disabled={create.isPending} />
      </SettingsSection>
      {create.error && <p className="error" role="alert">{t('actionFailed')}</p>}
    </form> : createdSourceId && purpose ? <Upload initialMode={purpose === 'notes' ? 'notes' : 'finance'} initialSourceId={createdSourceId} /> : null}
  </WizardShell>;
}

/** Which flow the dialog runs: a new connector, a new manual source, or editing an existing connector. */
export type SourceWizardRequest = { kind: 'connector' } | { kind: 'manual' } | { kind: 'manual_import'; purpose: 'notes' | 'personal_finance' } | { kind: 'edit'; source: Source };

/** Opens the full-screen add/edit dialog for the requested connector or manual flow. */
export function SourceWizard({ request, onClose, onChanged, registerTransitionGuard }: {
  request: SourceWizardRequest;
  onClose: () => void;
  onChanged: () => void;
  registerTransitionGuard: (guard: (() => boolean) | null) => void;
}) {
  if (request.kind === 'manual') return <ManualWizard onClose={onClose} onChanged={onChanged} />;
  if (request.kind === 'manual_import') return <ManualWizard onClose={onClose} onChanged={onChanged} purpose={request.purpose} />;
  return <ConnectorWizard source={request.kind === 'edit' ? request.source : null} onClose={onClose} onChanged={onChanged} registerTransitionGuard={registerTransitionGuard} />;
}
