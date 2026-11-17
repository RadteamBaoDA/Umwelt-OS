import { useLocale, useTranslations } from 'next-intl';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { MediaMultiChipPicker } from './gadget-editor-pickers';
import { MEDIA_SELECTOR_LIMITS } from './gadget-editor-model';
import type { DraftAccess, SectionFacts } from './gadget-editor-sections';
import type { GadgetScope } from './api';

const CANTONESE_LANGUAGE = 'zh-hk';

/** Literal catalog keys for normalized media enum values used by the editor. */
const MEDIA_VALUE_KEY = {
  movie: 'media.values.movie',
  anime: 'media.values.anime',
  manga: 'media.values.manga',
  manhwa: 'media.values.manhwa',
  manhua: 'media.values.manhua',
  light_novel: 'media.values.light_novel',
  book: 'media.values.book',
  audiobook: 'media.values.audiobook',
  youtube: 'media.values.youtube',
  tv: 'media.values.tv',
  podcast: 'media.values.podcast',
  music: 'media.values.music',
  game: 'media.values.game',
  action: 'media.values.action',
  adventure: 'media.values.adventure',
  sci_fi: 'media.values.sci_fi',
  fantasy: 'media.values.fantasy',
  romance: 'media.values.romance',
  thriller: 'media.values.thriller',
  horror: 'media.values.horror',
  comedy: 'media.values.comedy',
  drama: 'media.values.drama',
  mystery: 'media.values.mystery',
  documentary: 'media.values.documentary',
  animation: 'media.values.animation',
  slice_of_life: 'media.values.slice_of_life',
  history: 'media.values.history',
  sports: 'media.values.sports',
  non_fiction: 'media.values.non_fiction',
  programming: 'media.values.programming',
  rpg: 'media.values.rpg',
  strategy: 'media.values.strategy',
  en: 'media.values.en',
  vi: 'media.values.vi',
  ja: 'media.values.ja',
  ko: 'media.values.ko',
  zh: 'media.values.zh',
  [CANTONESE_LANGUAGE]: 'media.values.zh-hk',
  fr: 'media.values.fr',
  de: 'media.values.de',
  es: 'media.values.es',
  pt: 'media.values.pt',
  it: 'media.values.it',
  ru: 'media.values.ru',
  id: 'media.values.id',
  th: 'media.values.th',
  tv_short: 'media.values.tv_short',
  special: 'media.values.special',
  ova: 'media.values.ova',
  ona: 'media.values.ona',
  novel: 'media.values.novel',
  one_shot: 'media.values.one_shot',
  ebook: 'media.values.ebook',
  magazine: 'media.values.magazine',
  scripted: 'media.values.scripted',
  reality: 'media.values.reality',
  talk_show: 'media.values.talk_show',
  album: 'media.values.album',
  single: 'media.values.single',
  ep: 'media.values.ep',
  episodic: 'media.values.episodic',
  serial: 'media.values.serial',
  main_game: 'media.values.main_game',
  dlc: 'media.values.dlc',
  expansion: 'media.values.expansion',
  remake: 'media.values.remake',
  remaster: 'media.values.remaster',
  safe: 'media.values.safe',
  pg: 'media.values.pg',
  pg_13: 'media.values.pg_13',
  r: 'media.values.r',
  suggestive: 'media.values.suggestive',
  mature: 'media.values.mature',
  explicit: 'media.values.explicit',
  clean: 'media.values.clean',
  winter: 'media.values.winter',
  spring: 'media.values.spring',
  summer: 'media.values.summer',
  fall: 'media.values.fall',
  all: 'media.values.all',
  upcoming: 'media.values.upcoming',
  airing: 'media.values.airing',
  released: 'media.values.released',
  completed: 'media.values.completed',
  hiatus: 'media.values.hiatus',
  none: 'media.values.none',
  date: 'media.values.date',
  media_type: 'media.values.media_type',
  source: 'media.values.source',
} as const;
/** Canonical MediaType options from the dashboard contract. */
const MEDIATYPE = [
  'movie',
  'anime',
  'manga',
  'manhwa',
  'manhua',
  'light_novel',
  'book',
  'audiobook',
  'youtube',
  'tv',
  'podcast',
  'music',
  'game',
] as const;
/** Canonical MediaGenre options from the dashboard contract. */
const MEDIAGENRE = [
  'action',
  'adventure',
  'sci_fi',
  'fantasy',
  'romance',
  'thriller',
  'horror',
  'comedy',
  'drama',
  'mystery',
  'documentary',
  'animation',
  'slice_of_life',
  'history',
  'music',
  'sports',
  'non_fiction',
  'programming',
  'rpg',
  'strategy',
] as const;
/** Canonical MediaLanguage options from the dashboard contract. */
const MEDIALANGUAGE = ['en', 'vi', 'ja', 'ko', 'zh', 'zh-hk', 'fr', 'de', 'es', 'pt', 'it', 'ru', 'id', 'th'] as const;
/** Canonical MediaFormat options from the dashboard contract. */
const MEDIAFORMAT = [
  'tv',
  'tv_short',
  'movie',
  'special',
  'ova',
  'ona',
  'music',
  'manga',
  'manhwa',
  'manhua',
  'novel',
  'one_shot',
  'ebook',
  'audiobook',
  'magazine',
  'scripted',
  'animation',
  'reality',
  'talk_show',
  'documentary',
  'album',
  'single',
  'ep',
  'episodic',
  'serial',
  'main_game',
  'dlc',
  'expansion',
  'remake',
  'remaster',
] as const;
/** Canonical ContentRating options from the dashboard contract. */
const CONTENTRATING = ['safe', 'pg', 'pg_13', 'r', 'suggestive', 'mature', 'explicit', 'clean'] as const;
/** Canonical AnimeSeason options from the dashboard contract. */
const ANIMESEASON = ['winter', 'spring', 'summer', 'fall'] as const;
/** Canonical GadgetReleaseStatusFilter options from the dashboard contract. */
const GADGETRELEASESTATUSFILTER = ['all', 'upcoming', 'airing', 'released', 'completed', 'hiatus'] as const;
/** Canonical GadgetGroupBy options from the dashboard contract. */
const GADGETGROUPBY = ['none', 'date', 'media_type', 'source'] as const;
/** Literal labels for persisted media fields. */
const MEDIA_FIELD_KEY = {
  media_types: 'media.media_types',
  countries: 'media.countries',
  genres: 'media.genres',
  languages: 'media.languages',
  formats: 'media.formats',
  content_ratings: 'media.content_ratings',
  release_year: 'media.release_year',
  release_season: 'media.release_season',
  min_rating: 'media.min_rating',
  release_window_days: 'media.release_window_days',
  media_type: 'media.media_type',
  release_status: 'media.release_status',
  group_by: 'media.group_by',
} as const;

