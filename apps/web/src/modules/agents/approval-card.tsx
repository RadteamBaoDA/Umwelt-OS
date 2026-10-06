'use client';

import * as React from 'react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
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

/** Show the immutable action and effect status; missing protected arguments or stale data disable decisions. */
export function ApprovalCard({ approval, busy = false, disabled = false, onDecision }: ApprovalCardProps) {
  const t = useTranslations('chat');
  const now = useNow(approval.status === 'pending');
  const expired = approval.status === 'pending' && Date.parse(approval.expires_at) <= now;
  const pending = approval.status === 'pending' && Boolean(approval.arguments) && !expired;
  return (
    <section aria-labelledby={`approval-${approval.id}`} className="rounded-lg border border-border bg-background p-4 text-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 id={`approval-${approval.id}`} className="font-semibold text-foreground">{t('approvalTitle')}</h2>
          <p className="mt-1 text-muted-foreground">{approval.tool_name} · {approval.tool_version}</p>
        </div>
        <span className="rounded-full border border-border bg-secondary px-2 py-1 text-xs text-foreground">
          {expired ? t('approvalStatus.expired') : t(`approvalStatus.${approval.status}`)}
        </span>
      </div>
      <dl className="mt-3 grid gap-2 sm:grid-cols-2">
        <div><dt className="text-muted-foreground">{t('approvalTarget')}</dt><dd className="break-all font-medium">{approval.destination_id}</dd></div>
        <div><dt className="text-muted-foreground">{t('approvalExpires')}</dt><dd>{new Date(approval.expires_at).toLocaleString()}</dd></div>
      </dl>
      <details open={approval.status === 'pending' || approval.status === 'requires_review'} className="mt-3 rounded-md border border-border">
        <summary className="min-h-11 cursor-pointer px-3 py-2.5 font-medium">{t('approvalReviewDetails')}</summary>
        <div className="border-t border-border p-3">
          <h3 className="font-medium">{t('approvalArguments')}</h3>
          <pre className="mt-1 max-h-64 overflow-auto rounded-md bg-secondary p-3 text-xs text-foreground">{JSON.stringify(approval.arguments, null, 2)}</pre>
          <p className="mt-2 break-all text-xs text-muted-foreground">{t('approvalDigest')}: {approval.argument_hash}</p>
        </div>
      </details>
      {approval.effect_status && (
        <p className="mt-2 text-sm text-muted-foreground">
          {t('approvalEffectStatus')}: {t(`effectStatus.${approval.effect_status}`)}
        </p>
      )}
      {expired && <p role="status" className="mt-3 rounded-md border border-border bg-secondary p-3 text-foreground">{t('approvalExpired')}</p>}
      {approval.status === 'requires_review' && (
        <p role="status" className="mt-3 rounded-md border border-destructive/30 bg-destructive/5 p-3 text-foreground">
          {t('approvalReviewHelp')}
        </p>
      )}
      {pending && (
        <div className="mt-4 flex flex-wrap gap-2">
          <Button type="button" disabled={busy || disabled} onClick={() => onDecision(approval, 'approve')}>
            {busy ? t('approvalSaving') : t('approveAction')}
          </Button>
          <Button className="secondary" type="button" disabled={busy || disabled} onClick={() => onDecision(approval, 'deny')}>
            {t('denyAction')}
          </Button>
        </div>
      )}
    </section>
  );
}
