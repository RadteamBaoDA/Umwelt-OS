import { useLocale, useTranslations } from 'next-intl';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { MediaMultiChipPicker } from './gadget-editor-pickers';
import { MEDIA_SELECTOR_LIMITS } from './gadget-editor-model';
import type { DraftAccess, SectionFacts } from './gadget-editor-sections';
import type { GadgetScope } from './api';

import {
  MEDIACOUNTRY,
  MEDIA_VALUE_KEY,
  MEDIATYPE,
  MEDIAGENRE,
  MEDIALANGUAGE,
  MEDIAFORMAT,
  CONTENTRATING,
  ANIMESEASON,
  GADGETRELEASESTATUSFILTER,
  GADGETGROUPBY,
} from '@/core/media-options';

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
    Object.hasOwn(MEDIA_VALUE_KEY, value)
      ? t(MEDIA_VALUE_KEY[value as keyof typeof MEDIA_VALUE_KEY])
      : `${countryNames.of(value) ?? value} (${value})`;
  const lists = [
    ['media_types', MEDIATYPE],
    ['countries', MEDIACOUNTRY],
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
