'use client';

import { TablePagination, usePagination } from '@/components/ui/pagination';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { apiRequest, csrfHeaders } from '@/core/api';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { normalizeFormattingLocale } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { safeHttpUrl } from '@/core/safe-url';
import { connectorKeys, getConnectorCatalog } from '@/modules/sources/api';

export type PendingReview = {
  workspace_id: string;
  workspace_name: string;
  source_id: string;
  provider_id: string;
  terms_url: string;
  terms_version: string;
  declared_use: string;
  operator_review_state: string;
};
type ReviewDecision = {
  row: PendingReview;
  state: 'approved' | 'rejected';
  allowed_use?: 'personal' | 'noncommercial' | 'commercial';
  evidence_ref: string;
};

/** Pending provider-terms reviews as a table; each row's Review button opens a sheet holding the decision form. */
export function ProviderReviewTable({ rows }: { rows: PendingReview[] }) {
  const t = useTranslations('globalSettings');
  const display = useDisplayPreferences();
  const catalog = useQuery({ queryKey: connectorKeys.catalog, queryFn: ({ signal }) => getConnectorCatalog(signal), staleTime: 5 * 60_000 });
  const pager = usePagination(rows);
  return <><Table>
    <TableHeader><TableRow>
      <TableHead>{t('provider')}</TableHead><TableHead>{t('workspace')}</TableHead><TableHead>{t('termsVersion')}</TableHead><TableHead>{t('declaredUse')}</TableHead><TableHead><span className="sr-only">{t('review')}</span></TableHead>
    </TableRow></TableHeader>
    <TableBody>{pager.pageItems.map((row) => <ProviderReviewTableRow key={`${row.workspace_id}-${row.source_id}`} row={row} locale={display.locale} providerName={catalog.data?.find((entry) => entry.provider_id === row.provider_id)?.label ?? row.provider_id} />)}</TableBody>
  </Table><TablePagination {...pager} /></>;
}

/** One pending review row; owns the open state of its decision sheet. */
export function ProviderReviewTableRow({ row, locale, providerName }: { row: PendingReview; locale: string; providerName: string }) {
  const t = useTranslations('globalSettings');
  const [open, setOpen] = useState(false);
  const termsUrl = safeHttpUrl(row.terms_url);
  const version = /^\d{4}-\d{2}-\d{2}$/.test(row.terms_version)
    ? new Intl.DateTimeFormat(normalizeFormattingLocale(locale as never), { dateStyle: 'medium', timeZone: 'UTC' }).format(new Date(row.terms_version))
    : row.terms_version;
  const use = row.declared_use === 'personal' || row.declared_use === 'noncommercial' || row.declared_use === 'commercial' ? t(`use.${row.declared_use}`) : row.declared_use;
  return <TableRow>
    <TableCell className="font-medium">{providerName}{termsUrl && <> <a href={termsUrl} target="_blank" rel="noopener noreferrer" className="font-normal text-primary underline underline-offset-4">{t('termsLink')}</a></>}</TableCell>
    <TableCell>{row.workspace_name}</TableCell>
    <TableCell className="tabular-nums">{version}</TableCell>
    <TableCell>{use}</TableCell>
    <TableCell className="text-right"><Button type="button" variant="outline" onClick={() => setOpen(true)}>{t('review')}</Button></TableCell>
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetContent>
        <SheetHeader><SheetTitle>{t('reviewTitle')}</SheetTitle><SheetDescription>{providerName} · {row.workspace_name}</SheetDescription></SheetHeader>
        <div className="px-4 pb-4"><ProviderReviewRow row={row} onDone={() => setOpen(false)} /></div>
      </SheetContent>
    </Sheet>
  </TableRow>;
}

