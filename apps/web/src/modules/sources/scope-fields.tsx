'use client';

import { useQuery } from '@tanstack/react-query';
import { useTranslations } from 'next-intl';
import { useEffect, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { ApiError } from '@/core/api';
import { connectorKeys, searchCoinGeckoCoins, type CoinGeckoCoin, type ConnectorConfig } from './api';

/**
 * Input rules for one typed source field (format, delimiter, bounds, example), rendered under the input.
 * The id `source-hint-<field>` is what the input's `aria-describedby` points at; fields without a rule render nothing.
 */
export function ScopeHint({ field }: { field: string }) {
  const t = useTranslations('sources');
  const key = `scopeHint.${field}`;
  if (!t.has(key)) return null;
  return <small id={`source-hint-${field}`} className="text-muted-foreground">{t(key as 'guideTitle')}</small>;
}

/** Pairs a scope field with its translated label, constrained native input and hint (`id` = `source-scope-<name>`). */
export function ScopeField({ name, label, value, onValue, disabled, maxLength, pattern }: {
  name: keyof ConnectorConfig; label: string; value: string; onValue: (value: string) => void; disabled: boolean; maxLength?: number; pattern?: string;
}) {
  return <div className="field">
    <Label htmlFor={`source-scope-${name}`}>{label}</Label>
    <Input id={`source-scope-${name}`} value={value} disabled={disabled} required aria-describedby={`source-hint-${name}`} onChange={(event) => onValue(event.target.value)} maxLength={maxLength} pattern={pattern} />
    <ScopeHint field={name} />
  </div>;
}

/**
 * Text state for a comma/space separated id list that mirrors the draft configuration.
 * The raw text is kept locally so typed separators survive; it is re-synced from the configuration only when the parsed
 * list changed from outside (not by our own echo) or when `resetEpoch` bumps (a reset is authoritative even when the parsed
 * values match). `edit(raw)` stores the raw text and returns the parsed values for the caller to emit.
 */
export function useIdListText(ids: string[], resetEpoch: number): { value: string; edit: (raw: string) => string[] } {
  const signature = JSON.stringify(ids);
  const fromConfig = ids.join(', ');
  const [value, setValue] = useState(fromConfig);
  const lastSignature = useRef(signature);
  const lastResetEpoch = useRef(resetEpoch);
  useEffect(() => {
    if (resetEpoch !== lastResetEpoch.current || signature !== lastSignature.current) {
      lastResetEpoch.current = resetEpoch;
      lastSignature.current = signature;
      setValue(fromConfig);
    }
  }, [signature, resetEpoch, fromConfig]);
  /** Keeps every entered value (authoritative server bound/format rejection) and records our own echo signature. */
  const edit = (raw: string) => {
    setValue(raw);
    const values = raw.split(/[\s,]+/).filter(Boolean);
    lastSignature.current = JSON.stringify(values);
    return values;
  };
  return { value, edit };
}

/** Message key for a failed CoinGecko search: 409 means the key needs refreshing, 429 rate limited, anything else generic. */
function coinSearchErrorKey(error: unknown): 'coingeckoKeyNeedsRefresh' | 'coingeckoSearchLimited' | 'coingeckoSearchFailed' {
  if (error instanceof ApiError && error.status === 409) return 'coingeckoKeyNeedsRefresh';
  if (error instanceof ApiError && error.status === 429) return 'coingeckoSearchLimited';
  return 'coingeckoSearchFailed';
}

/** Chooses up to twenty CoinGecko IDs; the stored ID distinguishes assets with the same ticker symbol. */
export function CoinGeckoPicker({ sourceId, credentialConfigured, disabled, assets, onChange }: { sourceId: string; credentialConfigured: boolean; disabled: boolean; assets?: CoinGeckoCoin[]; onChange: (assets: CoinGeckoCoin[]) => void }) {
  const t = useTranslations('sources');
  const selected = assets ?? [{ id: 'bitcoin', symbol: 'BTC', name: 'Bitcoin' }];
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  useEffect(() => {
    const timer = setTimeout(() => setQuery(search.trim()), 350);
    return () => clearTimeout(timer);
  }, [search]);
  const ready = Boolean(sourceId && credentialConfigured);
  const coins = useQuery({
    queryKey: connectorKeys.coingeckoCoins(sourceId, query),
    queryFn: ({ signal }) => searchCoinGeckoCoins(sourceId, query, signal),
    enabled: ready && query.length >= 2,
    staleTime: 5 * 60_000,
    retry: false,
  });
  return <div className="field">
    <Label htmlFor="source-scope-coingecko-search">{t('coingeckoCoins')}</Label>
    <ul className="grid gap-1" aria-label={t('coingeckoSelected')}>
      {selected.map((coin) => <li key={coin.id} className="flex items-center justify-between gap-2 rounded-md border border-border p-2 text-sm">
        <span className="min-w-0 break-words">{coin.name} ({coin.symbol}) <span className="text-muted-foreground">{coin.id}</span></span>
        <Button type="button" variant="outline" size="sm" disabled={disabled || selected.length === 1} onClick={() => onChange(selected.filter((item) => item.id !== coin.id))} aria-label={t('coingeckoRemove', { name: coin.name })}>{t('coingeckoRemoveButton')}</Button>
      </li>)}
    </ul>
    <Input id="source-scope-coingecko-search" value={search} maxLength={80} disabled={disabled || !ready || selected.length >= 20} placeholder={t('coingeckoSearchPlaceholder')} onChange={(event) => setSearch(event.target.value)} aria-describedby="source-scope-coingecko-help" />
    <small id="source-scope-coingecko-help" className="text-muted-foreground">{ready ? t('coingeckoSearchHelp') : t('coingeckoSaveKeyFirst')}</small>
    {selected.length >= 20 && <small className="text-muted-foreground">{t('coingeckoLimit')}</small>}
    {coins.isPending && query.length >= 2 && ready && <small role="status" className="text-muted-foreground">{t('loading')}</small>}
    {coins.isError && <small role="alert" className="text-destructive">{t(coinSearchErrorKey(coins.error))}</small>}
    {coins.data && query.length >= 2 && <ul className="max-h-48 overflow-y-auto rounded-md border border-border" aria-label={t('coingeckoResults')}>
      {coins.data.items.filter((coin) => !selected.some((item) => item.id === coin.id)).map((coin) => <li key={coin.id}>
        <Button type="button" variant="ghost" className="h-auto min-h-9 w-full justify-start whitespace-normal text-left pointer-coarse:min-h-11" disabled={disabled || selected.length >= 20} onClick={() => { onChange([...selected, coin]); setSearch(''); setQuery(''); }}>{coin.name} ({coin.symbol}) · {coin.id}</Button>
      </li>)}
    </ul>}
  </div>;
}

/**
 * Fills coordinates from the browser Geolocation API after an explicit owner click (the browser asks for permission).
 * Coordinates are rounded to 4 decimals (~11 m), enough for weather and within the server bounds; denial or
 * failure shows a hint and leaves the typed values unchanged.
 */
export function LocationButton({ disabled, onLocate }: { disabled: boolean; onLocate: (latitude: number, longitude: number) => void }) {
  const t = useTranslations('sources');
  const [state, setState] = useState<'idle' | 'locating' | 'denied' | 'unavailable'>('idle');
  const supported = typeof navigator !== 'undefined' && 'geolocation' in navigator;
  /** Requests one position and reports it rounded, or records why it could not be read. */
  const locate = () => {
    setState('locating');
    navigator.geolocation.getCurrentPosition(
      ({ coords }) => {
        setState('idle');
        onLocate(Math.round(coords.latitude * 1e4) / 1e4, Math.round(coords.longitude * 1e4) / 1e4);
      },
      (failure) => setState(failure.code === failure.PERMISSION_DENIED ? 'denied' : 'unavailable'),
      { timeout: 10_000, maximumAge: 600_000 },
    );
  };
  return <div className="flex flex-wrap items-center gap-2">
    <Button type="button" variant="outline" disabled={disabled || !supported || state === 'locating'} onClick={locate}>
      {state === 'locating' ? t('locating') : t('useMyLocation')}
    </Button>
    {(state === 'denied' || state === 'unavailable' || !supported) && <small role="status" className="text-muted-foreground">
      {t(state === 'denied' ? 'locationDenied' : 'locationUnavailable')}
    </small>}
  </div>;
}
