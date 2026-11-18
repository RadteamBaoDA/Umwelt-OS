import { Badge } from '@/components/ui/badge';

/** Required attribution per provider id: brand label (not translated) and the credit page it links to. */
const ATTRIBUTION_BADGES = {
  tmdb_movies: { label: 'TMDB', href: 'https://www.themoviedb.org' },
  tmdb_tv: { label: 'TMDB', href: 'https://www.themoviedb.org' },
  tvmaze_schedule: { label: 'TVMaze · CC BY-SA 4.0', href: 'https://www.tvmaze.com' },
  mangadex_releases: { label: 'MangaDex', href: 'https://mangadex.org' },
  mangaupdates_releases: { label: 'MangaUpdates', href: 'https://www.mangaupdates.com' },
} as const;

/**
 * Renders the mandatory attribution badge for a provider id as an external link (new tab, noopener), or as plain
 * text when `link` is false (inside buttons, where a nested anchor would be invalid). Renders nothing for
 * providers without a required badge. Pure and side-effect free.
 */
export function AttributionBadge({ providerId, link = true }: { providerId: string; link?: boolean }) {
  if (!Object.hasOwn(ATTRIBUTION_BADGES, providerId)) return null;
  const { label, href } = ATTRIBUTION_BADGES[providerId as keyof typeof ATTRIBUTION_BADGES];
  if (!link)
    return (
      <Badge variant="outline" data-testid="attribution-badge">
        {label}
      </Badge>
    );
  return (
    <Badge asChild variant="outline" data-testid="attribution-badge">
      <a href={href} target="_blank" rel="noopener noreferrer">
        {label}
      </a>
    </Badge>
  );
}
