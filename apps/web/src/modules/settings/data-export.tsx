'use client';

import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { workspaceHeaders, workspaceTargetFor } from '@/core/api';

/** Render the owner portable data export downloads. */
export function DataExportSettings() {
  const t = useTranslations('exports');
  const [downloading, setDownloading] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState(false);
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

  return <section className="mt-8 space-y-6 border-t border-border pt-6" aria-labelledby="data-export-title">
    <div>
      <h2 id="data-export-title" className="text-lg font-semibold">{t('title')}</h2>
      <p className="text-muted-foreground mt-1 text-sm">{t('description')}</p>
    </div>
    <div className="space-y-3">
      <h3 className="font-semibold">{t('portableTitle')}</h3>
      <p className="text-muted-foreground text-sm">{t('portableDescription')}</p>
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
  </section>;
}
