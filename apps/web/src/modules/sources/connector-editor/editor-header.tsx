'use client';

import type { ConnectorEditorModel } from '../connector-editor-types';
import { useTranslations } from 'next-intl';
import { useEffect } from 'react';
import { Button } from '@/components/ui/button';
import { DisconnectDialog } from '../disconnect-dialog';
import { RenameSource } from '../source-rename';
import { useSourceActions } from '../use-source-actions';
import { ConnectorActivation, Source } from '../api';

/** Pause and Disconnect controls for the editor header, sharing handlers and the confirmation dialog with the source list rows. */
export function EditorSourceActions({
  source,
  activation,
  disabled,
  onChanged,
  onPurgeStarted,
  onClose,
  onBusyChange,
}: {
  source: Source;
  activation?: ConnectorActivation;
  disabled: boolean;
  onChanged: () => void;
  onPurgeStarted?: (operationId: string) => void;
  onClose: () => void;
  onBusyChange: (busy: boolean) => void;
}) {
  const t = useTranslations('sources');
  const actions = useSourceActions({
    source,
    activation,
    onChanged: () => {
      onChanged();
      onClose();
    },
    onPurgeStarted,
  });
  const { busy: actionsBusy } = actions;
  useEffect(() => {
    onBusyChange(actionsBusy);
    return () => onBusyChange(false);
  }, [actionsBusy, onBusyChange]);
  const off = disabled || actions.busy;
  return (
    <>
      {source.status === 'active' && (
        <Button variant="outline" disabled={off} onClick={() => void actions.toggleStatus()}>
          {actions.busy ? t('pausing') : t('pause')}
        </Button>
      )}
      <DisconnectDialog
        sourceId={source.id}
        name={source.name}
        archived={source.status === 'archived'}
        disabled={off}
        onConfirm={(deleteData) => void actions.disconnect(deleteData)}
        trigger={
          <Button variant="outline" aria-disabled={off}>
            {actions.busy ? t('disconnecting') : t(source.status === 'archived' ? 'disconnectDelete' : 'disconnectAction')}
          </Button>
        }
      />
      {actions.error && (
        <p className="error" role="alert">
          {t('actionFailed')}
        </p>
      )}
    </>
  );
}

/** Source name row shown at the top of the connect step when the editor is embedded in the wizard. */
export function EmbeddedNameRow({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { embedded, showStep, source, onChanged } = m;
  return (
    <>
      {source && embedded && (
        <div hidden={!showStep('connect')} className="flex items-center gap-2">
          <span className="text-sm font-medium">{t('sourceName')}</span>
          <RenameSource source={source} onRenamed={onChanged} />
        </div>
      )}
    </>
  );
}

/** Title with the status badge, revision and unsaved marker. */
export function HeaderTitle({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { name, secret, revision, dirty, sourceStatus, activationLabel, source } = m;
  return (
    <div>
      <h2>{name || source?.name || t('editTitle')}</h2>
      <p className="text-muted-foreground">
        <span className="badge">{sourceStatus === 'paused' ? t('statusPaused') : activationLabel}</span>{' '}
        {revision > 0 ? t('editorRevision', { revision }) : t('editorRevisionNew')}
        {dirty || secret ? ` · ${t('editorUnsaved')}` : ''}
      </p>
    </div>
  );
}

/** Collect now (active, clean) and Resume (paused, non-MCP) buttons. */
export function HeaderPrimaryActions({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { provider, dirty, busyAction, sourceStatus, visibleActivationState, resumeSource, collectNow, locked } = m;
  return (
    <>
      {sourceStatus === 'active' && visibleActivationState === 'active' && !dirty && (
        <Button variant="outline" disabled={locked} onClick={collectNow}>
          {busyAction === 'collect' ? t('collecting') : t('collectNow')}
        </Button>
      )}
      {sourceStatus === 'paused' && provider !== 'mcp' && (
        <Button variant="outline" disabled={locked} onClick={resumeSource}>
          {busyAction === 'resume' ? t('resuming') : t('resume')}
        </Button>
      )}
    </>
  );
}

/** Existing-source header: title, status line and Collect now / Resume / Pause / Disconnect / Close actions. */
export function EditorHeader({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const {
    secret,
    dirty,
    setActionsBusy,
    activationQuery,
    sourceStatus,
    closeEditor,
    locked,
    embedded,
    source,
    onClose,
    onChanged,
    onPurgeStarted,
  } = m;
  return (
    <>
      <EmbeddedNameRow m={m} />
      {!embedded && (
        <header className="section-heading">
          <HeaderTitle m={m} />
          <div className="form-actions">
            <HeaderPrimaryActions m={m} />
            {source && (
              <EditorSourceActions
                source={{ ...source, status: sourceStatus }}
                activation={activationQuery.data}
                disabled={locked || dirty || Boolean(secret)}
                onChanged={onChanged}
                onPurgeStarted={onPurgeStarted}
                onClose={onClose}
                onBusyChange={setActionsBusy}
              />
            )}
            <Button variant="outline" onClick={closeEditor}>
              {t('close')}
            </Button>
          </div>
        </header>
      )}
    </>
  );
}
