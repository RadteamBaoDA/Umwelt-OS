'use client';

import dynamic from 'next/dynamic';
import * as React from 'react';
import type { ChartSeries } from '@/core/charts/chart-format';
import type { AppLocaleId } from '@/core/i18n';
import { OpenGenerativeUIRenderer } from '../sandbox';
import type { GenerativeUIPayload } from '../sandbox/schema';
import type { GenerativeBarDatum } from './charts/bar-chart';
import type { GenerativePieDatum } from './charts/pie-chart';
import { InputForm } from './input-form';
import { MeetingTimePicker, type MeetingTimeSlot } from './meeting-time-picker';
import { genUiCopy, resolveWidgetLocale } from '../copy';
import { PlanCard, type PlanStep } from './plan-card';
import {
  chartPayloadSchema,
  formPayloadSchema,
  inputFormPayloadSchema,
  planPayloadSchema,
  sandboxPayloadSchema,
  tablePayloadSchema,
} from './payload-schemas';
import { ToolReasoning } from './tool-reasoning';
import { TableCatalog, type TableColumn, type TableRowData } from './table-catalog';

/** Props accepted by the unified ToolRendering dispatcher component. */
export interface ToolRenderingProps {
  /** Validated or streaming GenerativeUIPayload attached to a chat message or SSE stream. */
  payload: GenerativeUIPayload;
  /** Callback invoked when an interactive widget dispatches a follow-up prompt. */
  onSendPrompt?: (prompt: string) => void | Promise<unknown>;
  /** Application locale ('en-us' | 'vi-vi'). */
  locale?: AppLocaleId;
}

/** Everything a kind renderer needs (resolved once per render by the dispatcher). */
interface RenderContext {
  payload: GenerativeUIPayload;
  /** The payload viewed as a plain record (top-level properties emitted by backend tools). */
  raw: Record<string, unknown>;
  locale: AppLocaleId;
  catalog: ReturnType<typeof genUiCopy>;
  onSendPrompt?: (prompt: string) => void | Promise<unknown>;
}

type SafeParser = { safeParse: (v: unknown) => { success: boolean } };

/** Returns `obj[key]` when it is a string. */
function str(obj: Record<string, unknown>, key: string): string | undefined {
  return typeof obj[key] === 'string' ? (obj[key] as string) : undefined;
}

/** Returns `obj[key]` as an array of `T` (unchecked cast; shapes are validated by the kind schemas) or undefined. */
function arr<T>(obj: Record<string, unknown>, key: string): T[] | undefined {
  return Array.isArray(obj[key]) ? (obj[key] as T[]) : undefined;
}

/** Description precedence shared by all widgets: `description`, then `subtitle`. */
function descriptionOf(obj: Record<string, unknown>): string | undefined {
  return str(obj, 'description') ?? str(obj, 'subtitle');
}

/** Title precedence shared by all widgets: the object's non-empty title, then the payload title, then the fallback. */
function titleOf<F extends string | undefined>(obj: Record<string, unknown>, payload: GenerativeUIPayload, fallback: F): string | F {
  return (typeof obj.title === 'string' && obj.title) || payload.title || fallback;
}

/**
 * Localized invalid-payload alert (never throws) shown when a widget payload fails its kind-specific schema, prompting
 * the owner to ask the assistant to regenerate it.
 */
function InvalidPayload({ catalog }: { catalog: RenderContext['catalog'] }) {
  return (
    <div
      role="alert"
      data-testid="generative-ui-invalid-payload"
      className="rounded-lg border border-destructive bg-surface p-3 text-sm text-destructive"
    >
      {catalog.genUiInvalidPayload}
    </div>
  );
}

/** True when `value` satisfies the kind schema. */
function isValid(schema: SafeParser, value: unknown): boolean {
  return schema.safeParse(value).success;
}

/** Sandboxed streaming UI. */
function renderSandbox({ payload, catalog }: RenderContext): React.ReactNode {
  if (payload.sandbox !== undefined && !isValid(sandboxPayloadSchema, payload.sandbox)) {
    return <InvalidPayload catalog={catalog} />;
  }
  return (
    <OpenGenerativeUIRenderer
      payload={payload}
      sandbox={payload.sandbox}
      status={payload.status}
      title={payload.title ?? payload.sandbox?.title}
      labels={{
        expand: catalog.genUiExpandFullscreen,
        exportHtml: catalog.genUiExportHtml,
      }}
    />
  );
}

