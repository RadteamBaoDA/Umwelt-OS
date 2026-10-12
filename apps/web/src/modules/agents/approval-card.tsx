'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { formatDateTime } from '@/core/i18n';
import { useDisplayPreferences } from '@/core/query-provider';
import { Badge } from '@/components/ui/badge';
import { Button, buttonVariants } from '@/components/ui/button';
import { cn } from 'cn';
import type { AgentApproval } from './api';

export interface ApprovalCardProps {
  approval: AgentApproval;
  busy?: boolean;
  disabled?: boolean;
  onDecision: (approval: AgentApproval, decision: 'approve' | 'deny') => void;
}

/** Ticks while an approval is still pending so expiry is evaluated outside render. */
function useNow(active: boolean) {
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), 15_000);
    return () => clearInterval(timer);
  }, [active]);
  return now;
}

/** Show the immutable action and effect status in the design's accent-bordered approval block; missing protected arguments or stale data disable decisions. */
export function ApprovalCard({ approval, busy = false, disabled = false, onDecision }: ApprovalCardProps) {
  const t = useTranslations('chat');
  const display = useDisplayPreferences();
  const now = useNow(approval.status === 'pending');
  const expired = approval.status === 'pending' && Date.parse(approval.expires_at) <= now;
  const pending = approval.status === 'pending' && Boolean(approval.arguments) && !expired;
  return (
    <section aria-labelledby={`approval-${approval.id}`} className="flex flex-col gap-2.5 rounded-xl border border-primary bg-card p-4 text-sm">
      <span className="text-[13px] font-extrabold uppercase tracking-[0.12em] text-primary">{t('approvalNeeded')}</span>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 id={`approval-${approval.id}`} className="text-[15px] font-semibold text-foreground">{t('approvalTitle')}</h2>
          <p className="mt-1 text-sm text-muted-foreground">{approval.tool_name} · {approval.tool_version}</p>
        </div>
        <Badge variant="outline">
          {expired ? t('approvalStatus.expired') : t(`approvalStatus.${approval.status}`)}
        </Badge>
      </div>
      <dl className="grid gap-2 sm:grid-cols-2">
        <div><dt className="text-muted-foreground">{t('approvalTarget')}</dt><dd className="break-all font-medium">{approval.destination_id}</dd></div>
        <div><dt className="text-muted-foreground">{t('approvalExpires')}</dt><dd>{formatDateTime(approval.expires_at, display.locale, display.timezone)}</dd></div>
      </dl>
      {approval.effect_status && (
        <p className="text-sm text-muted-foreground">
          {t('approvalEffectStatus')}: {t(`effectStatus.${approval.effect_status}`)}
        </p>
      )}
      {expired && <p role="status" className="rounded-md border border-border bg-secondary p-3 text-foreground">{t('approvalExpired')}</p>}
      {approval.status === 'requires_review' && (
        <p role="status" className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-foreground">
          {t('approvalReviewHelp')}
        </p>
      )}
      <details open={approval.status === 'pending' || approval.status === 'requires_review'} className="group">
        <summary className={cn(buttonVariants({ variant: 'ghost' }), 'list-none px-4 py-2.5 [&::-webkit-details-marker]:hidden')}>{t('approvalReviewDetails')}</summary>
        <div className="mt-2 rounded-md border border-border p-3">
          <h3 className="font-medium">{t('approvalArguments')}</h3>
          <pre className="mt-1 max-h-64 overflow-auto rounded-md bg-secondary p-3 text-xs text-foreground">{JSON.stringify(approval.arguments, null, 2)}</pre>
          <p className="mt-2 break-all text-xs text-muted-foreground">{t('approvalDigest')}: {approval.argument_hash}</p>
        </div>
      </details>
      {pending && (
        <div className="flex flex-wrap gap-2">
          <Button type="button" disabled={busy || disabled} onClick={() => onDecision(approval, 'approve')}>
            {busy ? t('approvalSaving') : t('approveAction')}
          </Button>
          <Button variant="outline" type="button" disabled={busy || disabled} onClick={() => onDecision(approval, 'deny')}>
            {t('denyAction')}
          </Button>
        </div>
      )}
    </section>
  );
}