/** Records an operator decision with its exact terms version and required evidence reference; `onDone` runs after a saved decision. */
function ProviderReviewRow({ row, onDone }: { row: PendingReview; onDone?: () => void }) {
  const t = useTranslations('globalSettings');
  const session = useWorkspaceSession();
  const client = useQueryClient();
  const [evidence, setEvidence] = useState('');
  const display = useDisplayPreferences();
  const catalog = useQuery({ queryKey: connectorKeys.catalog, queryFn: ({ signal }) => getConnectorCatalog(signal), staleTime: 5 * 60_000 });
  // Falls back to the raw provider id when the catalog has no entry, so unknown providers stay identifiable.
  const providerName = catalog.data?.find((entry) => entry.provider_id === row.provider_id)?.label ?? row.provider_id;
  // Terms versions are ISO dates (UTC midnight, so no zone shift); any other format is shown verbatim rather than guessed at.
  const termsVersion = /^\d{4}-\d{2}-\d{2}$/.test(row.terms_version)
    ? new Intl.DateTimeFormat(normalizeFormattingLocale(display.locale), { dateStyle: 'medium', timeZone: 'UTC' }).format(new Date(row.terms_version))
    : row.terms_version;
  const useLabel = (value: string) => (value === 'personal' || value === 'noncommercial' || value === 'commercial' ? t(`use.${value}`) : value);
  const [allowedUse, setAllowedUse] = useState<ReviewDecision['allowed_use'] | ''>(
    row.declared_use === 'personal' || row.declared_use === 'noncommercial' || row.declared_use === 'commercial' ? row.declared_use : '',
  );
  const mutation = useMutation({
    mutationFn: ({ state, allowed_use, evidence_ref }: Omit<ReviewDecision, 'row'>) => apiRequest(
      `/api/v1/operator/connectors/${encodeURIComponent(row.workspace_id)}/${encodeURIComponent(row.source_id)}/terms-review`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', ...csrfHeaders(session.csrfToken) },
        body: JSON.stringify({ state, ...(allowed_use ? { allowed_use } : {}), evidence_ref, reviewed_terms_version: row.terms_version }),
      },
    ),
    onSuccess: () => { onDone?.(); return client.invalidateQueries({ queryKey: ['operator-provider-terms-review'] }); },
  });

  /** Submits one decision only when the required evidence and approval scope are present. */
  const submit = (event: { preventDefault(): void }, state: ReviewDecision['state']) => {
    event.preventDefault();
    const evidence_ref = evidence.trim();
    if (!evidence_ref || (state === 'approved' && !allowedUse)) return;
    mutation.mutate({ state, ...(state === 'approved' && allowedUse ? { allowed_use: allowedUse } : {}), evidence_ref });
  };

  return <article className="flex flex-col gap-3 border-b border-border pb-4 last:border-0 last:pb-0">
    <dl className="m-0 grid grid-cols-1 gap-2 text-sm sm:grid-cols-2">
      <div><dt className="text-muted-foreground">{t('workspaces')}</dt><dd className="m-0 font-medium">{row.workspace_name}</dd></div>
      <div><dt className="text-muted-foreground">{t('provider')}</dt><dd className="m-0 flex flex-wrap items-center gap-2 font-medium">{providerName}{safeHttpUrl(row.terms_url) && <a href={safeHttpUrl(row.terms_url)} target="_blank" rel="noopener noreferrer" className="font-normal text-primary underline underline-offset-4">{t('termsLink')}</a>}</dd></div>
      <div><dt className="text-muted-foreground">{t('termsVersion')}</dt><dd className="m-0">{termsVersion}</dd></div>
      <div><dt className="text-muted-foreground">{t('declaredUse')}</dt><dd className="m-0">{useLabel(row.declared_use)}</dd></div>
    </dl>
    <form className="flex flex-col gap-3" onSubmit={(event) => submit(event, 'approved')}>
      <div className="grid gap-2 sm:grid-cols-2">
        <div className="flex flex-col gap-2">
          <Label htmlFor={`evidence-${row.source_id}`}>{t('evidence')}</Label>
          <Input id={`evidence-${row.source_id}`} required value={evidence} onChange={(event) => setEvidence(event.target.value)} aria-describedby={`evidence-help-${row.source_id}`} />
          <p id={`evidence-help-${row.source_id}`} className="m-0 text-xs text-muted-foreground">{t('evidenceHelp')}</p>
        </div>
        <div className="flex flex-col gap-2">
          <Label htmlFor={`allowed-use-${row.source_id}`}>{t('allowedUse')}</Label>
          <Select value={allowedUse} onValueChange={(value) => setAllowedUse(value as NonNullable<ReviewDecision['allowed_use']>)}>
            <SelectTrigger id={`allowed-use-${row.source_id}`}><SelectValue placeholder={t('allowedUse')} /></SelectTrigger>
            <SelectContent>
              <SelectItem value="personal">{t('use.personal')}</SelectItem>
              <SelectItem value="noncommercial">{t('use.noncommercial')}</SelectItem>
              <SelectItem value="commercial">{t('use.commercial')}</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>
      {!evidence.trim() && <p className="m-0 text-xs text-muted-foreground">{t('evidenceRequired')}</p>}
      {mutation.isError && <p role="alert" className="m-0 text-sm text-destructive">{t('reviewFailed')}</p>}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" disabled={mutation.isPending || !evidence.trim() || !allowedUse}>
          {mutation.isPending ? t('saving') : t('approve')}
        </Button>
        <Button type="button" variant="outline" disabled={mutation.isPending || !evidence.trim()} onClick={(event) => submit(event, 'rejected')}>
          {mutation.isPending ? t('saving') : t('reject')}
        </Button>
      </div>
    </form>
  </article>;
}

