'use client';

import { useTranslations } from 'next-intl';
import type { ComponentType } from 'react';
import { Checkbox } from '@/components/ui/checkbox';
import { CheckboxField } from '@/components/settings-layout';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import type { ConnectorConfig } from './api';
import { CoinGeckoPicker, LocationButton, ScopeField, ScopeHint, useIdListText } from './scope-fields';

export { ProviderGuide, ProviderTerms } from './provider-guide';
export { ScopeHint } from './scope-fields';

export type NativeProvider = 'youtube' | 'arxiv' | 'huggingface' | 'reddit' | 'bluesky' | 'mastodon' | 'x' | 'github' | 'github_releases' | 'telegram' | 'alpha_vantage' | 'coingecko' | 'open_meteo' | 'google_news' | 'sec_edgar' | 'openalex' | 'fred' | 'openweathermap' | 'openaq' | 'nasa_firms' | 'notion' | 'slack';

type ScopeChange = <K extends keyof ConnectorConfig>(key: K, value: ConnectorConfig[K]) => void;

/** Props every provider scope section receives; sections read only the configuration keys they own. */
export interface ScopeSectionProps {
  provider: NativeProvider;
  configuration: ConnectorConfig;
  disabled: boolean;
  sourceId?: string;
  credentialConfigured?: boolean;
  resetEpoch: number;
  onChange: ScopeChange;
}

const LAT_PATTERN = '-?[0-9]{1,2}(\\.[0-9]+)?';
const LON_PATTERN = '-?[0-9]{1,3}(\\.[0-9]+)?';
/** Bluesky handle (domain name) or did:plc / did:web identifier; kept as a JS string so the escape is a single backslash. */
const BLUESKY_ACTOR_PATTERN = '(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\\.)+[A-Za-z]{2,63}|did:plc:[a-z2-7]{24}|did:web:[A-Za-z0-9.%:-]{1,220}';
/** Splits a comma/space separated list of terms. */
const splitTerms = (value: string) => value.split(/[\s,]+/).filter(Boolean);
/** Parses a numeric text field; an emptied field is `undefined`. */
const toNumber = (value: string) => (value === '' ? undefined : Number(value));

/** SEC EDGAR scope: CIK and the contact e-mail the SEC requires in the User-Agent. */
function SecEdgarScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return <>
    <ScopeField name="sec_cik" label={t('secCik')} value={configuration.sec_cik ?? ''} onValue={(value) => onChange('sec_cik', value)} disabled={disabled} maxLength={10} pattern="[0-9]{10}" />
    <ScopeField name="sec_contact_email" label={t('secContactEmail')} value={configuration.sec_contact_email ?? ''} onValue={(value) => onChange('sec_contact_email', value)} disabled={disabled} maxLength={254} />
    <small className="text-muted-foreground">{t('secHelp')}</small>
  </>;
}

/** Google News scope: query, optional site restriction and result locale. */
function GoogleNewsScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return <>
    <ScopeField name="news_query" label={t('newsQuery')} value={configuration.news_query ?? ''} onValue={(value) => onChange('news_query', value)} disabled={disabled} maxLength={200} />
    <div className="field">
      <Label htmlFor="source-scope-news_site">{t('newsSite')}</Label>
      <Select value={configuration.news_site ?? 'any'} onValueChange={(value) => onChange('news_site', value as ConnectorConfig['news_site'])} disabled={disabled}>
        <SelectTrigger id="source-scope-news_site"><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value="any">{t('newsSiteAny')}</SelectItem>
          {(['reuters.com', 'apnews.com', 'bbc.com', 'vnexpress.net'] as const).map((site) => <SelectItem key={site} value={site}>{site}</SelectItem>)}
        </SelectContent>
      </Select>
    </div>
    <div className="field">
      <Label htmlFor="source-scope-news_locale">{t('newsLocale')}</Label>
      <Select value={configuration.news_locale ?? 'en-US'} onValueChange={(value) => onChange('news_locale', value as ConnectorConfig['news_locale'])} disabled={disabled}>
        <SelectTrigger id="source-scope-news_locale"><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value="en-US">{t('newsLocaleEn')}</SelectItem>
          <SelectItem value="vi-VN">{t('newsLocaleVi')}</SelectItem>
        </SelectContent>
      </Select>
    </div>
    <small className="text-muted-foreground">{t('newsHelp')}</small>
  </>;
}

