'use client';

import * as React from 'react';
import { CalendarIcon, CheckCircle2Icon, ClockIcon } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import type { AppLocaleId } from '@/core/i18n';
import { genUiCopy, MEETING_COPY, resolveWidgetLocale } from '../copy';

/** Candidate meeting time slot descriptor. */
export interface MeetingTimeSlot {
  /** Unique identifier for the slot within the picker. */
  id: string;
  /** Human-readable date or day label (e.g. '2026-10-20' or 'Tuesday, Oct 20'). */
  date: string;
  /** Time window string (e.g. '10:00 AM - 10:30 AM' or '14:00'). Optional when `start_time` is provided. */
  time?: string;
  /** Backend TimeSlot start time (`modules/chat/generative_ui/tools/form.py`). */
  start_time?: string;
  /** Backend TimeSlot end time (`modules/chat/generative_ui/tools/form.py`). */
  end_time?: string;
  /** Optional contextual note or duration badge (e.g. '30 min', 'Preferred'). */
  label?: string;
  /** Optional duration string from upstream schema. */
  duration?: string;
  /** Optional slot-level timezone string. */
  timezone?: string;
  /** Whether the slot is highlighted as recommended by the scheduling tool. */
  recommended?: boolean;
  /** Whether the slot can be selected (defaults to true; false renders disabled). */
  available?: boolean;
}

/** Internal normalized slot with guaranteed `id` and `time` string. */
interface NormalizedMeetingTimeSlot extends MeetingTimeSlot {
  id: string;
  time: string;
}

/** Props accepted by the native structured MeetingTimePicker component. */
export interface MeetingTimePickerProps {
  /** Headline title for the scheduling card. */
  title?: string;
  /** Optional subtitle or scheduling rationale. */
  description?: string;
  /** Backend `MeetingTimePickerPayload.subtitle` alias. */
  subtitle?: string;
  /** Optional upstream alias for scheduling rationale. */
  reasonForScheduling?: string;
  /** Candidate time slots to choose from. */
  slots?: MeetingTimeSlot[];
  /** Optional upstream alias for slots when `slots` is omitted. */
  timeSlots?: Array<Omit<MeetingTimeSlot, 'id'> & { id?: string }>;
  /** Optional meeting duration in minutes. */
  meetingDuration?: number;
  /** Backend `MeetingTimePickerPayload.duration_minutes` snake_case alias. */
  duration_minutes?: number;
  /** Optional recommended/initial slot identifier from backend payload. */
  selected_slot_id?: string | null;
  /** Optional timezone identifier displayed in the header. */
  timezone?: string;
  /** Application locale ('en-us' | 'vi-vi'). */
  locale?: AppLocaleId;
  /** Callback invoked when the user confirms a time slot or requests alternative times. */
  onSendPrompt?: (prompt: string) => void;
}

/**
 * Normalizes `slots` or upstream `timeSlots` into canonical MeetingTimeSlot items with stable IDs
 * and formatted time windows (supporting both `time` and backend `start_time`/`end_time`).
 *
 * @param slots - Primary slots array.
 * @param timeSlots - Fallback upstream timeSlots array.
 * @param meetingDuration - Optional duration in minutes to attach as a label.
 * @returns Normalized MeetingTimeSlot array.
 */
function normalizeSlots(
  slots?: MeetingTimeSlot[],
  timeSlots?: Array<Omit<MeetingTimeSlot, 'id'> & { id?: string }>,
  meetingDuration?: number,
): NormalizedMeetingTimeSlot[] {
  const rawList = slots && slots.length > 0 ? slots : (timeSlots ?? []);
  return rawList.map((slot, index) => {
    const resolvedTime =
      slot.time ??
      (slot.start_time && slot.end_time
        ? `${slot.start_time} - ${slot.end_time}`
        : slot.start_time ?? '');
    const fallbackLabel =
      slot.label ?? slot.duration ?? (meetingDuration ? `${meetingDuration} min` : undefined);
    return {
      ...slot,
      id: slot.id ?? `slot-${index + 1}`,
      date: slot.date,
      time: resolvedTime,
      label: fallbackLabel,
      available: slot.available !== false,
    };
  });
}

/**
 * Native structured MeetingTimePicker widget ported from OpenIntelligentUI and adapted to
 * Umwelt-OS shadcn primitives. Allows the user to select an available time slot, confirm their
 * selection, and dispatch a structured follow-up prompt back to the conversation.
 *
 * @param props - MeetingTimePickerProps configuration and callbacks.
 * @returns Accessible meeting slot selector card.
 */
