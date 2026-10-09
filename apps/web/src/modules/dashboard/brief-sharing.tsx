'use client';

import { useQuery } from '@tanstack/react-query';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { ApiError } from '@/core/api';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { useWorkspace } from '@/core/workspace-context';
import { getDocument, grantShare, type ShareRow, type WorkspaceMember } from '@/modules/knowledge/api';
import { ShareDialog, shareErrorKey } from '@/modules/knowledge/document-sharing';
import { BriefText } from './brief-text';
import { dailyKeys, listBriefRevisions, type DailyBriefRevision } from './daily-api';

/** Raised when a brief share is blocked until the listed owner-visible documents are shared. */
export class EvidenceNotSharedError extends Error {
  constructor(readonly documentIds: string[]) { super('brief_evidence_not_shared'); }
}

function EvidenceLink({ id }: { id: string }) {
  const doc = useQuery({ queryKey: ['documents', id], queryFn: () => getDocument(id) });
  return <li><Link className="underline" href={`/knowledge/documents/${id}`}>{doc.data?.title ?? id}</Link></li>;
}

/** Owner-only share control for one saved brief revision. */
export function BriefShareButton({ brief }: { brief: Pick<DailyBriefRevision, 'id' | 'revision'> }) {
  const t = useTranslations('sharing');
  const { selection } = useWorkspace();
  const workspaceId = selection?.id ?? '';
  const grant = async (member: WorkspaceMember, revision: number, csrfToken: string): Promise<ShareRow> => {
    try {
      return await grantShare(workspaceId, 'brief', brief.id, member, revision, csrfToken);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409 && error.code === 'brief_evidence_not_shared') {
        const ids = error.payload.document_ids;
        throw new EvidenceNotSharedError(Array.isArray(ids) ? ids.filter((id): id is string => typeof id === 'string') : []);
      }
      throw error;
    }
  };
  return (
    <ShareDialog
      resourceType="brief" resourceId={brief.id} resourceRevision={brief.revision}
      semantics={t('semanticsBrief', { revision: brief.revision })} grant={grant}
      renderError={(error) => error instanceof EvidenceNotSharedError ? (
        <div role="alert" className="space-y-1 text-sm">
          <p>{t('evidenceNotShared')}</p>
          {error.documentIds.length > 0 && <ul className="list-disc pl-5">{error.documentIds.map((id) => <EvidenceLink key={id} id={id} />)}</ul>}
        </div>
      ) : null}
    />
  );
}

/** Member view: the briefs the owner shared for the selected day. */
export function SharedBriefs({ date, timezone }: { date: string; timezone: string }) {
  const t = useTranslations('sharing');
  const display = useDisplayPreferences();
  const briefs = useQuery({ queryKey: dailyKeys.briefs(date, timezone), queryFn: ({ signal }) => listBriefRevisions(date, timezone, signal) });
  return (
    <section aria-labelledby="shared-briefs-heading" className="space-y-2">
      <h3 id="shared-briefs-heading" className="text-sm font-semibold">{t('sharedBriefs')}</h3>
      {briefs.isLoading && <p role="status" className="text-sm text-muted-foreground">{t('loading')}</p>}
      {briefs.isError && <p role="alert" className="text-sm text-destructive">{t(shareErrorKey(briefs.error))}</p>}
      {briefs.data?.length === 0 && <p className="text-sm text-muted-foreground">{t('noSharedBriefs')}</p>}
      {briefs.data?.map((brief) => (
        <article key={brief.id} className="space-y-1 border-b border-border pb-2">
          <p className="text-xs text-muted-foreground">{t('briefMeta', { revision: brief.revision, time: formatDateTime(brief.generated_at, display.locale, display.timezone) })}</p>
          <BriefText brief={brief} />
        </article>
      ))}
    </section>
  );
}
