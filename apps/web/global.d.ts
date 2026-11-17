import type { jsonCatalogs } from '@locales';

// Types next-intl's `useTranslations`/`getTranslations` keys: every namespace is checked down to the nested key
// (`t('group.key')`) against the en-us catalogs built from locales/en-us/*.json.
declare module 'next-intl' {
  interface AppConfig {
    Messages: (typeof jsonCatalogs)['en-us'];
  }
}
