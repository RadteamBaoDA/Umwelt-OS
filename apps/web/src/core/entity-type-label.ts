import type { useTranslations } from 'next-intl';

/** Translator bound to the `entities` namespace. */
export type EntitiesT = ReturnType<typeof useTranslations<'entities'>>;

/** Literal catalog key per entity type the API can return (no runtime key building). */
const ENTITY_TYPE_KEYS = {
  person: 'type.person',
  organization: 'type.organization',
  company: 'type.company',
  project: 'type.project',
  repository: 'type.repository',
  place: 'type.place',
  country: 'type.country',
  product: 'type.product',
  topic: 'type.topic',
  technology: 'type.technology',
  asset: 'type.asset',
  device: 'type.device',
  website: 'type.website',
  event_subject: 'type.eventSubject',
  other: 'type.other',
} as const;

/**
 * Localized label of an entity type.
 * @param t Translator for the `entities` namespace.
 * @param type Entity type string from the API; unknown types are returned unchanged so new server types stay readable.
 */
export function entityTypeLabel(t: EntitiesT, type: string): string {
  return Object.hasOwn(ENTITY_TYPE_KEYS, type) ? t(ENTITY_TYPE_KEYS[type as keyof typeof ENTITY_TYPE_KEYS]) : type;
}
