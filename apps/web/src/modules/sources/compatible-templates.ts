import type { DataShape } from '@/core/data-shapes';

/** The `/api/v1/gadget-templates` fields this module reads (the dashboard module's full type is not on its public index). */
export type GadgetTemplate = { id: string; renderer: string; source_mode: 'none' | 'single' | 'multi'; accepted_shapes: DataShape[] | null };

/** Copy of `isAccepted` in `dashboard/use-preset-picker.ts` (deep imports are lint-restricted): null accepts any shape. */
function isAccepted(shapes: readonly DataShape[] | undefined, accepted: readonly DataShape[] | null): boolean {
  return accepted === null || (shapes ?? []).some((shape) => accepted.includes(shape));
}

/**
 * Gadget templates a source can feed: the template takes a source (`source_mode !== 'none'`) and the source's
 * `data_shapes` (absent = none) intersect its `accepted_shapes` (null accepts any). One template per renderer so chips stay distinct;
 * input order is kept. Pure; a source without shape metadata only matches shape-agnostic templates.
 */
export function compatibleTemplates(source: { data_shapes?: readonly DataShape[] }, templates: readonly GadgetTemplate[]): GadgetTemplate[] {
  const seen = new Set<string>();
  return templates.filter((template) => {
    if (template.source_mode === 'none' || seen.has(template.renderer) || !isAccepted(source.data_shapes, template.accepted_shapes)) return false;
    seen.add(template.renderer);
    return true;
  });
}

/** Dashboard deep link that asks to add `templateId` bound to `sourceId` (the dashboard side handles it separately). */
export function addTemplateHref(templateId: string, sourceId: string): string {
  return `/dashboard?addTemplate=${encodeURIComponent(templateId)}&source=${encodeURIComponent(sourceId)}`;
}
