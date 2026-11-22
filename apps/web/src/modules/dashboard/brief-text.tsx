'use client';

import { useMemo, useState } from 'react';
import { useTranslations } from 'next-intl';
import { TranslateActionButton, TranslatedBadge, TranslationNote, useContentTranslation } from '@/modules/translations';
import type { DailyBriefRevision } from './daily-api';
import { parseBriefParagraphs } from './brief-paragraphs';

/**
 * Reading view of a brief: one paragraph per blank-line block, a bold lead label and `[n]` markers as
 * links to the matching entry of the citation list (`#brief-ref-n`, see daily-brief.tsx). Same text as the plain view.
 */
function BriefParagraphs({ text }: { text: string }) {
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

/**
 * Brief text with on-demand translation of this saved revision. The original is always shown first;
 * when workspace translation is enabled, a Translate button sits above the body so users can translate
 * without scrolling to the bottom, and switches to a toggle back to the original once ready.
 *
 * @param props.brief Saved daily brief revision whose markdown content can be translated on demand.
 */
export function BriefText({ brief }: { brief: DailyBriefRevision }) {
  const t = useTranslations('translations');
  const [showOriginal, setShowOriginal] = useState(false);
  const targets = useMemo(() => [{ id: brief.id, revision: String(brief.revision) }], [brief.id, brief.revision]);
  const translation = useContentTranslation('daily_brief', targets);
  const translated = translation.results.get(brief.id)?.translation?.content;
  const issue = translation.issues.get(brief.id);
  const pending = translation.pendingIds.has(brief.id);
  const languageLabel = translation.targetLanguage === 'vi' ? t('languageVi') : t('languageEn');
  return (
    <>
      {translation.enabled ? (
        <div className="flex flex-wrap items-center gap-2">
          {translated ? (
            <TranslatedBadge
              showOriginal={showOriginal}
              targetLanguage={translation.targetLanguage}
              onToggle={() => setShowOriginal((v) => !v)}
            />
          ) : (
            <TranslateActionButton
              label={t('translateTo', { language: languageLabel })}
              pending={pending}
              onClick={() => {
                setShowOriginal(false);
                translation.translate(targets);
              }}
            />
          )}
          {!translated && !pending ? <TranslationNote issue={issue} /> : null}
        </div>
      ) : null}
      <BriefParagraphs text={translated && !showOriginal ? translated : brief.content} />
    </>
  );
}
