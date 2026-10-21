'use client';

import { useTranslations } from 'next-intl';
import { Checkbox } from '@/components/ui/checkbox';
import { Label } from '@/components/ui/label';
import { CheckboxField } from '@/modules/settings/settings-layout';

/** The two owner switches a source carries; both default to on so existing behavior is unchanged. */
export type SourceAiOptions = { graph_enabled: boolean; rag_index_enabled: boolean };

/**
 * Two checkboxes for "build knowledge graph" and "index for Q&A (RAG)" with short help text.
 * Controlled: the parent owns the values and decides whether a change is only local (create) or
 * patched immediately (edit). Collection never depends on either switch.
 *
 * @param idPrefix Makes the input ids unique when several instances share a page.
 */
export function SourceAiToggles({ value, onChange, disabled, idPrefix }: {
  value: SourceAiOptions;
  onChange: (next: SourceAiOptions) => void;
  disabled?: boolean;
  idPrefix: string;
}) {
  const t = useTranslations('sources');
  return <div className="flex flex-col gap-3" role="group" aria-label={t('aiOptionsTitle')}>
    <div className="flex flex-col gap-1">
      <CheckboxField>
        <Checkbox id={`${idPrefix}-graph`} disabled={disabled} checked={value.graph_enabled} onCheckedChange={(checked) => onChange({ ...value, graph_enabled: checked === true })} />
        <Label htmlFor={`${idPrefix}-graph`}>{t('graphEnabledLabel')}</Label>
      </CheckboxField>
      <p className="m-0 text-sm text-muted-foreground">{t('graphEnabledHelp')}</p>
    </div>
    <div className="flex flex-col gap-1">
      <CheckboxField>
        <Checkbox id={`${idPrefix}-rag`} disabled={disabled} checked={value.rag_index_enabled} onCheckedChange={(checked) => onChange({ ...value, rag_index_enabled: checked === true })} />
        <Label htmlFor={`${idPrefix}-rag`}>{t('ragIndexLabel')}</Label>
      </CheckboxField>
      <p className="m-0 text-sm text-muted-foreground">{t('ragIndexHelp')}</p>
    </div>
    <p className="m-0 text-sm text-muted-foreground">{t('aiOptionsNote')}</p>
  </div>;
}