/** OpenAlex scope: one scholarly search query. */
function OpenAlexScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return <>
    <ScopeField name="scholarly_query" label={t('scholarlyQuery')} value={configuration.scholarly_query ?? ''} onValue={(value) => onChange('scholarly_query', value)} disabled={disabled} maxLength={200} />
    <small className="text-muted-foreground">{t('scholarlyHelp')}</small>
  </>;
}

/** FRED scope: series id, upper-cased as typed. */
function FredScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return <ScopeField name="fred_series_id" label={t('fredSeriesId')} value={configuration.fred_series_id ?? ''} onValue={(value) => onChange('fred_series_id', value.toUpperCase())} disabled={disabled} maxLength={25} pattern="[A-Z0-9_]{1,25}" />;
}

/** OpenWeatherMap scope: coordinates, fillable from the device location. */
function OpenWeatherMapScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return <>
    <LocationButton disabled={disabled} onLocate={(latitude, longitude) => { onChange('owm_latitude', latitude); onChange('owm_longitude', longitude); }} />
    <ScopeField name="owm_latitude" label={t('weatherLatitude')} value={String(configuration.owm_latitude ?? '')} onValue={(value) => onChange('owm_latitude', toNumber(value))} disabled={disabled} maxLength={24} pattern={LAT_PATTERN} />
    <ScopeField name="owm_longitude" label={t('weatherLongitude')} value={String(configuration.owm_longitude ?? '')} onValue={(value) => onChange('owm_longitude', toNumber(value))} disabled={disabled} maxLength={24} pattern={LON_PATTERN} />
  </>;
}

/** OpenAQ scope: numeric location id. */
function OpenAqScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return <ScopeField name="openaq_location_id" label={t('openaqLocationId')} value={String(configuration.openaq_location_id ?? '')} onValue={(value) => onChange('openaq_location_id', toNumber(value))} disabled={disabled} maxLength={10} pattern="[0-9]{1,10}" />;
}

/** NASA FIRMS scope: three-letter country code, upper-cased as typed. */
function NasaFirmsScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return <ScopeField name="firms_country" label={t('firmsCountry')} value={configuration.firms_country ?? ''} onValue={(value) => onChange('firms_country', value.toUpperCase())} disabled={disabled} maxLength={3} pattern="[A-Z]{3}" />;
}

/** YouTube scope: channel id. */
function YoutubeScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return <ScopeField name="youtube_channel_id" label={t('youtubeChannelId')} value={configuration.youtube_channel_id ?? ''} onValue={(value) => onChange('youtube_channel_id', value)} disabled={disabled} maxLength={24} pattern="UC[A-Za-z0-9_-]{22}" />;
}

/** Reddit scope: one subreddit (read-only access). */
function RedditScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return <><ScopeField name="reddit_subreddit" label={t('redditSubreddit')} value={configuration.reddit_subreddit ?? ''} onValue={(value) => onChange('reddit_subreddit', value.trim())} disabled={disabled} maxLength={21} pattern="[A-Za-z0-9_]{2,21}" /><small className="text-muted-foreground">{t('redditReadOnlyHelp')}</small></>;
}

/** Bluesky scope: handle (domain) or DID. */
function BlueskyScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return <ScopeField name="bluesky_actor" label={t('blueskyActor')} value={configuration.bluesky_actor ?? ''} onValue={(value) => onChange('bluesky_actor', value.trim())} disabled={disabled} maxLength={253} pattern={BLUESKY_ACTOR_PATTERN} />;
}

