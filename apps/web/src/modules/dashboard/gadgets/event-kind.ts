/** Tone of one terminal event kind: a semantic colour token (danger, warning, success, info or muted). */
const TONE = {
  danger: 'var(--danger)',
  warn: 'var(--status-warning)',
  ok: 'var(--status-success)',
  info: 'var(--info, var(--accent))',
  muted: 'var(--muted)',
} as const;

/** Kind label and tone for a tag (provider-native or category) in precedence order; the first matching tag wins. */
const KIND_BY_TAG: Record<string, { label: string; tone: keyof typeof TONE }> = {
  earthquake: { label: 'QUAKE', tone: 'danger' },
  eq: { label: 'QUAKE', tone: 'danger' },
  volcanoes: { label: 'VOLC', tone: 'danger' },
  vo: { label: 'VOLC', tone: 'danger' },
  wildfires: { label: 'FIRE', tone: 'warn' },
  wf: { label: 'FIRE', tone: 'warn' },
  severestorms: { label: 'STORM', tone: 'info' },
  tc: { label: 'STORM', tone: 'info' },
  floods: { label: 'FLOOD', tone: 'info' },
  fl: { label: 'FLOOD', tone: 'info' },
  launch: { label: 'LAUNCH', tone: 'ok' },
  cve: { label: 'CVE', tone: 'danger' },
};

/** Kind label and tone for providers whose events carry no distinguishing tag. */
const KIND_BY_PROVIDER: Record<string, { label: string; tone: keyof typeof TONE }> = {
  usgs: { label: 'QUAKE', tone: 'danger' },
  firms: { label: 'FIRE', tone: 'warn' },
  launches: { label: 'LAUNCH', tone: 'ok' },
  nws_alerts: { label: 'STORM', tone: 'info' },
  nvd: { label: 'CVE', tone: 'danger' },
  cisa_kev: { label: 'CVE', tone: 'danger' },
  coingecko: { label: 'CRYPTO', tone: 'info' },
  fear_greed: { label: 'CRYPTO', tone: 'info' },
  github_releases: { label: 'REL', tone: 'ok' },
  freetogame: { label: 'GAME', tone: 'info' },
  gamerpower: { label: 'GAME', tone: 'info' },
  hackernews: { label: 'NEWS', tone: 'info' },
  devto: { label: 'NEWS', tone: 'info' },
  lobsters: { label: 'NEWS', tone: 'info' },
  business: { label: 'NEWS', tone: 'info' },
  fed: { label: 'NEWS', tone: 'info' },
  sec: { label: 'NEWS', tone: 'info' },
  bbc_world: { label: 'NEWS', tone: 'info' },
  aljazeera: { label: 'NEWS', tone: 'info' },
  vnexpress_world: { label: 'NEWS', tone: 'info' },
  vnexpress_news: { label: 'NEWS', tone: 'info' },
  gaming_news: { label: 'NEWS', tone: 'info' },
};

/** Longest label shown in the terminal KIND column. */
const KIND_MAX = 6;

/**
 * Short terminal KIND label and colour token of an event, derived from its real fields: a specific tag first
 * (earthquake, EONET / GDACS types, launch, cve), then its provider id (usgs, firms, launches, news sources), and
 * finally its first tag or provider id upper-cased and cut to six characters, in the muted tone.
 *
 * Pure; no I/O. Tags are expected lower-case (the feed store lower-cases them).
 *
 * @param event The stored event's `tags` and `provider` id.
 * @returns A label of at most six characters and a CSS colour value.
 */
export function eventKind(event: { tags: string[]; provider: string }): { label: string; tone: string } {
  const tag = event.tags.find((candidate) => KIND_BY_TAG[candidate]);
  const known = (tag && KIND_BY_TAG[tag]) || KIND_BY_PROVIDER[event.provider];
  if (known) return { label: known.label, tone: TONE[known.tone] };
  return { label: (event.tags[0] ?? event.provider).toUpperCase().slice(0, KIND_MAX), tone: TONE.muted };
}
