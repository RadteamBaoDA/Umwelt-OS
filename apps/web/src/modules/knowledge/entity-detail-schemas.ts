import { z } from 'zod';
import { ApiError } from '@/core/api';

export const identitySchema = z.object({ name: z.string().max(300), description: z.string().max(10_000), reason: z.string().trim().min(1).max(300) });
export type IdentityValues = z.infer<typeof identitySchema>;
export const aliasSchema = z.object({ alias: z.string().trim().min(1).max(300), reason: z.string().trim().min(1).max(300) });
export const correctionSchema = z.object({ targetId: z.string(), splitName: z.string(), reason: z.string().trim().min(1, 'correctionReasonRequired').max(300, 'correctionReasonRequired') });
export const mergeConfirmationSchema = z.object({ targetId: z.string().trim().min(1).max(100), reason: z.string().trim().min(1).max(300) });
export const splitConfirmationSchema = z.object({ splitName: z.string().trim().min(1).max(300), reason: z.string().trim().min(1).max(300) });
export type CorrectionValues = z.infer<typeof correctionSchema>;

/** Extracts conflict details suitable for display from a request error. */
export function conflictDetails(error: unknown): string[] {
  if (!(error instanceof ApiError) || error.status !== 409 || !error.details || typeof error.details !== 'object') return [];
  const details = error.details as Record<string, unknown>;
  /** Accepts only string conflict fields and truncates them to the caller’s display bound. */
  const boundedText = (value: unknown, limit: number) => typeof value === 'string' ? value.slice(0, limit) : null;
  const raw = Array.isArray(details.conflicts) ? details.conflicts : details.conflict ? [details.conflict] : [];
  return raw.slice(0, 5).flatMap((value) => {
    if (!value || typeof value !== 'object') return [];
    const conflict = value as Record<string, unknown>;
    return [
      [boundedText(conflict.code, 80), boundedText(conflict.message, 300)].filter((part): part is string => part !== null).join(': '),
      ...(['entity_ids', 'membership_ids', 'relationship_ids'] as const).flatMap((key) => {
        const ids = conflict[key];
        return Array.isArray(ids) && ids.length ? [`${key}: ${ids.slice(0, 5).map((id) => boundedText(id, 80)).filter((id): id is string => id !== null).join(', ')}`] : [];
      }),
    ].filter(Boolean).slice(0, 4);
  }).slice(0, 20);
}
