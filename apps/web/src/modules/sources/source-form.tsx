'use client';

import { zodResolver } from '@hookform/resolvers/zod';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useForm } from 'react-hook-form';
import { useTranslations } from 'next-intl';
import { z } from 'zod';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import { createManualSource, sourceKeys } from './api';

const schema = z.object({ name: z.string().trim().min(1).max(200) });
type Values = z.infer<typeof schema>;

/** Creates a manual source and invokes onSaved after the mutation succeeds. */
export function SourceForm({ onSaved }: { onSaved: () => void }) {
  const t = useTranslations('sources');
  const { csrfToken } = useWorkspaceSession();
  const queryClient = useQueryClient();
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { name: '' } });
  const create = useMutation({ mutationFn: (values: Values) => createManualSource(values.name, csrfToken), onSuccess: () => { queryClient.invalidateQueries({ queryKey: sourceKeys.all }); onSaved(); } });
  return <form className="form" onSubmit={form.handleSubmit((values) => create.mutate(values))}>
    <div className="field"><Label htmlFor="source-name">{t('sourceName')}</Label><Input id="source-name" autoFocus maxLength={200} {...form.register('name')} />{form.formState.errors.name && <span className="error">{t('sourceNameRequired')}</span>}</div>
    <p className="text-muted-foreground">{t('manualDescription')}</p>
    {create.error && <p className="error" role="alert">{t('actionFailed')}</p>}
    <div className="form-actions"><Button type="submit" disabled={create.isPending}>{t('saveSource')}</Button><Button type="button" variant="outline" onClick={onSaved}>{t('cancel')}</Button></div>
  </form>;
}
