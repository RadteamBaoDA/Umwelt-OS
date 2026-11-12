'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useId, useState } from 'react';
import { useResetOnChange } from '@/modules/shared';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { PasswordInput } from '@/components/ui/password-input';
import { apiRequest, csrfHeaders } from '@/core/api';
import type { components } from '@/core/api-types.gen';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { useWorkspace } from '@/core/workspace-context';

/** Live-map config as the backend serializes it (generated from the OpenAPI schema). */
type Config = components['schemas']['LiveConfigResponse'];

/** Terms-acceptance draft: selected providers, use class, operator review confirmation and evidence reference. */
function useTermsDraft() {
  const [termsSelected, setTermsSelected] = useState<string[]>([]);
  const [termsUse, setTermsUse] = useState<'personal' | 'noncommercial' | 'commercial'>('personal');
  const [termsReview, setTermsReview] = useState(false);
  const [termsEvidence, setTermsEvidence] = useState('');
  return { termsSelected, setTermsSelected, termsUse, setTermsUse, termsReview, setTermsReview, termsEvidence, setTermsEvidence };
}

/** Write-only credential draft: new keys, removal flags and the SEC contact email (never prefilled with stored keys). */
function useCredentialsDraft() {
  const [weatherKey, setWeatherKey] = useState('');
  const [firmsKey, setFirmsKey] = useState('');
  const [removeWeather, setRemoveWeather] = useState(false);
  const [removeFirms, setRemoveFirms] = useState(false);
  const [email, setEmail] = useState('');
  const [emailChanged, setEmailChanged] = useState(false);
  return { weatherKey, setWeatherKey, firmsKey, setFirmsKey, removeWeather, setRemoveWeather, removeFirms, setRemoveFirms, email, setEmail, emailChanged, setEmailChanged };
}

type TermsDraft = ReturnType<typeof useTermsDraft>;
type CredentialsDraft = ReturnType<typeof useCredentialsDraft>;

/**
 * Save-credentials, accept-terms and revoke-terms mutations. Each sends the server revision the form was loaded
 * with (`expected_revision`), replaces the cached config with the response and resets live-map queries so a consent
 * or credential change takes effect immediately; accepted/saved drafts are cleared.
 */
function useLiveConfigMutations(configKey: (string | undefined)[], revision: number | undefined, terms: TermsDraft, creds: CredentialsDraft) {
  const session = useWorkspaceSession();
  const client = useQueryClient();
  const put = (body: object) => apiRequest<Config>('/api/v1/live-map/config', { method: 'PUT', headers: { 'Content-Type': 'application/json', ...csrfHeaders(session.csrfToken) }, body: JSON.stringify({ expected_revision: revision, ...body }) });
  const { weatherKey, firmsKey, removeWeather, removeFirms, email, emailChanged } = creds;
  const save = useMutation({
    mutationFn: () => put({ ...(removeWeather ? { openweather_key: null } : weatherKey ? { openweather_key: weatherKey } : {}), ...(removeFirms ? { firms_key: null } : firmsKey ? { firms_key: firmsKey } : {}), ...(emailChanged ? { sec_contact_email: email.trim() || null } : {}) }),
    onSuccess: (saved) => { client.setQueryData(configKey, saved); void client.resetQueries({ queryKey: ['live-map'] }); creds.setWeatherKey(''); creds.setFirmsKey(''); creds.setRemoveWeather(false); creds.setRemoveFirms(false); creds.setEmailChanged(false); },
  });
  const acceptTerms = useMutation({
    mutationFn: () => put({ terms_accept: terms.termsSelected, terms_use: terms.termsUse, terms_review_confirmed: terms.termsReview, terms_evidence_ref: terms.termsEvidence.trim() || undefined }),
    onSuccess: (saved) => { client.setQueryData(configKey, saved); terms.setTermsSelected([]); terms.setTermsReview(false); terms.setTermsEvidence(''); void client.resetQueries({ queryKey: ['live-map'] }); },
  });
  const revokeTerms = useMutation({
    mutationFn: (provider: string) => put({ terms_revoke: [provider] }),
    // Clear the old response as well as stopping future reads after consent changes.
    onSuccess: (saved) => { client.setQueryData(configKey, saved); void client.resetQueries({ queryKey: ['live-map'] }); },
  });
  return { save, acceptTerms, revokeTerms };
}

