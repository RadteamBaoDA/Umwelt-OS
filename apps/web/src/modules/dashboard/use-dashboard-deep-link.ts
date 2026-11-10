import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { useEffect, useRef, useState } from 'react';

/** Preset ids are lowercase snake_case; template/source ids are validated against loaded data by the consumer, only their shape is checked here. */
const PRESET_ID = /^[a-z_]{1,40}$/;
const OPAQUE_ID = /^[A-Za-z0-9_-]{1,80}$/;

/** One-shot dashboard intent parsed from the URL. */
export interface DashboardDeepLink {
  /** `?preset=<id>`: open the preset picker on that preset. */
  presetId?: string;
  /** `?addTemplate=<templateId>[&source=<sourceId>]`: open Add gadget with that template (and source) preselected. */
  addTemplate?: { templateId: string; sourceId?: string };
}

/**
 * Reads `?preset` / `?addTemplate` / `?source` once on mount, then strips them with `router.replace` so a reload or
 * back-navigation never reopens the dialog. Malformed ids are dropped. Each intent is delivered at most once.
 *
 * @param options Readiness flag and the callbacks that act on the intent.
 */
export function useDashboardDeepLink({ ready, onPreset, onAddTemplate }: {
  /** True once the dashboard is loaded; the intent is held until then so later loads cannot undo it. */
  ready: boolean;
  /** Called once with a validated preset id. */
  onPreset: (presetId: string) => void;
  /** Called once with the template (and optional source) to preselect in Add gadget. */
  onAddTemplate: (target: { templateId: string; sourceId?: string }) => void;
}): void {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  // Parsed once from the first render's URL; the effect below only strips the params.
  const [link] = useState<DashboardDeepLink>(() => parseDeepLink(params));
  const delivered = useRef(false);
  const stripped = useRef(false);

  useEffect(() => {
    if (stripped.current || !['preset', 'addTemplate', 'source'].some((key) => params.has(key))) return;
    stripped.current = true;
    const rest = new URLSearchParams(params.toString());
    ['preset', 'addTemplate', 'source'].forEach((key) => rest.delete(key));
    const query = rest.toString();
    router.replace(query ? `${pathname}?${query}` : pathname);
  }, [params, router, pathname]);

  useEffect(() => {
    if (!ready || delivered.current || (!link.presetId && !link.addTemplate)) return;
    delivered.current = true;
    if (link.presetId) onPreset(link.presetId);
    if (link.addTemplate) onAddTemplate(link.addTemplate);
  }, [ready, link, onPreset, onAddTemplate]);
}

/** Validates the deep-link params by shape only; unknown ids are dropped later by the consumer. */
function parseDeepLink(params: URLSearchParams): DashboardDeepLink {
  const preset = params.get('preset');
  const template = params.get('addTemplate');
  const source = params.get('source');
  const next: DashboardDeepLink = {};
  if (preset && PRESET_ID.test(preset)) next.presetId = preset;
  if (template && OPAQUE_ID.test(template)) next.addTemplate = { templateId: template, sourceId: source && OPAQUE_ID.test(source) ? source : undefined };
  return next;
}
