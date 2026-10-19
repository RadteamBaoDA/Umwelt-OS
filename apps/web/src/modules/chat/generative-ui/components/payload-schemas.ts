import { z } from 'zod';

/**
 * Zod validators for every generative-UI payload kind, mirroring the backend models in
 * `modules/chat/generative_ui/tools/{plan,query,form}.py` (snake_case + camelCase aliases the
 * widgets already accept). They are intentionally lenient about optional/unknown fields
 * (`passthrough`) but strict about container types, so a malformed model/tool payload is
 * rejected before it can crash a widget.
 */

const optionalText = z.string().optional();
const optionalNumber = z.number().finite().optional();

/** Plan payload (`PlanPayload`): steps/todos are arrays of records, text fields are strings. */
export const planPayloadSchema = z
  .object({
    title: optionalText,
    description: optionalText,
    subtitle: optionalText,
    approach: optionalText,
    technology: optionalText,
    source: optionalText,
    keyElements: z.array(z.string()).optional(),
    key_elements: z.array(z.string()).optional(),
    steps: z.array(z.object({ id: optionalText, title: optionalText, label: optionalText }).passthrough()).optional(),
    todos: z.array(z.record(z.string(), z.unknown())).optional(),
  })
  .passthrough();

/** Table payload (`TablePayload`): columns are keyed descriptors or plain header strings. */
export const tablePayloadSchema = z
  .object({
    title: optionalText,
    description: optionalText,
    subtitle: optionalText,
    source: optionalText,
    columns: z.array(
      z.union([z.string(), z.object({ key: z.string().min(1), label: z.string() }).passthrough()]),
    ),
    rows: z.array(z.union([z.array(z.unknown()), z.record(z.string(), z.unknown())])).optional(),
    records: z.array(z.record(z.string(), z.unknown())).optional(),
  })
  .passthrough();

/** Chart payload (`ChartPayload`) shared by the bar/line/scatter/distribution and pie widgets. */
export const chartPayloadSchema = z
  .object({
    title: optionalText,
    description: optionalText,
    subtitle: optionalText,
    source: optionalText,
    unit: optionalText,
    currency: optionalText,
    categoryKey: optionalText,
    x_key: optionalText,
    // True chart type; `kind` stays bar/pie for persisted compatibility. Unknown values render as bar.
    chart_type: optionalText,
    data: z.array(z.record(z.string(), z.unknown())),
    series: z.array(z.record(z.string(), z.unknown())).optional(),
  })
  .passthrough();

/** Meeting picker payload (`MeetingTimePickerPayload`). */
export const formPayloadSchema = z
  .object({
    title: optionalText,
    description: optionalText,
    subtitle: optionalText,
    timezone: optionalText,
    durationMinutes: optionalNumber,
    duration_minutes: optionalNumber,
    meetingDuration: optionalNumber,
    selected_slot_id: z.string().nullable().optional(),
    slots: z.array(z.object({ id: z.string() }).passthrough()),
  })
  .passthrough();

/** Sandbox payload (partial while streaming): only string-typed fields are enforced. */
export const sandboxPayloadSchema = z
  .object({
    initialHeight: optionalNumber,
    placeholderMessages: z.array(z.string()).optional(),
    css: optionalText,
    html: z.union([z.string(), z.array(z.string())]).optional(),
    jsFunctions: optionalText,
    jsExpressions: z.union([z.string(), z.array(z.string())]).optional(),
    title: optionalText,
  })
  .passthrough();