type ConfigMutations = ReturnType<typeof useLiveConfigMutations>;

/** Terms of the live data providers: acknowledgement checkboxes, use class, operator review and accept/revoke actions. */
function TermsFieldset({ config, terms, mutations }: { config: Config; terms: TermsDraft; mutations: ConfigMutations }) {
  const t = useTranslations('dashboard');
  const id = useId();
  const workspace = useWorkspace();
  const { save, acceptTerms, revokeTerms } = mutations;
  const { termsSelected, setTermsSelected, termsUse, setTermsUse, termsReview, setTermsReview, termsEvidence, setTermsEvidence } = terms;
  return <fieldset className="space-y-2"><legend className="text-xs font-semibold">{t('liveMapTerms')}</legend><p className="text-xs text-muted-foreground">{t('liveMapTermsHelp')}</p>
    {(config.terms_catalog ?? []).map((term) => <div key={term.id} className="flex items-start gap-2 text-xs"><Checkbox aria-label={term.label} disabled={!workspace.isOwner || term.acknowledged} checked={term.acknowledged || termsSelected.includes(term.id)} onCheckedChange={(checked) => setTermsSelected((current) => checked === true ? [...current, term.id] : current.filter((value) => value !== term.id))} /><span>{term.label} · <a href={term.terms_url} target="_blank" rel="noreferrer" className="underline">{t('liveMapTermsLink')}</a> · {term.version} · {t(`liveMapEligibility_${term.eligibility}`)} {term.accepted ? `· ${t('liveMapTermsAccepted')}` : term.acknowledged ? `· ${t('liveMapTermsPending')}` : ''}<span className="block">{term.attribution}</span></span>{term.acknowledged && <Button type="button" size="sm" variant="outline" disabled={!workspace.isOwner || revokeTerms.isPending || acceptTerms.isPending || save.isPending} onClick={() => revokeTerms.mutate(term.id)}>{t('liveMapTermsRevoke', { provider: term.label })}</Button>}</div>)}
    <Label htmlFor={`${id}-terms-use`}>{t('liveMapTermsUse')}</Label><Select value={termsUse} onValueChange={(value) => setTermsUse(value as typeof termsUse)}><SelectTrigger id={`${id}-terms-use`}><SelectValue /></SelectTrigger><SelectContent><SelectItem value="personal">{t('liveMapUsePersonal')}</SelectItem><SelectItem value="noncommercial">{t('liveMapUseNoncommercial')}</SelectItem><SelectItem value="commercial">{t('liveMapUseCommercial')}</SelectItem></SelectContent></Select>
    <label className="flex items-start gap-2 text-xs"><Checkbox checked={termsReview} disabled={!workspace.isOwner || !config.is_operator} onCheckedChange={(value) => setTermsReview(value === true)} />{t('liveMapTermsReview')}</label>
    <div><Label htmlFor={`${id}-terms-evidence`}>{t('liveMapTermsEvidence')}</Label><Input id={`${id}-terms-evidence`} value={termsEvidence} disabled={!workspace.isOwner || !config.is_operator} onChange={(event) => setTermsEvidence(event.target.value)} /></div>
    {revokeTerms.isError && <p role="alert" className="text-xs text-destructive">{t('liveMapTermsRevokeError')}</p>}{revokeTerms.isSuccess && <p role="status" className="text-xs">{t('liveMapTermsRevoked')}</p>}
    {acceptTerms.isError && <p role="alert" className="text-xs text-destructive">{t('liveMapTermsError')}</p>}{acceptTerms.isSuccess && <p role="status" className="text-xs">{t('liveMapTermsSaved')}</p>}
    <Button type="button" size="sm" disabled={!workspace.isOwner || acceptTerms.isPending || revokeTerms.isPending || save.isPending || termsSelected.length === 0} onClick={() => acceptTerms.mutate()}>{t('liveMapTermsAccept')}</Button>
  </fieldset>;
}