/** One optional text input with its hint, used by the mutually-exclusive scopes (Mastodon, X); an emptied value is sent as undefined. */
function OptionalScopeInput({ name, label, value, required, maxLength, type, disabled, onValue }: {
  name: keyof ConnectorConfig; label: string; value: string; required: boolean; maxLength: number; type?: 'url'; disabled: boolean; onValue: (raw: string) => void;
}) {
  return <div className="field">
    <Label htmlFor={`source-scope-${name}`}>{label}</Label>
    <Input id={`source-scope-${name}`} type={type} required={required} maxLength={maxLength} disabled={disabled} value={value} aria-describedby={`source-hint-${name}`} onChange={(event) => onValue(event.target.value)} />
    <ScopeHint field={name} />
  </div>;
}

/** Mastodon scope. Account and hashtag are mutually exclusive on the server, so each is optional here and the empty one is sent as undefined. */
function MastodonScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return <>
    <OptionalScopeInput name="mastodon_instance_url" type="url" label={t('mastodonInstanceUrl')} required maxLength={512} disabled={disabled} value={configuration.mastodon_instance_url ?? ''} onValue={(raw) => onChange('mastodon_instance_url', raw.trim() || undefined)} />
    <OptionalScopeInput name="mastodon_account" label={t('mastodonAccount')} required={!configuration.mastodon_hashtag} maxLength={320} disabled={disabled} value={configuration.mastodon_account ?? ''} onValue={(raw) => onChange('mastodon_account', raw.trim() || undefined)} />
    <OptionalScopeInput name="mastodon_hashtag" label={t('mastodonHashtag')} required={!configuration.mastodon_account} maxLength={100} disabled={disabled} value={configuration.mastodon_hashtag ?? ''} onValue={(raw) => onChange('mastodon_hashtag', raw.trim().replace(/^#/, '') || undefined)} />
    <small className="text-muted-foreground">{t('mastodonHelp')}</small>
  </>;
}

/** X scope. The server requires exactly one of account or search query; each is optional here and the empty one is sent as undefined. */
function XScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return <>
    <OptionalScopeInput name="x_account" label={t('xAccount')} required={!configuration.x_query} maxLength={16} disabled={disabled} value={configuration.x_account ?? ''} onValue={(raw) => onChange('x_account', raw.trim().replace(/^@/, '') || undefined)} />
    <OptionalScopeInput name="x_query" label={t('xQuery')} required={!configuration.x_account} maxLength={512} disabled={disabled} value={configuration.x_query ?? ''} onValue={(raw) => onChange('x_query', raw.trim() || undefined)} />
    <small className="text-muted-foreground">{t('xScopeHelp')}</small>
  </>;
}

/** arXiv scope: category. */
function ArxivScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return <ScopeField name="arxiv_category" label={t('arxivCategory')} value={configuration.arxiv_category ?? ''} onValue={(value) => onChange('arxiv_category', value)} disabled={disabled} maxLength={64} pattern="[A-Za-z][A-Za-z0-9.-]{0,63}" />;
}

/** Hugging Face scope: author. */
function HuggingFaceScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return <ScopeField name="huggingface_author" label={t('huggingfaceAuthor')} value={configuration.huggingface_author ?? ''} onValue={(value) => onChange('huggingface_author', value)} disabled={disabled} maxLength={96} pattern="[A-Za-z0-9][A-Za-z0-9_-]{0,95}" />;
}

const GITHUB_INCLUDES = [['include_issues', 'githubIssues'], ['include_pulls', 'githubPulls'], ['include_commits', 'githubCommits'], ['include_releases', 'githubReleases']] as const;

