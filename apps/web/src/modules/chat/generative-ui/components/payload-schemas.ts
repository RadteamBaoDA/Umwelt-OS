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
    is_sample: z.boolean().optional(),
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
    is_sample: z.boolean().optional(),
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

/** One input field of the generic form widget; limits mirror `FormFieldSpec` in `tools/form.py`. */
export const inputFieldSchema = z.object({
  id: z.string().regex(/^[A-Za-z][A-Za-z0-9_-]{0,39}$/),
  label: z.string().min(1).max(120),
  type: z.enum(['text', 'textarea', 'number', 'select', 'checkbox', 'date']).default('text'),
  required: z.boolean().default(false),
  placeholder: z.string().max(120).default(''),
  options: z.array(z.string().min(1).max(80)).max(20).default([]),
  min: z.number().finite().nullable().optional(),
  max: z.number().finite().nullable().optional(),
  maxlength: z.number().int().min(1).max(4000).nullable().optional(),
});

/** Generic input form payload (`FormPayload`): 1..12 uniquely-id'd fields. */
export const inputFormPayloadSchema = z
  .object({
    title: z.string().min(1).max(255),
    description: z.string().max(500).default(''),
    fields: z.array(inputFieldSchema).min(1).max(12),
    submit_label: z.string().max(40).default(''),
  })
  .refine((f) => new Set(f.fields.map((x) => x.id)).size === f.fields.length, { message: 'duplicate field ids' });

/** Parsed generic form field. */
export type InputFormField = z.infer<typeof inputFieldSchema>;

/** Parsed generic form payload. */
export type InputFormPayload = z.infer<typeof inputFormPayloadSchema>;

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
