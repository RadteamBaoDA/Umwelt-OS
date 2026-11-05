'use client';

import { CheckboxField, FieldGrid, SettingsActions, SettingsPageHeader } from '@/components/settings-layout';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { ApiError } from '@/core/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { useWorkspace } from '@/core/workspace-context';
import { fetchTranslationSettings, saveTranslationSettings } from '@/modules/translations';
import type { TranslationLanguage } from '@/modules/translations';

/** Workspace translation toggle and target language. Owners save with expected_revision; members see it read only. */
// eslint-disable-next-line complexity -- JSX presentational markup: the score counts every conditional-render branch (&&, ?:, ??) of this panel; it holds no branching business logic, so splitting would only relocate the same render conditions
export function TranslationSettings() {
  const t = useTranslations('translations');
  const { csrfToken } = useWorkspaceSession();
  const { isOwner, selection } = useWorkspace();
  const tShell = useTranslations('shell');
  const client = useQueryClient();
  const key = ['translation-settings', selection?.id ?? null];
  const query = useQuery({ queryKey: key, queryFn: ({ signal }) => fetchTranslationSettings(signal) });
  const [draft, setDraft] = useState<{ enabled?: boolean; language?: TranslationLanguage }>({});
  const enabled = draft.enabled ?? query.data?.enabled ?? false;
  const language = draft.language ?? query.data?.target_language ?? 'vi';
  const setEnabled = (value: boolean) => setDraft((d) => ({ ...d, enabled: value }));
  const setLanguage = (value: TranslationLanguage) => setDraft((d) => ({ ...d, language: value }));
  const save = useMutation({
    mutationFn: () => saveTranslationSettings(
      { enabled, target_language: language, expected_revision: query.data?.configuration_revision ?? 1 }, csrfToken),
    onSuccess: (value) => { client.setQueryData(key, value); setDraft({}); },
    onError: () => { setDraft({}); void query.refetch(); },
  });
  const dirty = Boolean(query.data && (query.data.enabled !== enabled || query.data.target_language !== language));
  const readOnly = !isOwner;
  const failure = save.error instanceof ApiError && save.error.status === 409 ? t('stale') : save.error ? t('saveFailed') : null;
  return (
    <div className="flex flex-col gap-5">
      <SettingsPageHeader eyebrow={tShell('settings')} title={t('settingsTitle')} description={t('settingsHelp')} />
      <section className="flex min-w-0 flex-col gap-4 border-t border-border pt-6">
        {query.isError ? <p role="alert" className="error m-0">{t('loadFailed')}</p> : null}
        <CheckboxField>
          <Checkbox id="translation-enable" checked={enabled} disabled={readOnly || !query.data} onCheckedChange={(checked) => setEnabled(checked === true)} />
          <label htmlFor="translation-enable">{t('enable')}</label>
        </CheckboxField>
        <FieldGrid>
          <label className="field">
            <span className="label">{t('targetLanguage')}</span>
            <Select value={language} disabled={readOnly || !query.data} onValueChange={(value) => setLanguage(value as TranslationLanguage)}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="vi">{t('languageVi')}</SelectItem>
                <SelectItem value="en">{t('languageEn')}</SelectItem>
              </SelectContent>
            </Select>
          </label>
        </FieldGrid>
        {failure ? <p role="alert" className="error m-0">{failure}</p> : null}
        {readOnly ? <p className="text-muted-foreground m-0 text-xs">{t('ownerOnly')}</p> : (
          <SettingsActions>
            {save.isSuccess && !dirty ? <p role="status" className="text-muted-foreground me-auto self-center text-xs">{t('saved')}</p> : null}
            <Button type="button" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>{t('save')}</Button>
          </SettingsActions>
        )}
      </section>
    </div>
  );
}
