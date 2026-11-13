import { getRequestConfig } from 'next-intl/server';
import { missingKeyHandlers } from './i18n-missing';
import { messages } from './messages';

/** Server request config; adds the e2e-only missing-key logger (see `i18n-missing.ts`), otherwise next-intl defaults. */
export default getRequestConfig(async () => ({
  locale: 'en-US',
  messages: messages['en-us'],
  ...missingKeyHandlers(),
}));