export function MeetingTimePicker({
  title,
  description,
  subtitle,
  reasonForScheduling,
  slots,
  timeSlots,
  meetingDuration,
  duration_minutes,
  selected_slot_id,
  timezone,
  locale,
  onSendPrompt,
}: MeetingTimePickerProps) {
  const resolvedLocale = resolveWidgetLocale(locale);
  const copy = MEETING_COPY[resolvedLocale];
  const catalog = genUiCopy(resolvedLocale);

  const resolvedDuration = meetingDuration ?? duration_minutes;
  const displayTitle = title || reasonForScheduling || copy.defaultTitle;
  const displayDescription =
    description ?? subtitle ?? (title && reasonForScheduling ? reasonForScheduling : undefined);

  const normalizedSlots = React.useMemo(
    () => normalizeSlots(slots, timeSlots, resolvedDuration),
    [resolvedDuration, slots, timeSlots],
  );

  const [selectedSlotId, setSelectedSlotId] = React.useState<string | null>(null);
  const [confirmedSlot, setConfirmedSlot] = React.useState<NormalizedMeetingTimeSlot | null>(null);
  const [isDeclined, setIsDeclined] = React.useState(false);

  const selectedSlot = React.useMemo(
    () => normalizedSlots.find((slot) => slot.id === selectedSlotId) ?? null,
    [normalizedSlots, selectedSlotId],
  );

  /**
   * Selects a candidate available slot prior to final confirmation.
   */
  const handleSelectSlot = React.useCallback((slot: NormalizedMeetingTimeSlot) => {
    if (slot.available === false) return;
    setIsDeclined(false);
    setConfirmedSlot(null);
    setSelectedSlotId(slot.id);
  }, []);

  /**
   * Confirms the currently selected time slot and dispatches a follow-up prompt.
   */
  const handleConfirm = React.useCallback(() => {
    if (!selectedSlot || selectedSlot.available === false) return;
    setConfirmedSlot(selectedSlot);
    setIsDeclined(false);
    onSendPrompt?.(
      copy.confirmPrompt(displayTitle, selectedSlot.date, selectedSlot.time, selectedSlot.label),
    );
  }, [copy, displayTitle, onSendPrompt, selectedSlot]);

  /**
   * Declines the proposed slots and asks the assistant for alternative times.
   */
  const handleDecline = React.useCallback(() => {
    setIsDeclined(true);
    setConfirmedSlot(null);
    onSendPrompt?.(copy.declinePrompt(displayTitle));
  }, [copy, displayTitle, onSendPrompt]);

  return (
    <Card
      data-testid="generative-meeting-time-picker"
      className="my-2 w-full max-w-md border-border bg-surface text-foreground shadow-sm"
    >
      <CardHeader className="gap-1.5 border-b border-border pb-4">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <span className="inline-flex size-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <CalendarIcon className="size-4" aria-hidden="true" />
            </span>
            <div>
              <CardTitle className="text-base font-semibold text-foreground">
                {displayTitle}
              </CardTitle>
              {displayDescription && (
                <CardDescription className="mt-0.5 text-xs text-muted-foreground">
                  {displayDescription}
                </CardDescription>
              )}
            </div>
          </div>
          {timezone && (
            <Badge variant="outline" className="font-mono text-xs">
              {timezone}
            </Badge>
          )}
        </div>
      </CardHeader>

      <CardContent className="space-y-2.5 pt-4">
        <div role="group" aria-label={displayTitle} className="space-y-2">
          {normalizedSlots.map((slot) => {
            const isSelected = selectedSlotId === slot.id;
            const isAvailable = slot.available !== false;
            const isRecommended =
              slot.recommended === true || (Boolean(selected_slot_id) && slot.id === selected_slot_id);

            return (
              <button
                key={slot.id}
                type="button"
                disabled={!isAvailable}
                aria-pressed={isSelected}
                data-testid={`meeting-slot-${slot.id}`}
                onClick={() => handleSelectSlot(slot)}
                className={`flex w-full items-center justify-between gap-3 rounded-lg border p-3 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                  !isAvailable
                    ? 'cursor-not-allowed border-border bg-muted/30 opacity-60'
                    : isSelected
                      ? 'border-primary bg-primary/10 text-foreground'
                      : 'border-border bg-background hover:bg-muted/50 text-foreground'
                }`}
              >
                <div className="min-w-0">
                  <div className="text-sm font-semibold text-foreground">{slot.date}</div>
                  <div className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground">
                    <ClockIcon className="size-3.5 shrink-0" aria-hidden="true" />
                    <span className="font-mono">{slot.time}</span>
                  </div>
                </div>

                <div className="flex shrink-0 items-center gap-1.5">
                  {isRecommended && isAvailable && (
                    <Badge variant="outline" className="border-primary/40 text-primary text-[11px]">
                      {copy.recommendedBadge}
                    </Badge>
                  )}
                  {slot.label && (
                    <Badge variant="secondary" className="text-[11px]">
                      {slot.label}
                    </Badge>
                  )}
                  <Badge
                    variant={!isAvailable ? 'outline' : isSelected ? 'default' : 'secondary'}
                    className="text-[11px]"
                  >
                    {isAvailable ? copy.availableBadge : copy.unavailableBadge}
                  </Badge>
                </div>
              </button>
            );
          })}
        </div>

        {confirmedSlot && (
          <p
            role="status"
            data-testid="meeting-confirmed-banner"
            className="flex items-center gap-2 rounded-lg border border-primary/30 bg-primary/10 px-3 py-2 text-xs font-medium text-primary"
          >
            <CheckCircle2Icon className="size-4 shrink-0" aria-hidden="true" />
            <span>{copy.confirmedStatus(confirmedSlot.date, confirmedSlot.time)}</span>
          </p>
        )}

        {isDeclined && (
          <p
            role="status"
            data-testid="meeting-declined-banner"
            className="rounded-lg border border-border bg-muted/50 px-3 py-2 text-xs text-muted-foreground"
          >
            {copy.declinedStatus}
          </p>
        )}
      </CardContent>

      <CardFooter className="flex flex-wrap items-center justify-end gap-2 border-t border-border pt-4">
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={handleDecline}
          data-testid="meeting-decline-button"
        >
          {copy.suggestAlternative}
        </Button>
        <Button
          type="button"
          variant="default"
          size="sm"
          disabled={!selectedSlot || selectedSlot.available === false}
          onClick={handleConfirm}
          data-testid="meeting-confirm-button"
        >
          {catalog.genUiTimeConfirm}
        </Button>
      </CardFooter>
    </Card>
  );
}

