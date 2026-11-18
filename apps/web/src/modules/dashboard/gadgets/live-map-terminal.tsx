'use client';

import { ChevronDown, ExternalLink, RefreshCw, Settings } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { useMemo, type Dispatch, type ReactNode, type SetStateAction } from 'react';
import { Button } from '@/components/ui/button';
import { DropdownMenu, DropdownMenuCheckboxItem, DropdownMenuContent, DropdownMenuTrigger } from '@/components/ui/dropdown-menu';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { normalizeFormattingLocale } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { LIVE_TAG_KEY, LIVE_TAGS } from '../live-feed/sources';
import type { StoredEvent } from '../live-feed/store';
import { safeLiveLink } from '../live-map-data';
import { legendTone } from './live-map-legend';

/**
 * Event kind shown in the terminal event rows: the first known category tag of the event (upper-cased by CSS),
 * else its provider id, so every row carries a short coloured label.
 */
export function eventKind(event: Pick<StoredEvent, 'tags' | 'provider'>): { label: string; tone: string } {
  const tag = LIVE_TAGS.find((candidate) => event.tags.includes(candidate));
  return { label: tag ?? event.provider, tone: tag ? legendTone(tag) : 'var(--muted)' };
}

/**
 * Terminal title bar of the overview map pane: status dot, "── live map ──" title, muted meta (event count and
 * range) and the pane actions (engine segment and collapse control from `actions`, refresh, sources).
 *
 * @param props.count Events currently shown; `props.days` is the active range in days.
 * @param props.refreshing Disables the refresh icon while a provider request is in flight.
 */
export function LiveMapTitleBar({
  count,
  days,
  actions,
  refreshing,
  onRefresh,
  onOpenSources,
}: {
  count: number;
  days: string;
  actions?: ReactNode;
  refreshing: boolean;
  onRefresh: () => void;
  onOpenSources?: () => void;
}) {
  const t = useTranslations('dashboard');
  return (
    <header className="live-map-titlebar gadget-header">
      <span className="gadget-dot rounded-full" aria-hidden="true" />
      <h3 className="gadget-title">{t('liveMap.titleBar')}</h3>
      <span className="gadget-meta">{t('liveMap.titleMeta', { count, days })}</span>
      <div className="live-map-titlebar-actions">
        {actions}
        <Button type="button" variant="ghost" size="icon" aria-label={t('liveMap.refresh')} disabled={refreshing} onClick={onRefresh}>
          <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
        </Button>
        {onOpenSources && (
          <Button type="button" variant="ghost" size="icon" aria-label={t('liveMap.sources')} onClick={onOpenSources}>
            <Settings className="h-3.5 w-3.5" aria-hidden="true" />
          </Button>
        )}
      </div>
    </header>
  );
}

/**
 * Terminal filter row: text search, a multi-select category dropdown (same toggles as the default chip row, "All"
 * resets) and the range select. Equivalent state to the default toolbar; only the presentation is compact.
 *
 * @param props.tags Active category tags (empty means all); `props.count` is the filtered event count.
 */
export function LiveMapTerminalFilters({
  tags,
  onTags,
  query,
  onQuery,
  days,
  onDays,
  count,
}: {
  tags: string[];
  onTags: Dispatch<SetStateAction<string[]>>;
  query: string;
  onQuery: (value: string) => void;
  days: string;
  onDays: (value: string) => void;
  count: number;
}) {
  const t = useTranslations('dashboard');
  const label =
    tags.length === 0 ? t('liveMap.categoryAll') : tags.map((tag) => t(LIVE_TAG_KEY[tag as keyof typeof LIVE_TAG_KEY])).join(', ');
  return (
    <div className="live-map-filters">
      <div className="live-map-prompt">
        <span aria-hidden="true">&gt;</span>
        <Input
          type="search"
          value={query}
          onChange={(event) => onQuery(event.target.value)}
          aria-label={t('liveMap.search')}
          placeholder={t('liveMap.filterEvents')}
        />
      </div>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button type="button" className="live-map-cat" aria-label={t('liveMap.category')}>
            <span className="truncate">
              {label} · {count}
            </span>
            <ChevronDown className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="max-h-72 overflow-y-auto">
          <DropdownMenuCheckboxItem
            checked={tags.length === 0}
            onSelect={(event) => event.preventDefault()}
            onCheckedChange={() => onTags([])}
          >
            {t('liveMap.categoryAll')}
          </DropdownMenuCheckboxItem>
          {LIVE_TAGS.map((tag) => (
            <DropdownMenuCheckboxItem
              key={tag}
              checked={tags.includes(tag)}
              onSelect={(event) => event.preventDefault()}
              onCheckedChange={() =>
                onTags((current) => (current.includes(tag) ? current.filter((item) => item !== tag) : [...current, tag]))
              }
            >
              {t(LIVE_TAG_KEY[tag])}
            </DropdownMenuCheckboxItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
      <Select value={days} onValueChange={onDays}>
        <SelectTrigger aria-label={t('liveMap.period')}>
          <SelectValue>{t('liveMap.rangeShort', { days: Number(days) })}</SelectValue>
        </SelectTrigger>
        <SelectContent>
          {['1', '7', '30'].map((value) => (
            <SelectItem key={value} value={value}>
              {t('map.range', { days: Number(value) })}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

/**
 * One terminal event row `HH:MM  KIND  title  #tag`: the title button selects the event on the map, and the
 * provider links are icon-only anchors (accessible names, no visible "Open event" text).
 *
 * @param props.onSelect Called with the event key when the title is pressed.
 */
export function TerminalEventRow({ event, onSelect }: { event: StoredEvent; onSelect: (key: string) => void }) {
  const t = useTranslations('dashboard');
  const display = useDisplayPreferences();
  const clock = useMemo(
    () =>
      new Intl.DateTimeFormat(normalizeFormattingLocale(display.locale), {
        hour: '2-digit',
        minute: '2-digit',
        hourCycle: 'h23',
        timeZone: display.timezone,
      }),
    [display.locale, display.timezone],
  );
  const kind = eventKind(event);
  const href = safeLiveLink(event.url ?? null);
  const time = event.observed_at && Number.isFinite(Date.parse(event.observed_at)) ? event.observed_at : null;
  return (
    <li className="live-event-row live-event-row-term">
      {time ? <time dateTime={time}>{clock.format(new Date(time))}</time> : <span>{t('liveMap.timeUnknown')}</span>}
      <span className="live-event-kind" style={{ color: kind.tone }}>
        {kind.label}
      </span>
      <button type="button" className="live-event-title" title={event.title} onClick={() => onSelect(event.key)}>
        {event.title}
      </button>
      <span className="live-event-source">{event.source}</span>
      <span className="live-event-links">
        {href && (
          <a href={href} target="_blank" rel="noreferrer" aria-label={t('liveMap.openEvent')} title={t('liveMap.openEvent')}>
            <ExternalLink className="h-3 w-3" aria-hidden="true" />
          </a>
        )}
        {event.sources?.map(
          ({ url }) =>
            safeLiveLink(url) && (
              <a key={url} href={safeLiveLink(url) ?? undefined} target="_blank" rel="noreferrer" aria-label={t('liveMap.providerSource')}>
                <ExternalLink className="h-3 w-3" aria-hidden="true" />
              </a>
            ),
        )}
      </span>
    </li>
  );
}
