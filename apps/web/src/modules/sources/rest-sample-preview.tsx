'use client';

import { useTranslations } from 'next-intl';
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { ApiError } from '@/core/api';
import type { ConnectorEditorModel } from './connector-editor-types';
import { restSample, type RestSample } from './rest-sample';
import { RestSampleTree, restSampleErrorMessage } from './rest-sample-tree';

/**
 * "Test connection" control for the REST editor: fetches a sample with the current draft (auth none only) and shows
 * the clickable JSON tree. Needs a saved source id; nothing is persisted.
 */
export function RestSamplePreview({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<RestSample | null>(null);
  const [error, setError] = useState<string | null>(null);
  const supported = m.authMethod === 'none' && Boolean(m.sourceId);

  /** Sends the draft to the preview endpoint; errors map to bilingual messages. */
  async function run() {
    if (!m.sourceId) return;
    setBusy(true);
    setError(null);
    try {
      setResult(await restSample(m.sourceId, { expected_revision: m.revision, expected_source_generation: m.sourceGenerationRef.current, configuration: { ...m.configuration }, auth_method: 'none' }));
    } catch (e) {
      setResult(null);
      setError(restSampleErrorMessage(t, e instanceof ApiError ? { code: e.code, message: e.message } : {}));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-2">
      <Button type="button" variant="outline" disabled={!supported || busy} onClick={() => void run()}>
        {busy ? t('restTesting') : t('restTest')}
      </Button>
      {m.authMethod !== 'none' && <p className="m-0 text-sm text-muted">{t('restHeaderAuth')}</p>}
      {error && (
        <p className="m-0 text-sm text-danger" role="alert">
          {error}
        </p>
      )}
      {result && <RestSampleTree result={result} itemsPath={m.configuration.items_path ?? ''} onFill={(k, v) => m.changeConfiguration(k, v)} />}
    </div>
  );
}
