import { Badge } from '@/components/ui/badge';
import type { AppLocaleId } from '@/core/i18n';
import { genUiCopy } from '../copy';

/**
 * Badge marking a widget whose data is illustrative rather than retrieved from the user's sources.
 *
 * @param props.locale - Optional widget locale (falls back to document language).
 * @returns Outline "Sample data" badge using semantic tokens.
 */
export function SampleDataBadge({ locale }: { locale?: AppLocaleId }) {
  return (
    <Badge variant="outline" data-testid="sample-data-badge" className="text-xs">
      {genUiCopy(locale).genUiSampleData}
    </Badge>
  );
}
