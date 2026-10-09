'use client';

import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { apiRequest, workspaceHeaders, workspaceTargetFor } from '@/core/api';

type BackupControl = {
  phase: 'idle' | 'draining' | 'quiesced' | 'snapshotting' | 'resuming' | 'failed_recovery_required';
  active_activities: number;
  uncertain_activities: number;
  unresolved_owner_effects: Array<{ owner: string; reason: string; count: number }>;
};

/** Render owner export downloads and the credential-free durable backup coordination status. */
export function BackupExportSettings() {
  const t = useTranslations('exports');
  const [downloading, setDownloading] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState(false);
  const control = useQuery({
    queryKey: ['backup', 'control'],
    queryFn: () => apiRequest<BackupControl>('/api/v1/backups/control'),
    staleTime: 15_000,
  });
  const phaseLabels = {
    idle: 'phaseIdle',
    draining: 'phaseDraining',
    quiesced: 'phaseQuiesced',
    snapshotting: 'phaseSnapshotting',
    resuming: 'phaseResuming',
    failed_recovery_required: 'phaseFailedRecoveryRequired',
  } as const;

  /** Fetch one bounded export, report authentication loss, and save it through the browser download flow. */
  async function download(format: 'json' | 'markdown' | 'csv') {
    setDownloading(format);
    setDownloadError(false);
    try {
      const response = await fetch(`/api/v1/exports/${format}`, {
        cache: 'no-store', credentials: 'same-origin', headers: workspaceHeaders(workspaceTargetFor(`/api/v1/exports/${format}`)),
      });
      if (!response.ok) {
        if (response.status === 401) window.dispatchEvent(new Event('bbd:unauthorized'));
        throw new Error('Export failed');
      }
      const objectUrl = URL.createObjectURL(await response.blob());
      const anchor = document.createElement('a');
      anchor.href = objectUrl;
      anchor.download = `umwelt-os-export.${format}`;
      anchor.click();
      URL.revokeObjectURL(objectUrl);
    } catch {
      setDownloadError(true);
    } finally {
      setDownloading(null);
    }
  }

  return <section className="mt-8 space-y-6 border-t border-border pt-6" aria-labelledby="backup-export-title">
    <div>
      <h2 id="backup-export-title" className="text-lg font-semibold">{t('title')}</h2>
      <p className="muted mt-1 text-sm">{t('description')}</p>
    </div>
    <div className="space-y-3">
      <h3 className="font-semibold">{t('portableTitle')}</h3>
      <p className="muted text-sm">{t('portableDescription')}</p>
      <div className="flex flex-wrap gap-3">
        {(['json', 'markdown', 'csv'] as const).map((format) => <Button
          key={format}
          type="button"
          variant="outline"
          disabled={downloading !== null}
          onClick={() => void download(format)}
        >{downloading === format ? t('downloading') : t(format)}</Button>)}
      </div>
      {downloadError && <p className="text-sm text-destructive" role="alert">{t('downloadFailed')}</p>}
    </div>
    <div className="space-y-2 border-t border-border pt-5">
      <h3 className="font-semibold">{t('backupStatus')}</h3>
      <p className="muted text-sm">{t('backupDescription')}</p>
      {control.isPending ? <p role="status" className="muted text-sm">{t('downloading')}</p> : control.isError ? <div>
        <p role="alert" className="text-sm text-destructive">{t('statusUnavailable')}</p>
        <Button className="mt-2" type="button" variant="outline" onClick={() => void control.refetch()}>{t('retry')}</Button>
      </div> : <dl className="grid gap-2 text-sm sm:grid-cols-3">
        <div><dt className="muted">{t('phase')}</dt><dd>{t(phaseLabels[control.data.phase])}</dd></div>
        <div><dt className="muted">{t('activeActivities')}</dt><dd>{control.data.active_activities}</dd></div>
        <div><dt className="muted">{t('uncertainActivities')}</dt><dd>{control.data.uncertain_activities}</dd></div>
        {control.data.unresolved_owner_effects.length > 0 && <div className="sm:col-span-3">
          <dt className="muted">{t('unresolvedOwnerEffects')}</dt>
          <dd>
            <p className="muted mb-1">{t('ownerEffectDescription')}</p>
            <ul className="list-inside list-disc">
              {control.data.unresolved_owner_effects.map((effect) => <li key={`${effect.owner}:${effect.reason}`}>
                {effect.owner}: {effect.reason} ({effect.count})
              </li>)}
            </ul>
          </dd>
        </div>}
      </dl>}
    </div>
  </section>;
}
