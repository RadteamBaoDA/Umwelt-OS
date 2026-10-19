import { loadPolicy } from '@/modules/legal/policy-loader';
import { PolicyDocument } from '@/modules/legal/policy-document';

/** Serves public privacy in both supported languages without reading account content. */
export default async function PrivacyPage({ searchParams }: { searchParams: Promise<{ lang?: string }> }) {
  const { lang } = await searchParams;
  const language = lang === 'en-us' || lang === 'vi-vi' ? lang : undefined;
  const [english, vietnamese, inventory] = await Promise.all([loadPolicy('privacy', 'en-us'), loadPolicy('privacy', 'vi-vi'), loadPolicy('inventory', 'en-us')]);
  return <PolicyDocument name="privacy" language={language} documents={{ 'en-us': english, 'vi-vi': vietnamese }} inventory={inventory} />;
}
