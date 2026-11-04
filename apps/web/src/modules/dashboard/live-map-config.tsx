'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useId, useState } from 'react';
import { useResetOnChange } from '@/modules/shared/use-reset-on-change';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { PasswordInput } from '@/components/ui/password-input';
import { apiRequest, csrfHeaders } from '@/core/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { useWorkspace } from '@/core/workspace-context';

type Config = { revision: number; openweather_configured: boolean; firms_configured: boolean; sec_contact_email: string | null; is_operator: boolean; terms_catalog: { id: string; label: string; terms_url: string; version: string; attribution: string; eligibility: 'open' | 'personal' | 'noncommercial' | 'review'; accepted: boolean; acknowledged: boolean; review_state: string | null }[] };

/** Edit write-only live provider credentials using the server revision, without loading stored keys into the browser. */
export function LiveMapConfig() {
  const t = useTranslations('dashboard');
  const id = useId();
  const workspace = useWorkspace();
  const session = useWorkspaceSession();
  const client = useQueryClient();
  const key = ['live-map-config', workspace.selection?.id];
  const config = useQuery({ queryKey: key, queryFn: ({ signal }) => apiRequest<Config>('/api/v1/live-map/config', { signal }), enabled: Boolean(workspace.selection?.id), gcTime: 0 });
  const [termsSelected, setTermsSelected] = useState<string[]>([]);
  const [termsUse, setTermsUse] = useState<'personal' | 'noncommercial' | 'commercial'>('personal');
  const [termsReview, setTermsReview] = useState(false);
  const [termsEvidence, setTermsEvidence] = useState('');
  const [weatherKey, setWeatherKey] = useState('');
  const [firmsKey, setFirmsKey] = useState('');
  const [removeWeather, setRemoveWeather] = useState(false);
  const [removeFirms, setRemoveFirms] = useState(false);
  const [email, setEmail] = useState('');
  const [emailChanged, setEmailChanged] = useState(false);
  // Clear unsaved secrets immediately when workspace authority changes.
  useResetOnChange([workspace.selection?.id], () => { setWeatherKey(''); setFirmsKey(''); setEmail(''); setEmailChanged(false); setRemoveWeather(false); setRemoveFirms(false); setTermsSelected([]); setTermsReview(false); setTermsEvidence(''); });
  const save = useMutation({
    mutationFn: () => apiRequest<Config>('/api/v1/live-map/config', { method: 'PUT', headers: { 'Content-Type': 'application/json', ...csrfHeaders(session.csrfToken) }, body: JSON.stringify({ expected_revision: config.data?.revision, ...(removeWeather ? { openweather_key: null } : weatherKey ? { openweather_key: weatherKey } : {}), ...(removeFirms ? { firms_key: null } : firmsKey ? { firms_key: firmsKey } : {}), ...(emailChanged ? { sec_contact_email: email.trim() || null } : {}) }) }),
    onSuccess: (saved) => { client.setQueryData(key, saved); void client.resetQueries({ queryKey: ['live-map'] }); setWeatherKey(''); setFirmsKey(''); setRemoveWeather(false); setRemoveFirms(false); setEmailChanged(false); },
  });
  const acceptTerms = useMutation({
    mutationFn: () => apiRequest<Config>('/api/v1/live-map/config', { method: 'PUT', headers: { 'Content-Type': 'application/json', ...csrfHeaders(session.csrfToken) }, body: JSON.stringify({ expected_revision: config.data?.revision, terms_accept: termsSelected, terms_use: termsUse, terms_review_confirmed: termsReview, terms_evidence_ref: termsEvidence.trim() || undefined }) }),
    onSuccess: (saved) => { client.setQueryData(key, saved); setTermsSelected([]); setTermsReview(false); setTermsEvidence(''); void client.resetQueries({ queryKey: ['live-map'] }); },
  });
  const revokeTerms = useMutation({
    mutationFn: (provider: string) => apiRequest<Config>('/api/v1/live-map/config', { method: 'PUT', headers: { 'Content-Type': 'application/json', ...csrfHeaders(session.csrfToken) }, body: JSON.stringify({ expected_revision: config.data?.revision, terms_revoke: [provider] }) }),
    // Clear the old response as well as stopping future reads after consent changes.
    onSuccess: (saved) => { client.setQueryData(key, saved); void client.resetQueries({ queryKey: ['live-map'] }); },
  });
  if (config.isPending) return <p role="status" className="text-xs">{t('liveMapSourceLoading')}</p>;
  if (config.isError || !config.data) return <p role="alert" className="text-xs text-destructive">{t('liveMapConfigLoadError')} <Button type="button" variant="outline" size="sm" onClick={() => void config.refetch()}>{t('retry')}</Button></p>;
  return <div className="space-y-2 rounded border border-border p-2"><h5 className="text-xs font-semibold">{t('liveMapConfig')}</h5>
    <p className="text-xs text-muted-foreground">{t('liveMapConfigHelp')}</p>
    <fieldset className="space-y-2"><legend className="text-xs font-semibold">{t('liveMapTerms')}</legend><p className="text-xs text-muted-foreground">{t('liveMapTermsHelp')}</p>
      {(config.data.terms_catalog ?? []).map((term) => <div key={term.id} className="flex items-start gap-2 text-xs"><Checkbox aria-label={term.label} disabled={!workspace.isOwner || term.acknowledged} checked={term.acknowledged || termsSelected.includes(term.id)} onCheckedChange={(checked) => setTermsSelected((current) => checked === true ? [...current, term.id] : current.filter((value) => value !== term.id))} /><span>{term.label} · <a href={term.terms_url} target="_blank" rel="noreferrer" className="underline">{t('liveMapTermsLink')}</a> · {term.version} · {t(`liveMapEligibility_${term.eligibility}`)} {term.accepted ? `· ${t('liveMapTermsAccepted')}` : term.acknowledged ? `· ${t('liveMapTermsPending')}` : ''}<span className="block">{term.attribution}</span></span>{term.acknowledged && <Button type="button" size="sm" variant="outline" disabled={!workspace.isOwner || revokeTerms.isPending || acceptTerms.isPending || save.isPending} onClick={() => revokeTerms.mutate(term.id)}>{t('liveMapTermsRevoke', { provider: term.label })}</Button>}</div>)}
      <Label htmlFor={`${id}-terms-use`}>{t('liveMapTermsUse')}</Label><Select value={termsUse} onValueChange={(value) => setTermsUse(value as typeof termsUse)}><SelectTrigger id={`${id}-terms-use`}><SelectValue /></SelectTrigger><SelectContent><SelectItem value="personal">{t('liveMapUsePersonal')}</SelectItem><SelectItem value="noncommercial">{t('liveMapUseNoncommercial')}</SelectItem><SelectItem value="commercial">{t('liveMapUseCommercial')}</SelectItem></SelectContent></Select>
      <label className="flex items-start gap-2 text-xs"><Checkbox checked={termsReview} disabled={!workspace.isOwner || !config.data.is_operator} onCheckedChange={(value) => setTermsReview(value === true)} />{t('liveMapTermsReview')}</label>
      <div><Label htmlFor={`${id}-terms-evidence`}>{t('liveMapTermsEvidence')}</Label><Input id={`${id}-terms-evidence`} value={termsEvidence} disabled={!workspace.isOwner || !config.data.is_operator} onChange={(event) => setTermsEvidence(event.target.value)} /></div>
      {revokeTerms.isError && <p role="alert" className="text-xs text-destructive">{t('liveMapTermsRevokeError')}</p>}{revokeTerms.isSuccess && <p role="status" className="text-xs">{t('liveMapTermsRevoked')}</p>}
      {acceptTerms.isError && <p role="alert" className="text-xs text-destructive">{t('liveMapTermsError')}</p>}{acceptTerms.isSuccess && <p role="status" className="text-xs">{t('liveMapTermsSaved')}</p>}
      <Button type="button" size="sm" disabled={!workspace.isOwner || acceptTerms.isPending || revokeTerms.isPending || save.isPending || termsSelected.length === 0} onClick={() => acceptTerms.mutate()}>{t('liveMapTermsAccept')}</Button>
    </fieldset>
    <div><Label htmlFor={`${id}-weather`}>{t('liveMapWeatherKey')}</Label><PasswordInput id={`${id}-weather`} autoComplete="new-password" value={weatherKey} disabled={!workspace.isOwner || removeWeather} onChange={(event) => setWeatherKey(event.target.value)} /><p className="text-xs text-muted-foreground">{config.data.openweather_configured ? t('liveMapConfigured') : t('liveMapNotConfigured')}</p>{config.data.openweather_configured && <label className="flex gap-2 text-xs"><Checkbox checked={removeWeather} disabled={!workspace.isOwner} onCheckedChange={(value) => setRemoveWeather(value === true)} />{t('liveMapRemoveKey')}</label>}</div>
    <div><Label htmlFor={`${id}-firms`}>{t('liveMapFirmsKey')}</Label><PasswordInput id={`${id}-firms`} autoComplete="new-password" value={firmsKey} disabled={!workspace.isOwner || removeFirms} onChange={(event) => setFirmsKey(event.target.value)} /><p className="text-xs text-muted-foreground">{config.data.firms_configured ? t('liveMapConfigured') : t('liveMapNotConfigured')}</p>{config.data.firms_configured && <label className="flex gap-2 text-xs"><Checkbox checked={removeFirms} disabled={!workspace.isOwner} onCheckedChange={(value) => setRemoveFirms(value === true)} />{t('liveMapRemoveKey')}</label>}</div>
    <div><Label htmlFor={`${id}-email`}>{t('liveMapSecEmail')}</Label><Input id={`${id}-email`} type="email" disabled={!workspace.isOwner} value={emailChanged ? email : config.data.sec_contact_email ?? ''} onChange={(event) => { setEmail(event.target.value); setEmailChanged(true); }} /></div>
    {save.isError && <p role="alert" className="text-xs text-destructive">{t('liveMapConfigSaveError')}</p>}{save.isSuccess && <p role="status" className="text-xs">{t('liveMapConfigSaved')}</p>}
    <Button type="button" size="sm" disabled={!workspace.isOwner || save.isPending || acceptTerms.isPending || revokeTerms.isPending || !(weatherKey || firmsKey || removeWeather || removeFirms || emailChanged)} onClick={() => save.mutate()}>{t('liveMapConfigSave')}</Button>
  </div>;
}
