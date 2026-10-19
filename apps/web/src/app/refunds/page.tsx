import { loadPolicy } from '@/modules/legal/policy-loader';
import { PolicyDocument } from '@/modules/legal/policy-document';

/** Serves public refunds in both supported languages without reading account content. */
export default async function RefundsPage({ searchParams }: { searchParams: Promise<{ lang?: string }> }) {
  const { lang } = await searchParams;
  const language = lang === 'en-us' || lang === 'vi-vi' ? lang : undefined;
  const [english, vietnamese, inventory] = await Promise.all([loadPolicy('refunds', 'en-us'), loadPolicy('refunds', 'vi-vi'), loadPolicy('inventory', 'en-us')]);
  return <PolicyDocument name="refunds" language={language} documents={{ 'en-us': english, 'vi-vi': vietnamese }} inventory={inventory} />;
}