/** Execution plan card. */
function renderPlan({ payload, raw, catalog, locale, onSendPrompt }: RenderContext): React.ReactNode {
  const planObj = (payload.plan ?? raw) as Record<string, unknown>;
  if (!isValid(planPayloadSchema, planObj)) return <InvalidPayload catalog={catalog} />;
  return (
    <PlanCard
      title={titleOf(planObj, payload, catalog.genUiTitlePlan)}
      description={descriptionOf(planObj)}
      steps={arr<PlanStep>(planObj, 'steps')}
      todos={arr<PlanStep>(planObj, 'todos')}
      approach={str(planObj, 'approach')}
      technology={str(planObj, 'technology')}
      keyElements={arr<string>(planObj, 'keyElements') ?? arr<string>(planObj, 'key_elements')}
      source={str(planObj, 'source')}
      locale={locale}
      onSendPrompt={onSendPrompt}
    />
  );
}

/** Sortable, filterable table. */
function renderTable({ payload, raw, catalog, locale, onSendPrompt }: RenderContext): React.ReactNode {
  const tableObj = (payload.table ?? raw) as Record<string, unknown>;
  if (!isValid(tablePayloadSchema, tableObj)) return <InvalidPayload catalog={catalog} />;
  return (
    <TableCatalog
      title={titleOf(tableObj, payload, catalog.genUiTitleTable)}
      description={descriptionOf(tableObj)}
      columns={arr<TableColumn | string>(tableObj, 'columns') ?? []}
      rows={arr<TableRowData>(tableObj, 'rows')}
      records={arr<Record<string, unknown>>(tableObj, 'records')}
      source={str(tableObj, 'source')}
      isSample={tableObj.is_sample === true}
      locale={locale}
      onSendPrompt={onSendPrompt}
    />
  );
}

/** Fields shared by the bar and pie chart widgets. */
function chartCommon(chartObj: Record<string, unknown>) {
  return {
    description: descriptionOf(chartObj),
    categoryKey: str(chartObj, 'categoryKey') ?? str(chartObj, 'x_key'),
    series: arr<ChartSeries>(chartObj, 'series'),
    currency: str(chartObj, 'currency'),
    unit: str(chartObj, 'unit'),
    source: str(chartObj, 'source'),
    isSample: chartObj.is_sample === true,
  };
}

/** Placeholder while a chart chunk loads (same neutral surface the dashboard gadgets use). */
function ChartChunkSkeleton(): React.ReactElement {
  return <div className="skeleton h-64 w-full" aria-hidden="true" />;
}

/** Charts are lazy so Recharts loads only when a chart payload actually renders, not with the chat route. */
const GenerativeBarChart = dynamic(() => import('./charts/bar-chart').then((m) => m.GenerativeBarChart), { loading: ChartChunkSkeleton });
const GenerativePieChart = dynamic(() => import('./charts/pie-chart').then((m) => m.GenerativePieChart), { loading: ChartChunkSkeleton });

/** Bar / line / scatter / distribution chart (`chart_type`, default bar). */
function renderBarChart({ payload, raw, catalog, locale }: RenderContext): React.ReactNode {
  const chartObj = (payload.chart ?? raw) as Record<string, unknown>;
  if (!isValid(chartPayloadSchema, chartObj)) return <InvalidPayload catalog={catalog} />;
  const chartType = chartObj.chart_type;
  return (
    <GenerativeBarChart
      title={titleOf(chartObj, payload, catalog.genUiTitleBar)}
      data={arr<GenerativeBarDatum>(chartObj, 'data') ?? []}
      {...chartCommon(chartObj)}
      locale={locale}
      chartType={chartType === 'line' || chartType === 'scatter' || chartType === 'distribution' ? chartType : 'bar'}
    />
  );
}

/** Pie chart. */
function renderPieChart({ payload, raw, catalog, locale }: RenderContext): React.ReactNode {
  const chartObj = (payload.chart ?? raw) as Record<string, unknown>;
  if (!isValid(chartPayloadSchema, chartObj)) return <InvalidPayload catalog={catalog} />;
  return (
    <GenerativePieChart
      title={titleOf(chartObj, payload, catalog.genUiTitlePie)}
      data={arr<GenerativePieDatum>(chartObj, 'data') ?? []}
      {...chartCommon(chartObj)}
      locale={locale}
    />
  );
}