/** Optional numeric defaults keep blank inputs unset and display field-tagged API errors beside each control. */
function MediaNumberFields({ draft, set, facts }: DraftAccess & { facts: SectionFacts }) {
  const t = useTranslations('gadgetSettings');
  const numbers = [
    ['release_year', 'releaseYear', 1800, 2100, 1],
    ['min_rating', 'minRating', 0, 100, 'any'],
    ...(facts.fields.has('releaseWindow') ? [['release_window_days', 'releaseWindow', 1, 90, 1] as const] : []),
  ] as const;
  return (
    <>
      {numbers.map(([field, key, min, max, step]) => (
        <div key={field} data-field={field} className="space-y-1.5">
          <Label htmlFor={`${facts.nameId}-${field}`}>{t(MEDIA_FIELD_KEY[field])}</Label>
          <Input
            id={`${facts.nameId}-${field}`}
            type="number"
            min={min}
            max={max}
            step={step}
            value={draft[key]}
            onChange={(event) => set(key, event.target.value)}
            aria-invalid={!facts.mediaValid || Boolean(facts.fieldError(field))}
          />
          {facts.fieldError(field) && (
            <p role="alert" className="text-xs text-destructive">
              {facts.fieldError(field)}
            </p>
          )}
        </div>
      ))}
    </>
  );
}

