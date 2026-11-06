'use client';

import { useTranslations } from 'next-intl';
import { useConnectorEditor, type ConnectorEditorProps } from './use-connector-editor';
import { ProviderStep } from './connector-editor/provider-step';
import { SourceEditorBody } from './connector-editor/editor-body';

export { providerCredentialRequired } from './connector-editor-model';
export type { EditorStep, VerifyState, EditorSnapshot, EditorActions } from './connector-editor-model';

/** Edit and activate connectors through owner APIs; local FHIR/Home Assistant scopes use encrypted bearer tokens, while GitHub uncertainty and retained history stay explicit. */
export function ConnectorEditor(props: ConnectorEditorProps) {
  const m = useConnectorEditor(props);
  const t = useTranslations('sources');
  const { sourceId, embedded } = m;
  return (
    <section
      className={
        embedded ? 'source-editor flex flex-col gap-4' : 'source-editor flex flex-col gap-4 rounded-lg border border-border bg-card p-5'
      }
      aria-label={sourceId ? t('editTitle') : t('setupTitle')}
    >
      <ProviderStep m={m} />
      <SourceEditorBody m={m} />
    </section>
  );
}