/** Duration precedence: `durationMinutes`, `meetingDuration`, then `duration_minutes` (numbers only). */
function meetingDurationOf(formObj: Record<string, unknown>): number | undefined {
  for (const key of ['durationMinutes', 'meetingDuration', 'duration_minutes']) {
    if (typeof formObj[key] === 'number') return formObj[key] as number;
  }
  return undefined;
}

/** Meeting time picker (declarative payload; answers go through the prompt bridge). */
function renderMeetingPicker({ payload, raw, catalog, locale, onSendPrompt }: RenderContext): React.ReactNode {
  const formObj = (payload.form ?? raw) as Record<string, unknown>;
  if (!isValid(formPayloadSchema, formObj)) return <InvalidPayload catalog={catalog} />;
  return (
    <MeetingTimePicker
      title={titleOf(formObj, payload, undefined)}
      description={descriptionOf(formObj)}
      slots={arr<MeetingTimeSlot>(formObj, 'slots') ?? []}
      meetingDuration={meetingDurationOf(formObj)}
      selected_slot_id={str(formObj, 'selected_slot_id')}
      timezone={str(formObj, 'timezone')}
      locale={locale}
      onSendPrompt={onSendPrompt}
    />
  );
}

/** Generic input form. Partial arguments are expected while streaming; only a completed payload is an error. */
function renderForm({ payload, raw, catalog, locale, onSendPrompt }: RenderContext): React.ReactNode {
  const parsed = inputFormPayloadSchema.safeParse(payload.form ?? raw);
  if (!parsed.success) {
    return payload.status === 'streaming' ? null : <InvalidPayload catalog={catalog} />;
  }
  return (
    <InputForm
      key={parsed.data.fields.map((f) => f.id).join('|')}
      title={parsed.data.title}
      description={parsed.data.description}
      fields={parsed.data.fields}
      submitLabel={parsed.data.submit_label}
      disabled={payload.status === 'streaming'}
      locale={locale}
      onSendPrompt={onSendPrompt}
    />
  );
}

/**
 * Renderer registry keyed by `payload.kind`. Add a new native widget by registering one entry here; unregistered kinds
 * fall back to a collapsed tool-call summary.
 */
const KIND_RENDERERS: Record<string, (ctx: RenderContext) => React.ReactNode> = {
  sandboxed_ui: renderSandbox,
  plan: renderPlan,
  table: renderTable,
  bar_chart: renderBarChart,
  pie_chart: renderPieChart,
  meeting_time_picker: renderMeetingPicker,
  form: renderForm,
};

/**
 * Unified Generative UI dispatcher that routes a `GenerativeUIPayload` by `payload.kind`
 * to either the Sandboxed Streaming UI renderer (`OpenGenerativeUIRenderer`) or one of the
 * five native structured Umwelt-OS widgets (`PlanCard`, `TableCatalog`, `GenerativeBarChart`,
 * `GenerativePieChart`, `MeetingTimePicker`).
 *
 * Supports both nested sub-objects (`payload.plan`, `payload.table`, `payload.chart`, `payload.form`)
 * and top-level payload properties emitted by backend tool definitions.
 *
 * @param props - ToolRenderingProps containing payload, prompt callback, and locale.
 * @returns Rendered Generative UI widget, or null if payload is absent/unrecognized.
 */
export function ToolRendering({ payload, onSendPrompt, locale }: ToolRenderingProps) {
  const resolvedLocale = resolveWidgetLocale(locale);
  const catalog = genUiCopy(resolvedLocale);

  if (!payload || typeof payload !== 'object') {
    return null;
  }

  const raw = payload as unknown as Record<string, unknown>;
  const render = Object.hasOwn(KIND_RENDERERS, payload.kind) ? KIND_RENDERERS[payload.kind] : undefined;
  if (render) return <>{render({ payload, raw, locale: resolvedLocale, catalog, onSendPrompt })}</>;
  // No dedicated widget for this kind: show a collapsed, inspectable tool-call summary.
  return <ToolReasoning name={String((payload as { kind?: unknown }).kind ?? 'tool')} data={raw} locale={resolvedLocale} />;
}
