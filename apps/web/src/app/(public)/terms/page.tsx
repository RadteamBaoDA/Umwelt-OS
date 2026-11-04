import { loadPolicy } from '@/modules/legal/policy-loader';
import { PolicyDocument } from '@/modules/legal/policy-document';

/** Serves public terms in both supported languages without reading account content. */
export default async function TermsPage({ searchParams }: { searchParams: Promise<{ lang?: string }> }) {
  const { lang } = await searchParams;
  const language = lang === 'en-us' || lang === 'vi-vi' ? lang : undefined;
  const [english, vietnamese, inventory] = await Promise.all([loadPolicy('terms', 'en-us'), loadPolicy('terms', 'vi-vi'), loadPolicy('inventory', 'en-us')]);
  return <PolicyDocument name="terms" language={language} documents={{ 'en-us': english, 'vi-vi': vietnamese }} inventory={inventory} />;
}