/** One write-only provider key field with its configured state and removal checkbox. */
function KeyField({ id, label, value, onChange, configured, remove, onRemove }: {
  id: string; label: string; value: string; onChange: (value: string) => void; configured: boolean; remove: boolean; onRemove: (remove: boolean) => void;
}) {
  const t = useTranslations('dashboard');
  const workspace = useWorkspace();
  return <div><Label htmlFor={id}>{label}</Label><PasswordInput id={id} autoComplete="new-password" value={value} disabled={!workspace.isOwner || remove} onChange={(event) => onChange(event.target.value)} /><p className="text-xs text-muted-foreground">{configured ? t('liveMapConfigured') : t('liveMapNotConfigured')}</p>{configured && <label className="flex gap-2 text-xs"><Checkbox checked={remove} disabled={!workspace.isOwner} onCheckedChange={(checked) => onRemove(checked === true)} />{t('liveMapRemoveKey')}</label>}</div>;
}

/** Provider keys, SEC contact email, save feedback and the Save button (enabled only with an unsaved change). */
function CredentialsFields({ config, creds, mutations }: { config: Config; creds: CredentialsDraft; mutations: ConfigMutations }) {
  const t = useTranslations('dashboard');
  const id = useId();
  const workspace = useWorkspace();
  const { save, acceptTerms, revokeTerms } = mutations;
  const { weatherKey, firmsKey, removeWeather, removeFirms, email, emailChanged } = creds;
  return <>
    <KeyField id={`${id}-weather`} label={t('liveMapWeatherKey')} value={weatherKey} onChange={creds.setWeatherKey} configured={config.openweather_configured} remove={removeWeather} onRemove={creds.setRemoveWeather} />
    <KeyField id={`${id}-firms`} label={t('liveMapFirmsKey')} value={firmsKey} onChange={creds.setFirmsKey} configured={config.firms_configured} remove={removeFirms} onRemove={creds.setRemoveFirms} />
    <div><Label htmlFor={`${id}-email`}>{t('liveMapSecEmail')}</Label><Input id={`${id}-email`} type="email" disabled={!workspace.isOwner} value={emailChanged ? email : config.sec_contact_email ?? ''} onChange={(event) => { creds.setEmail(event.target.value); creds.setEmailChanged(true); }} /></div>
    {save.isError && <p role="alert" className="text-xs text-destructive">{t('liveMapConfigSaveError')}</p>}{save.isSuccess && <p role="status" className="text-xs">{t('liveMapConfigSaved')}</p>}
    <Button type="button" size="sm" disabled={!workspace.isOwner || save.isPending || acceptTerms.isPending || revokeTerms.isPending || !(weatherKey || firmsKey || removeWeather || removeFirms || emailChanged)} onClick={() => save.mutate()}>{t('liveMapConfigSave')}</Button>
  </>;
}

/** Edit write-only live provider credentials using the server revision, without loading stored keys into the browser. */
export function LiveMapConfig() {
  const t = useTranslations('dashboard');
  const workspace = useWorkspace();
  const key = ['live-map-config', workspace.selection?.id];
  const config = useQuery({ queryKey: key, queryFn: ({ signal }) => apiRequest<Config>('/api/v1/live-map/config', { signal }), enabled: Boolean(workspace.selection?.id), gcTime: 0 });
  const terms = useTermsDraft();
  const creds = useCredentialsDraft();
  // Clear unsaved secrets immediately when workspace authority changes.
  useResetOnChange([workspace.selection?.id], () => { creds.setWeatherKey(''); creds.setFirmsKey(''); creds.setEmail(''); creds.setEmailChanged(false); creds.setRemoveWeather(false); creds.setRemoveFirms(false); terms.setTermsSelected([]); terms.setTermsReview(false); terms.setTermsEvidence(''); });
  const mutations = useLiveConfigMutations(key, config.data?.revision, terms, creds);
  if (config.isPending) return <p role="status" className="text-xs">{t('liveMapSourceLoading')}</p>;
  if (config.isError || !config.data) return <p role="alert" className="text-xs text-destructive">{t('liveMapConfigLoadError')} <Button type="button" variant="outline" size="sm" onClick={() => void config.refetch()}>{t('retry')}</Button></p>;
  return <div className="space-y-2 rounded border border-border p-2"><h5 className="text-xs font-semibold">{t('liveMapConfig')}</h5>
    <p className="text-xs text-muted-foreground">{t('liveMapConfigHelp')}</p>
    <TermsFieldset config={config.data} terms={terms} mutations={mutations} />
    <CredentialsFields config={config.data} creds={creds} mutations={mutations} />
  </div>;
}
