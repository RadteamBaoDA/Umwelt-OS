'use client';

import { useMemo, useState } from 'react';
import { useContentTranslation } from '@/modules/translations/use-content-translation';
import { TranslatedBadge } from '@/modules/translations/translated-badge';
import type { DailyBriefRevision } from './daily-api';

/** Brief text with the automatic translation of this saved revision; the original is shown first. */
export function BriefText({ brief }: { brief: DailyBriefRevision }) {
  const [showOriginal, setShowOriginal] = useState(false);
  const targets = useMemo(() => [{ id: brief.id, revision: String(brief.revision) }], [brief.id, brief.revision]);
  const translated = useContentTranslation('daily_brief', targets).results.get(brief.id)?.translation?.content;
  return (
    <>
      <p className="whitespace-pre-wrap text-sm leading-relaxed">{translated && !showOriginal ? translated : brief.content}</p>
      {translated && <TranslatedBadge showOriginal={showOriginal} onToggle={() => setShowOriginal((v) => !v)} />}
    </>
  );
}
