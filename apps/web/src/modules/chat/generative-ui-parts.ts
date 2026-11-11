/** Helpers for the ordered multi-widget ("parts") model of an assistant turn. */

/** One widget payload as sent by the API / SSE (`part_id` identifies it within its turn). */
export type UiPart = Record<string, unknown>;

/**
 * Insert or replace a streamed widget part, keyed by `part_id`, keeping arrival order.
 *
 * A later snapshot of the same part replaces the earlier one in place; a payload without a
 * `part_id` (older server) is treated as the single part with the empty id.
 *
 * A part still marked `rebuilding` (see {@link markUiPartRebuilding}) is dropped once its own replacement (same
 * `part_id`) or a genuinely new part arrives: the replacement round has started, so the placeholder frame has done its
 * job. A snapshot that merely updates another live part leaves the placeholder alone.
 *
 * @param parts Current ordered parts of the streaming turn.
 * @param next Newly received `message.ui.delta` / `message.ui.done` payload.
 * @returns A new array; the input is never mutated.
 */
export function upsertUiPart(parts: readonly UiPart[], next: UiPart): UiPart[] {
  const id = typeof next.part_id === 'string' ? next.part_id : '';
  const keyOf = (p: UiPart) => (typeof p.part_id === 'string' ? p.part_id : '');
  const updatesLivePart = parts.some((p) => p.rebuilding !== true && keyOf(p) === id);
  const live = !updatesLivePart && parts.some((p) => p.rebuilding === true) ? parts.filter((p) => p.rebuilding !== true) : parts;
  const at = live.findIndex((p) => keyOf(p) === id);
  if (at < 0) return [...live, next];
  const copy = [...live];
  copy[at] = next;
  return copy;
}

/**
 * Keep a streamed widget part the server reported invalid (`message.ui.discard`) visible as "rebuilding" instead of
 * removing it: the model is asked to call the tool again, and the frame stays (without running its script again) until
 * a replacement snapshot arrives or the run ends and the live parts are cleared.
 *
 * @param parts Current ordered parts of the streaming turn.
 * @param partId Id of the discarded part; an unknown id leaves the list unchanged.
 * @returns A new array when a part was marked, otherwise the same array.
 */
export function markUiPartRebuilding(parts: readonly UiPart[], partId: string): UiPart[] {
  if (!parts.some((p) => p.part_id === partId)) return parts as UiPart[];
  return parts.map((p) => (p.part_id === partId ? { ...p, status: 'streaming', rebuilding: true } : p));
}

/**
 * Ordered widget parts of a persisted message.
 *
 * Prefers `generative_ui_parts`; rows written before multi-widget turns only carry `generative_ui`.
 *
 * @param message Message-like object with the optional widget fields.
 * @returns Parts in render order (possibly empty).
 */
export function messageUiParts(message: { generative_ui?: UiPart | null; generative_ui_parts?: UiPart[] | null }): UiPart[] {
  if (Array.isArray(message.generative_ui_parts) && message.generative_ui_parts.length > 0) {
    return message.generative_ui_parts;
  }
  return message.generative_ui ? [message.generative_ui] : [];
}

/**
 * Stable React key for a part.
 *
 * @param part Widget payload.
 * @param index Position, used when the payload carries no `part_id`.
 */
export function uiPartKey(part: UiPart, index: number): string {
  return typeof part.part_id === 'string' && part.part_id ? part.part_id : `part-${index}`;
}