/** GitHub-only scope extras: which activity kinds to include (commits/releases default on) and the history window in days. */
function GithubActivityOptions({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return <>
    {GITHUB_INCLUDES.map(([key, label]) => <CheckboxField key={key}>
      <Checkbox id={`source-scope-${key}`} disabled={disabled} checked={configuration[key] ?? (key === 'include_commits' || key === 'include_releases')} onCheckedChange={(checked) => onChange(key, checked === true)} />
      <Label htmlFor={`source-scope-${key}`}>{t(label)}</Label>
    </CheckboxField>)}
    <div className="field">
      <Label htmlFor="source-scope-github_history_days">{t('githubHistoryDays')}</Label>
      <Input id="source-scope-github_history_days" type="number" min={1} max={365} value={configuration.github_history_days ?? 90} disabled={disabled} aria-describedby="source-hint-github_history_days" onChange={(event) => onChange('github_history_days', Number(event.target.value))} />
      <ScopeHint field="github_history_days" />
    </div>
  </>;
}

/** GitHub / GitHub releases scope: owner and repository; the full GitHub provider adds activity options. */
function GithubScope(props: ScopeSectionProps) {
  const t = useTranslations('sources');
  const { configuration, disabled, onChange, provider } = props;
  return <>
    <ScopeField name="github_owner" label={t('githubOwner')} value={configuration.github_owner ?? ''} onValue={(value) => onChange('github_owner', value)} disabled={disabled} maxLength={39} pattern="[A-Za-z0-9][A-Za-z0-9-]{0,38}" />
    <ScopeField name="github_repository" label={t('githubRepository')} value={configuration.github_repository ?? ''} onValue={(value) => onChange('github_repository', value)} disabled={disabled} maxLength={100} pattern="[A-Za-z0-9_.-]{1,100}" />
    {provider === 'github' && <GithubActivityOptions {...props} />}
  </>;
}

/** Alpha Vantage scope: up to five symbols, quote currency and exchange timezone. */
function AlphaVantageScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return <>
    <ScopeField name="market_symbols" label={t('marketSymbols')} value={(configuration.market_symbols ?? []).join(', ')} onValue={(value) => onChange('market_symbols', splitTerms(value).slice(0, 5))} disabled={disabled} maxLength={110} />
    <ScopeField name="market_currency" label={t('marketCurrency')} value={configuration.market_currency ?? ''} onValue={(value) => onChange('market_currency', value.toUpperCase())} disabled={disabled} maxLength={3} pattern="[A-Z]{3}" />
    <ScopeField name="market_exchange_timezone" label={t('marketExchangeTimezone')} value={configuration.market_exchange_timezone ?? ''} onValue={(value) => onChange('market_exchange_timezone', value)} disabled={disabled} maxLength={64} />
  </>;
}

/** CoinGecko scope: asset picker backed by the keyed coin search. */
function CoinGeckoScope({ configuration, disabled, sourceId, credentialConfigured, onChange }: ScopeSectionProps) {
  return <CoinGeckoPicker sourceId={sourceId ?? ''} credentialConfigured={credentialConfigured ?? false} disabled={disabled} assets={configuration.coingecko_assets} onChange={(assets) => onChange('coingecko_assets', assets)} />;
}

/** Open-Meteo scope: coordinates (fillable from the device, which also sets the browser timezone), timezone and metrics. */
function OpenMeteoScope({ configuration, disabled, onChange }: ScopeSectionProps) {
  const t = useTranslations('sources');
  return <>
    <LocationButton disabled={disabled} onLocate={(latitude, longitude) => {
      onChange('weather_latitude', latitude);
      onChange('weather_longitude', longitude);
      // The browser zone matches the located device, so it replaces the default forecast timezone.
      onChange('weather_timezone', Intl.DateTimeFormat().resolvedOptions().timeZone);
    }} />
    <ScopeField name="weather_latitude" label={t('weatherLatitude')} value={String(configuration.weather_latitude ?? '')} onValue={(value) => onChange('weather_latitude', toNumber(value))} disabled={disabled} maxLength={24} pattern={LAT_PATTERN} />
    <ScopeField name="weather_longitude" label={t('weatherLongitude')} value={String(configuration.weather_longitude ?? '')} onValue={(value) => onChange('weather_longitude', toNumber(value))} disabled={disabled} maxLength={24} pattern={LON_PATTERN} />
    <ScopeField name="weather_timezone" label={t('weatherTimezone')} value={configuration.weather_timezone ?? ''} onValue={(value) => onChange('weather_timezone', value)} disabled={disabled} maxLength={64} />
    <ScopeField name="weather_metrics" label={t('weatherMetrics')} value={(configuration.weather_metrics ?? []).join(', ')} onValue={(value) => onChange('weather_metrics', splitTerms(value).slice(0, 4))} disabled={disabled} maxLength={200} />
  </>;
}

