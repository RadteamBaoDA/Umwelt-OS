'use client';

import { useTranslations } from 'next-intl';
import { useState } from 'react';

/** Mapping field a clicked key is written into. */
export type RestTarget = 'items_path' | 'id_field' | 'title_field' | 'content_field' | 'updated_field';

const TARGETS: RestTarget[] = ['items_path', 'id_field', 'title_field', 'content_field', 'updated_field'];

/** Translator over the `sources` namespace (`useTranslations('sources')`). */
type Translate = (key: string) => string;

/** Server error codes that have a `restError_<code>` message. */
const REST_ERROR_CODES: readonly string[] = ['rest_sample_auth_unsupported', 'rest_sample_url_refused', 'rest_sample_too_large', 'rest_sample_not_json', 'rest_sample_rate_limited', 'rest_sample_upstream_error', 'rest_sample_unreachable'];

/** Maps a server error code (ApiError code or message) to a human message; unknown text falls back to a generic one. */
export function restSampleErrorMessage(t: Translate, error: { code?: string | null; message?: string }): string {
  const key = [error.code, error.message].find((c): c is string => typeof c === 'string' && REST_ERROR_CODES.includes(c));
  return key ? t(`restError_${key}`) : t('restFallback');
}

/**
 * Converts a clicked absolute path into the value for `target`. items_path keeps the full path; item fields drop the
 * items_path prefix and the array index that follows it, so `data.items.0.title` becomes `title`.
 */
export function pathForTarget(target: RestTarget, clicked: string, itemsPath: string): string {
  if (target === 'items_path' || !itemsPath) return clicked;
  const prefix = `${itemsPath}.`;
  if (!clicked.startsWith(prefix)) return clicked;
  return clicked.slice(prefix.length).replace(/^\d+\.?/, '');
}

/** One JSON node: primitives render inline, objects/arrays as native details; the key is a real button that reports its path. */
function Node({ name, path, value, onPick }: { name: string; path: string; value: unknown; onPick: (path: string) => void }) {
  const isObj = typeof value === 'object' && value !== null;
  const key = (
    <button type="button" className="font-mono text-accent underline-offset-2 hover:underline" onClick={() => onPick(path)}>
      {name}
    </button>
  );
  if (!isObj) {
    return (
      <div className="pl-4">
        {key}: <span className="text-muted">{JSON.stringify(value)}</span>
      </div>
    );
  }
  const entries = Object.entries(value as object);
  return (
    <details className="pl-4">
      <summary className="cursor-pointer">
        {key} <span className="text-muted">{Array.isArray(value) ? `[${entries.length}]` : '{...}'}</span>
      </summary>
      {entries.map(([k, v]) => (
        <Node key={k} name={k} path={`${path}.${k}`} value={v} onPick={onPick} />
      ))}
    </details>
  );
}

/**
 * Status line, truncation note and a collapsible JSON tree of a REST sample. Clicking a key writes its dotted path
 * (converted by `pathForTarget`) into the field chosen in the "Fill into" select through `onFill`.
 */
export function RestSampleTree({ result, itemsPath, onFill }: { result: { status_code: number; truncated: boolean; sample: unknown }; itemsPath: string; onFill: (target: RestTarget, value: string) => void }) {
  const t = useTranslations('sources');
  const [target, setTarget] = useState<RestTarget>('items_path');
  const root = typeof result.sample === 'object' && result.sample !== null ? Object.entries(result.sample) : [];
  return (
    <div className="space-y-2 text-sm">
      <p className="m-0">
        {t('restStatus')}: {result.status_code}
      </p>
      {result.truncated && (
        <p className="m-0 text-muted" role="status">
          {t('restTruncated')}
        </p>
      )}
      <label className="flex items-center gap-2">
        {t('restFill')}
        <select className="rounded-md border border-line bg-surface px-2 py-1" value={target} onChange={(e) => setTarget(e.target.value as RestTarget)}>
          {TARGETS.map((target) => (
            <option key={target} value={target}>
              {t(`restTarget_${target}`)}
            </option>
          ))}
        </select>
      </label>
      <p className="m-0 text-muted">{t('restHint')}</p>
      <div className="max-h-72 overflow-auto rounded-md border border-line bg-surface p-2">
        {root.map(([k, v]) => (
          <Node key={k} name={k} path={k} value={v} onPick={(p) => onFill(target, pathForTarget(target, p, itemsPath))} />
        ))}
      </div>
    </div>
  );
}
