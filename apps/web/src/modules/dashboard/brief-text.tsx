'use client';

import { useMemo, useState } from 'react';
import { useContentTranslation } from '@/modules/translations';
import { TranslatedBadge } from '@/modules/translations';
import { TranslationNote } from '@/modules/translations';
import { useSkin } from '@/core/use-skin';
import type { DailyBriefRevision } from './daily-api';
import { parseBriefParagraphs } from './brief-paragraphs';

/**
 * Terminal-skin reading view of a brief: one paragraph per blank-line block, a bold lead label and `[n]` markers as
 * links to the matching entry of the citation list (`#brief-ref-n`, see daily-brief.tsx). Same text as the plain view.
 */
function TerminalBriefText({ text }: { text: string }) {
  return (
    <div className="brief-term">
      {parseBriefParagraphs(text).map((paragraph, index) => (
        <p key={index} className="brief-term-p">
          {paragraph.lead && <b>{paragraph.lead} </b>}
          {paragraph.parts.map((part, i) =>
            part.kind === 'cite' ? (
              <a key={i} className="brief-cite" href={`#brief-ref-${part.ref}`}>
                [{part.ref}]
              </a>
            ) : (
              <span key={i}>{part.value}</span>
            ),
          )}
        </p>
      ))}
    </div>
  );
}

/** Brief text with the automatic translation of this saved revision; the original is shown first. The terminal skin renders paragraphs with a bold lead and linked citations. */
export function BriefText({ brief }: { brief: DailyBriefRevision }) {
  const terminal = useSkin() === 'terminal';
  const [showOriginal, setShowOriginal] = useState(false);
  const targets = useMemo(() => [{ id: brief.id, revision: String(brief.revision) }], [brief.id, brief.revision]);
  const translation = useContentTranslation('daily_brief', targets);
  const translated = translation.results.get(brief.id)?.translation?.content;
  return (
    <>
      {terminal ? (
        <TerminalBriefText text={translated && !showOriginal ? translated : brief.content} />
      ) : (
        <p className="whitespace-pre-wrap text-sm leading-relaxed">{translated && !showOriginal ? translated : brief.content}</p>
      )}
      {!translated && <TranslationNote issue={translation.issues.get(brief.id)} />}
      {translated && <TranslatedBadge showOriginal={showOriginal} onToggle={() => setShowOriginal((v) => !v)} />}
    </>
  );
}
