'use client';

import * as React from 'react';
import type { ChartSeries } from '@/core/charts/recharts-wrapper';
import type { AppLocaleId } from '@/core/i18n';
import { chatMessages } from '@/core/messages/chat';
import { OpenGenerativeUIRenderer } from '../sandbox';
import type { GenerativeUIPayload } from '../sandbox/schema';
import { GenerativeBarChart, type GenerativeBarDatum } from './charts/bar-chart';
import { GenerativePieChart, type GenerativePieDatum } from './charts/pie-chart';
import { MeetingTimePicker, type MeetingTimeSlot } from './meeting-time-picker';
import { PlanCard, resolveWidgetLocale, type PlanStep } from './plan-card';
import { TableCatalog, type TableColumn, type TableRowData } from './table-catalog';

/** Props accepted by the unified ToolRendering dispatcher component. */
export interface ToolRenderingProps {
  /** Validated or streaming GenerativeUIPayload attached to a chat message or SSE stream. */
  payload: GenerativeUIPayload;
  /** Callback invoked when an interactive widget dispatches a follow-up prompt. */
  onSendPrompt?: (prompt: string) => void;
  /** Application locale ('en-us' | 'vi-vi'). */
  locale?: AppLocaleId;
}

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
export function ToolRendering({
  payload,
  onSendPrompt,
  locale,
}: ToolRenderingProps) {
  const resolvedLocale = resolveWidgetLocale(locale);
  const catalog = chatMessages[resolvedLocale];

  if (!payload || typeof payload !== 'object') {
    return null;
  }

  const rawRecord = payload as unknown as Record<string, unknown>;

  switch (payload.kind) {
    case 'sandboxed_ui': {
      return (
        <OpenGenerativeUIRenderer
          payload={payload}
          sandbox={payload.sandbox}
          status={payload.status}
          title={payload.title ?? payload.sandbox?.title}
          onSendPrompt={onSendPrompt}
          labels={{
            expand: catalog.genUiExpandFullscreen,
            exportHtml: catalog.genUiExportHtml,
          }}
        />
      );
    }

    case 'plan': {
      const planObj = (payload.plan ?? rawRecord) as Record<string, unknown>;
      const title =
        (typeof planObj.title === 'string' && planObj.title) ||
        payload.title ||
        'Execution Plan';
      const description =
        typeof planObj.description === 'string'
          ? planObj.description
          : typeof planObj.subtitle === 'string'
            ? planObj.subtitle
            : undefined;
      const steps = Array.isArray(planObj.steps) ? (planObj.steps as PlanStep[]) : undefined;
      const todos = Array.isArray(planObj.todos) ? (planObj.todos as PlanStep[]) : undefined;
      const approach = typeof planObj.approach === 'string' ? planObj.approach : undefined;
      const technology = typeof planObj.technology === 'string' ? planObj.technology : undefined;
      const keyElements = Array.isArray(planObj.keyElements)
        ? (planObj.keyElements as string[])
        : Array.isArray(planObj.key_elements)
          ? (planObj.key_elements as string[])
          : undefined;
      const source = typeof planObj.source === 'string' ? planObj.source : undefined;

      return (
        <PlanCard
          title={title}
          description={description}
          steps={steps}
          todos={todos}
          approach={approach}
          technology={technology}
          keyElements={keyElements}
          source={source}
          locale={resolvedLocale}
          onSendPrompt={onSendPrompt}
        />
      );
    }

    case 'table': {
      const tableObj = (payload.table ?? rawRecord) as Record<string, unknown>;
      const title =
        (typeof tableObj.title === 'string' && tableObj.title) ||
        payload.title ||
        'Data Table';
      const description =
        typeof tableObj.description === 'string'
          ? tableObj.description
          : typeof tableObj.subtitle === 'string'
            ? tableObj.subtitle
            : undefined;
      const columns = Array.isArray(tableObj.columns)
        ? (tableObj.columns as Array<TableColumn | string>)
        : [];
      const rows = Array.isArray(tableObj.rows) ? (tableObj.rows as TableRowData[]) : undefined;
      const records = Array.isArray(tableObj.records)
        ? (tableObj.records as Array<Record<string, unknown>>)
        : undefined;
      const source = typeof tableObj.source === 'string' ? tableObj.source : undefined;

      return (
        <TableCatalog
          title={title}
          description={description}
          columns={columns}
          rows={rows}
          records={records}
          source={source}
          locale={resolvedLocale}
          onSendPrompt={onSendPrompt}
        />
      );
    }

    case 'bar_chart': {
      const chartObj = (payload.chart ?? rawRecord) as Record<string, unknown>;
      const title =
        (typeof chartObj.title === 'string' && chartObj.title) ||
        payload.title ||
        'Bar Chart';
      const description =
        typeof chartObj.description === 'string'
          ? chartObj.description
          : typeof chartObj.subtitle === 'string'
            ? chartObj.subtitle
            : undefined;
      const data = Array.isArray(chartObj.data) ? (chartObj.data as GenerativeBarDatum[]) : [];
      const categoryKey =
        typeof chartObj.categoryKey === 'string'
          ? chartObj.categoryKey
          : typeof chartObj.x_key === 'string'
            ? chartObj.x_key
            : undefined;
      const series = Array.isArray(chartObj.series)
        ? (chartObj.series as ChartSeries[])
        : undefined;
      const currency = typeof chartObj.currency === 'string' ? chartObj.currency : undefined;
      const unit = typeof chartObj.unit === 'string' ? chartObj.unit : undefined;
      const source = typeof chartObj.source === 'string' ? chartObj.source : undefined;

      return (
        <GenerativeBarChart
          title={title}
          description={description}
          data={data}
          categoryKey={categoryKey}
          series={series}
          currency={currency}
          unit={unit}
          source={source}
          locale={resolvedLocale}
        />
      );
    }

    case 'pie_chart': {
      const chartObj = (payload.chart ?? rawRecord) as Record<string, unknown>;
      const title =
        (typeof chartObj.title === 'string' && chartObj.title) ||
        payload.title ||
        'Pie Chart';
      const description =
        typeof chartObj.description === 'string'
          ? chartObj.description
          : typeof chartObj.subtitle === 'string'
            ? chartObj.subtitle
            : undefined;
      const data = Array.isArray(chartObj.data) ? (chartObj.data as GenerativePieDatum[]) : [];
      const currency = typeof chartObj.currency === 'string' ? chartObj.currency : undefined;
      const unit = typeof chartObj.unit === 'string' ? chartObj.unit : undefined;
      const source = typeof chartObj.source === 'string' ? chartObj.source : undefined;

      return (
        <GenerativePieChart
          title={title}
          description={description}
          data={data}
          currency={currency}
          unit={unit}
          source={source}
          locale={resolvedLocale}
        />
      );
    }

    case 'meeting_time_picker': {
      const formObj = (payload.form ?? rawRecord) as Record<string, unknown>;
      const title =
        (typeof formObj.title === 'string' && formObj.title) ||
        payload.title ||
        undefined;
      const description =
        typeof formObj.description === 'string'
          ? formObj.description
          : typeof formObj.subtitle === 'string'
            ? formObj.subtitle
            : undefined;
      const slots = Array.isArray(formObj.slots) ? (formObj.slots as MeetingTimeSlot[]) : [];
      const meetingDuration =
        typeof formObj.durationMinutes === 'number'
          ? formObj.durationMinutes
          : typeof formObj.meetingDuration === 'number'
            ? formObj.meetingDuration
            : typeof formObj.duration_minutes === 'number'
              ? formObj.duration_minutes
              : undefined;
      const selectedSlotId =
        typeof formObj.selected_slot_id === 'string' ? formObj.selected_slot_id : undefined;
      const timezone = typeof formObj.timezone === 'string' ? formObj.timezone : undefined;

      return (
        <MeetingTimePicker
          title={title}
          description={description}
          slots={slots}
          meetingDuration={meetingDuration}
          selected_slot_id={selectedSlotId}
          timezone={timezone}
          locale={resolvedLocale}
          onSendPrompt={onSendPrompt}
        />
      );
    }

    default:
      return null;
  }
}

