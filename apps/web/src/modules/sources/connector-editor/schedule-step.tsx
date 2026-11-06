'use client';

import type { ConnectorEditorModel } from '../connector-editor-types';
import { CheckboxField, FieldGrid, SettingsSection } from '@/components/settings-layout';
import { useTranslations } from 'next-intl';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { BuiltinOptions } from '../builtin-options';
import { ScopeHint } from '../provider-scope';
import { ExecutionBackend, ConnectorConfig, allowedIntervals, minIntervalMinutes, RealtimeMode } from '../api';
import { intervals, isNativeEntry } from '../connector-editor-model';
import { ConnectStep, ChooseStep } from './connect-choose-steps';

/** Schedule step: interval, timezone, realtime mode, built-in options and history mode. */
export function ScheduleStep({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { provider, currentEntry, configuration, builtinEntry, changeConfiguration, locked, showStep } = m;
  return (
    <>
      <div id="source-step-collect" hidden={!showStep('collect')} className="scroll-mt-4">
        <SettingsSection title={t('panelSchedule')} description={t('panelScheduleHint')}>
          <FieldGrid>
            <div className="field">
              <Label htmlFor="source-schedule">{t('schedule')}</Label>
              <Select
                value={String(configuration.schedule_interval_minutes)}
                onValueChange={(value) =>
                  changeConfiguration('schedule_interval_minutes', Number(value) as ConnectorConfig['schedule_interval_minutes'])
                }
              >
                <SelectTrigger id="source-schedule">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(provider === 'alpha_vantage' ? [1440] : allowedIntervals(intervals, currentEntry)).map((minutes) => (
                    <SelectItem key={minutes} value={String(minutes)}>
                      {t('everyMinutes', { minutes })}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {minIntervalMinutes(currentEntry) > 15 && (
                <small className="text-muted-foreground">
                  {minIntervalMinutes(currentEntry) >= 60
                    ? t('minIntervalHours', { hours: minIntervalMinutes(currentEntry) / 60 })
                    : t('minIntervalMinutes', { minutes: minIntervalMinutes(currentEntry) })}
                </small>
              )}
            </div>
            <div className="field">
              <Label htmlFor="source-timezone">{t('timezone')}</Label>
              <Input
                id="source-timezone"
                aria-describedby="source-hint-timezone"
                value={configuration.timezone}
                onChange={(event) => changeConfiguration('timezone', event.target.value)}
              />
              <ScopeHint field="timezone" />
            </div>
            <div className="field">
              <Label htmlFor="source-realtime-mode">{t('trackingModeLabel')}</Label>
              <Select
                value={configuration.realtime_mode ?? currentEntry?.realtime_tier ?? 'near_realtime'}
                onValueChange={(value) => changeConfiguration('realtime_mode', value as RealtimeMode)}
              >
                <SelectTrigger id="source-realtime-mode">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="realtime">{t('trackingModeRealtime')}</SelectItem>
                  <SelectItem value="near_realtime">{t('trackingModeNearRealtime')}</SelectItem>
                </SelectContent>
              </Select>
              <small className="text-muted-foreground">{t('trackingModeHelp')}</small>
            </div>
          </FieldGrid>
          {builtinEntry && (
            <BuiltinOptions
              builtin={builtinEntry}
              disabled={locked}
              values={configuration}
              // An emptied field is saved as an explicit null (clears the stored value); omitted keys keep it.
              onChange={(key, value) =>
                changeConfiguration(
                  key as keyof ConnectorConfig,
                  ((Array.isArray(value) && value.length === 0) || value === undefined ? null : value) as never,
                )
              }
            />
          )}
          {isNativeEntry(currentEntry) && (
            <>
              <div className="field">
                <Label htmlFor="source-history-mode">{t('historyMode')}</Label>
                <Input
                  id="source-history-mode"
                  readOnly
                  aria-readonly="true"
                  value={t(provider === 'telegram' ? 'pendingUpdatesOnly' : 'returnedSnapshot')}
                />
              </div>
            </>
          )}
        </SettingsSection>
      </div>
    </>
  );
}

/** Advanced limits (pages, depth, timeout, JS rendering) for web and native sources. */
export function AdvancedDetails({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { provider, currentEntry, configuration, changeConfiguration } = m;
  return (
    <>
      {(provider === 'web' || isNativeEntry(currentEntry)) && (
        <details className="rounded-lg border border-border p-4">
          <summary className="cursor-pointer font-semibold">{t('advancedTitle')}</summary>
          <div className="mt-3">
            <FieldGrid>
              {provider === 'web' && (
                <>
                  <div className="field">
                    <Label htmlFor="source-max-pages">{t('maxPages')}</Label>
                    <Input
                      id="source-max-pages"
                      type="number"
                      min={1}
                      max={10}
                      value={configuration.max_pages}
                      onChange={(event) => changeConfiguration('max_pages', Number(event.target.value))}
                    />
                  </div>
                  <div className="field">
                    <Label htmlFor="source-max-depth">{t('maxDepth')}</Label>
                    <Input
                      id="source-max-depth"
                      type="number"
                      min={0}
                      max={2}
                      value={configuration.max_depth}
                      onChange={(event) => changeConfiguration('max_depth', Number(event.target.value))}
                    />
                  </div>
                  <div className="field">
                    <Label htmlFor="source-timeout">{t('timeout')}</Label>
                    <Input
                      id="source-timeout"
                      type="number"
                      min={1}
                      max={60}
                      value={configuration.timeout_seconds}
                      onChange={(event) => changeConfiguration('timeout_seconds', Number(event.target.value))}
                    />
                  </div>
                  <CheckboxField>
                    <Checkbox
                      id="source-js-render"
                      checked={configuration.js_render}
                      onCheckedChange={(checked) => changeConfiguration('js_render', checked === true)}
                    />
                    <Label htmlFor="source-js-render">{t('renderJavascript')}</Label>
                  </CheckboxField>
                </>
              )}
              {isNativeEntry(currentEntry) && (
                <>
                  <div className="field">
                    <Label htmlFor="source-native-timeout">{t('timeout')}</Label>
                    <Input
                      id="source-native-timeout"
                      type="number"
                      min={1}
                      max={30}
                      value={configuration.timeout_seconds}
                      onChange={(event) => changeConfiguration('timeout_seconds', Number(event.target.value))}
                    />
                  </div>
                </>
              )}
            </FieldGrid>
          </div>
        </details>
      )}
    </>
  );
}

/** Owner-only execution backend choice. */
export function BackendDetails({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { backendChoice, setBackendChoice, workspace, showStep } = m;
  return (
    <>
      {showStep('collect') && workspace.isOwner && (
        <details className="rounded-lg border border-border p-4">
          <summary className="cursor-pointer font-semibold">{t('backendAdvanced')}</summary>
          <div className="mt-3 grid gap-3">
            <div className="field">
              <Label htmlFor="source-backend">{t('backendLabel')}</Label>
              <Select
                value={backendChoice ?? 'native'}
                onValueChange={(value) => {
                  setBackendChoice(value as ExecutionBackend);
                }}
              >
                <SelectTrigger id="source-backend">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="native">{t('backendNative')}</SelectItem>
                  <SelectItem value="n8n">{t('backendN8n')}</SelectItem>
                </SelectContent>
              </Select>
            </div>
            {(backendChoice ?? 'native') === 'n8n' && (
              <p className="text-muted-foreground" role="note">
                {t('backendN8nPrereq')}
              </p>
            )}
          </div>
        </details>
      )}
    </>
  );
}

/** Advanced settings panel shown with the schedule step. */
export function AdvancedSection({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { provider, workspace, currentEntry, showStep } = m;
  return (
    <>
      {showStep('collect') && (provider === 'web' || isNativeEntry(currentEntry) || workspace.isOwner) && (
        <SettingsSection title={t('panelAdvanced')} description={t('panelAdvancedHint')}>
          <AdvancedDetails m={m} />
          <BackendDetails m={m} />
        </SettingsSection>
      )}
    </>
  );
}

/** The editable fieldset: connect, choose-data, schedule and advanced sections (locked while a request runs). */
export function EditorFields({ m }: { m: ConnectorEditorModel }) {
  const { locked } = m;
  return (
    <>
      <fieldset className="source-editor-fields" disabled={locked}>
        <div className="form source-editor-form">
          <ConnectStep m={m} />
          <ChooseStep m={m} />
          <ScheduleStep m={m} />
          <AdvancedSection m={m} />
        </div>
      </fieldset>
    </>
  );
}

/** Provider quota limits. */
export function QuotaSection({ m }: { m: ConnectorEditorModel }) {
  const t = useTranslations('sources');
  const { currentEntry, showStep } = m;
  return (
    <>
      {showStep('choose') && currentEntry?.quota_limits && (
        <SettingsSection title={t('quota')}>
          <p className="m-0 text-sm">
            {Object.entries(currentEntry.quota_limits)
              .map(([key, value]) => `${key}: ${value}`)
              .join(' · ')}
          </p>
        </SettingsSection>
      )}
    </>
  );
}