/** Id-list scopes (Notion pages, Slack channels, Telegram chats) share one raw-text field; only keys and messages differ. */
const ID_LIST_SCOPES = {
  notion: { key: 'notion_page_ids', label: 'notionPageIds', help: 'notionPageIdsHelp', helpId: 'source-scope-notion-help', maxLength: 13000 },
  slack: { key: 'slack_channel_ids', label: 'slackChannelIds', help: 'slackChannelIdsHelp', helpId: 'source-scope-slack-help', maxLength: 8400 },
  telegram: { key: 'telegram_chat_ids', label: 'telegramChannelIds', help: 'telegramChannelHelp', helpId: 'source-scope-telegram-help', maxLength: 2200 },
} as const;

/** Raw-text id list field bound to one configuration key; see `useIdListText` for the sync rules with the draft. */
function IdListScope({ spec, configuration, disabled, resetEpoch, onChange }: ScopeSectionProps & { spec: (typeof ID_LIST_SCOPES)[keyof typeof ID_LIST_SCOPES] }) {
  const t = useTranslations('sources');
  const text = useIdListText(configuration[spec.key] ?? [], resetEpoch);
  return <div className="field">
    <Label htmlFor={`source-scope-${spec.key}`}>{t(spec.label)}</Label>
    <Input id={`source-scope-${spec.key}`} maxLength={spec.maxLength} disabled={disabled} required value={text.value} aria-describedby={spec.helpId} onChange={(event) => onChange(spec.key, text.edit(event.target.value))} />
    <small id={spec.helpId} className="text-muted-foreground">{t(spec.help)}</small>
  </div>;
}

/**
 * Provider -> scope section registry. A new native provider plugs in by adding one entry here (and its name to
 * `NativeProvider`); `ProviderScope` itself does not change.
 */
const SCOPE_SECTIONS: Record<NativeProvider, ComponentType<ScopeSectionProps>> = {
  sec_edgar: SecEdgarScope, google_news: GoogleNewsScope, openalex: OpenAlexScope, fred: FredScope, openweathermap: OpenWeatherMapScope,
  openaq: OpenAqScope, nasa_firms: NasaFirmsScope, youtube: YoutubeScope, reddit: RedditScope, bluesky: BlueskyScope,
  mastodon: MastodonScope, x: XScope, arxiv: ArxivScope, huggingface: HuggingFaceScope, github: GithubScope, github_releases: GithubScope,
  alpha_vantage: AlphaVantageScope, coingecko: CoinGeckoScope, open_meteo: OpenMeteoScope,
  notion: (props) => <IdListScope {...props} spec={ID_LIST_SCOPES.notion} />,
  slack: (props) => <IdListScope {...props} spec={ID_LIST_SCOPES.slack} />,
  telegram: (props) => <IdListScope {...props} spec={ID_LIST_SCOPES.telegram} />,
};

/** Renders bounded native-provider scopes, including Reddit subreddits and selected Notion pages/Slack channels; values stay in the revision-fenced draft. */
export function ProviderScope(props: ScopeSectionProps) {
  const Section = SCOPE_SECTIONS[props.provider] ?? SCOPE_SECTIONS.telegram;
  return <Section {...props} />;
}