/** Media defaults persisted on the definition; filter overrides remain distinct from source scope. */
export function MediaFiltersSection({ draft, set, facts }: DraftAccess & { facts: SectionFacts }) {
  const t = useTranslations('gadgetSettings');
  const locale = useLocale();
  const countryNames = new Intl.DisplayNames([locale], { type: 'region' });
  /** Labels canonical enums through static keys and ISO country codes through the active locale. */
  const label = (value: string) =>
    value in MEDIA_VALUE_KEY ? t(MEDIA_VALUE_KEY[value as keyof typeof MEDIA_VALUE_KEY]) : `${countryNames.of(value) ?? value} (${value})`;
  const lists = [
    ['media_types', MEDIATYPE],
    ['countries', ['JP', 'KR', 'CN', 'TW', 'HK', 'VN', 'US', 'GB', 'FR', 'DE', 'CA', 'AU', 'IN', 'IT', 'ES', 'BR', 'TH', 'ID']],
    ['genres', MEDIAGENRE],
    ['languages', MEDIALANGUAGE],
    ['formats', MEDIAFORMAT],
    ['content_ratings', CONTENTRATING],
  ] as const;
  return (
    <section className="space-y-3">
      {lists.map(([field, options]) => (
        <div key={field} data-field={field} className="space-y-1.5">
          <Label id={`${facts.nameId}-${field}-label`}>{t(MEDIA_FIELD_KEY[field])}</Label>
          <MediaMultiChipPicker
            maxItems={MEDIA_SELECTOR_LIMITS[field]}
            value={draft.mediaScope[field] ?? []}
            options={options}
            label={label}
            country={field === 'countries'}
            labelId={`${facts.nameId}-${field}-label`}
            onChange={(next) => set('mediaScope', (scope) => ({ ...scope, [field]: next }) as GadgetScope)}
          />
          {facts.fieldError(field) && (
            <p role="alert" className="text-xs text-destructive">
              {facts.fieldError(field)}
            </p>
          )}
        </div>
      ))}
      <MediaNumberFields draft={draft} set={set} facts={facts} />
      {!facts.mediaValid && (
        <p role="alert" className="text-xs text-destructive">
          {t('media.invalid')}
        </p>
      )}
      {(
        [
          ['release_season', ANIMESEASON],
          ['media_type', MEDIATYPE],
          ['release_status', GADGETRELEASESTATUSFILTER],
          ['group_by', GADGETGROUPBY],
        ] as const
      ).map(([field, options]) => {
        const value = field === 'release_season' ? draft.mediaScope.release_season : draft.mediaFilters[field];
        return (
          <div key={field} data-field={field} className="space-y-1.5">
            <Label htmlFor={`${facts.nameId}-${field}`}>{t(MEDIA_FIELD_KEY[field])}</Label>
            <Select
              value={value ?? '__unset'}
              onValueChange={(next) => {
                const patch = { [field]: next === '__unset' ? null : next };
                if (field === 'release_season') set('mediaScope', (scope) => ({ ...scope, ...patch }) as GadgetScope);
                else set('mediaFilters', (filters) => ({ ...filters, ...patch }));
              }}
            >
              <SelectTrigger id={`${facts.nameId}-${field}`}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__unset">{t('media.any')}</SelectItem>
                {options.map((item) => (
                  <SelectItem key={item} value={item}>
                    {label(item)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {facts.fieldError(field) && (
              <p role="alert" className="text-xs text-destructive">
                {facts.fieldError(field)}
              </p>
            )}
          </div>
        );
      })}
    </section>
  );
}
