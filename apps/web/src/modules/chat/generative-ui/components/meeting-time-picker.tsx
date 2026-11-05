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
import { INPUT_DECLINE } from '../../api';
import { genUiCopy, INPUT_COPY, MEETING_COPY, resolveWidgetLocale } from '../copy';
import { BareButton } from '@/components/bare-button';

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
  /**
   * Id of a pending `request_user_input` (human-in-the-loop). When present together with `onSubmitInput`
   * the choice resumes the paused run through this path instead of `onSendPrompt` (a new message/run).
   */
  requestId?: string;
  /** Answers the pending request; the slot id (or `__decline__`) is the answer. Rejection re-enables the card. */
  onSubmitInput?: (requestId: string, answer: string) => Promise<void> | void;
  /** Lifecycle of the request; anything but 'pending' locks the card and shows the matching banner. */
  inputState?: 'pending' | 'expired' | 'cancelled';
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

/** Resolved copy bundles of the picker. */
type MeetingCopy = (typeof MEETING_COPY)[keyof typeof MEETING_COPY];
type InputCopy = (typeof INPUT_COPY)[keyof typeof INPUT_COPY];

/** Border/background classes of a slot button by availability and selection. */
function slotClass(isAvailable: boolean, isSelected: boolean): string {
  if (!isAvailable) return 'cursor-not-allowed border-border bg-muted/30 opacity-60';
  return isSelected
    ? 'border-primary bg-primary/10 text-foreground'
    : 'border-border bg-background hover:bg-muted/50 text-foreground';
}

/** One selectable slot: date, time and the recommended / label / availability badges. */
function SlotButton({ slot, isSelected, isRecommended, disabled, copy, onSelect }: {
  slot: NormalizedMeetingTimeSlot;
  isSelected: boolean;
  isRecommended: boolean;
  disabled: boolean;
  copy: MeetingCopy;
  onSelect: (slot: NormalizedMeetingTimeSlot) => void;
}) {
  const isAvailable = slot.available !== false;
  return (
    <BareButton
      type="button"
      disabled={!isAvailable || disabled}
      aria-pressed={isSelected}
      data-testid={`meeting-slot-${slot.id}`}
      onClick={() => onSelect(slot)}
      className={`flex w-full items-center justify-between gap-3 rounded-lg border p-3 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${slotClass(isAvailable, isSelected)}`}
    >
      <div className="min-w-0">
        <div className="text-sm font-semibold text-foreground">{slot.date}</div>
        <div className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground">
          <ClockIcon className="size-3.5 shrink-0" aria-hidden="true" />
          <span className="tabular-nums">{slot.time}</span>
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
    </BareButton>
  );
}

/** Status banners under the slot list: confirmed, request closed, send failed and declined. */
function MeetingBanners({ confirmedSlot, showClosed, inputState, submitFailed, isDeclined, copy, inputCopy }: {
  confirmedSlot: NormalizedMeetingTimeSlot | null;
  /** Request path only: the request expired/was cancelled without an accepted answer. */
  showClosed: boolean;
  inputState: 'pending' | 'expired' | 'cancelled';
  submitFailed: boolean;
  isDeclined: boolean;
  copy: MeetingCopy;
  inputCopy: InputCopy;
}) {
  return (
    <>
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

      {showClosed && (
        <p
          role="status"
          data-testid="meeting-input-state-banner"
          className="rounded-lg border border-border bg-muted/50 px-3 py-2 text-xs text-muted-foreground"
        >
          {inputState === 'expired' ? inputCopy.expired : inputCopy.cancelled}
        </p>
      )}

      {submitFailed && (
        <p role="alert" data-testid="meeting-input-failed" className="text-xs text-destructive">
          {inputCopy.failed}
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
    </>
  );
}

/**
 * Selection, confirm and decline state of the picker.
 *
 * Two answer paths: a parked `request_user_input` (`requestId` + `onSubmitInput`: locked from click until the server
 * rejects, then retry is allowed, and permanently after acceptance) or a plain follow-up prompt through `onSendPrompt`.
 *
 * @returns State, derived flags and the select/confirm/decline handlers.
 */
function useMeetingSelection(args: {
  normalizedSlots: NormalizedMeetingTimeSlot[];
  copy: MeetingCopy;
  displayTitle: string;
  requestId?: string;
  onSubmitInput?: MeetingTimePickerProps['onSubmitInput'];
  onSendPrompt?: MeetingTimePickerProps['onSendPrompt'];
  inputState: 'pending' | 'expired' | 'cancelled';
}) {
  const { normalizedSlots, copy, displayTitle, requestId, onSubmitInput, onSendPrompt, inputState } = args;
  const viaRequest = Boolean(requestId && onSubmitInput);
  const [selectedSlotId, setSelectedSlotId] = React.useState<string | null>(null);
  const [confirmedSlot, setConfirmedSlot] = React.useState<NormalizedMeetingTimeSlot | null>(null);
  const [isDeclined, setIsDeclined] = React.useState(false);
  const [submitState, setSubmitState] = React.useState<'idle' | 'sending' | 'sent'>('idle');
  const [submitFailed, setSubmitFailed] = React.useState(false);
  const locked = viaRequest && (submitState !== 'idle' || inputState !== 'pending');

  /** Sends the answer through the request path; on failure the card unlocks so the user can retry. */
  const submitAnswer = React.useCallback(
    async (answer: string, onAccepted: () => void) => {
      if (!requestId || !onSubmitInput) return;
      setSubmitState('sending');
      setSubmitFailed(false);
      try {
        await onSubmitInput(requestId, answer);
        setSubmitState('sent');
        onAccepted();
      } catch {
        setSubmitState('idle');
        setSubmitFailed(true);
      }
    },
    [onSubmitInput, requestId],
  );

  const selectedSlot = React.useMemo(
    () => normalizedSlots.find((slot) => slot.id === selectedSlotId) ?? null,
    [normalizedSlots, selectedSlotId],
  );

  /** Selects a candidate available slot prior to final confirmation. */
  const handleSelectSlot = React.useCallback((slot: NormalizedMeetingTimeSlot) => {
    if (slot.available === false) return;
    setIsDeclined(false);
    setConfirmedSlot(null);
    setSelectedSlotId(slot.id);
  }, []);

  /** Confirms the currently selected time slot and dispatches a follow-up prompt. */
  const handleConfirm = React.useCallback(() => {
    if (!selectedSlot || selectedSlot.available === false) return;
    if (viaRequest) {
      void submitAnswer(selectedSlot.id, () => setConfirmedSlot(selectedSlot));
      return;
    }
    setConfirmedSlot(selectedSlot);
    setIsDeclined(false);
    onSendPrompt?.(
      copy.confirmPrompt(displayTitle, selectedSlot.date, selectedSlot.time, selectedSlot.label),
    );
  }, [copy, displayTitle, onSendPrompt, selectedSlot, submitAnswer, viaRequest]);

  /** Declines the proposed slots and asks the assistant for alternative times. */
  const handleDecline = React.useCallback(() => {
    if (viaRequest) {
      void submitAnswer(INPUT_DECLINE, () => {
        setIsDeclined(true);
        setConfirmedSlot(null);
      });
      return;
    }
    setIsDeclined(true);
    setConfirmedSlot(null);
    onSendPrompt?.(copy.declinePrompt(displayTitle));
  }, [copy, displayTitle, onSendPrompt, submitAnswer, viaRequest]);

  return { viaRequest, selectedSlotId, selectedSlot, confirmedSlot, isDeclined, submitState, submitFailed, locked, handleSelectSlot, handleConfirm, handleDecline };
}

/** Card header: calendar glyph, title, optional description and the timezone badge. */
function PickerHeader({ title, description, timezone }: { title: string; description?: string; timezone?: string }) {
  return (
    <CardHeader className="gap-1.5 border-b border-border pb-4">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="inline-flex size-8 items-center justify-center rounded-lg bg-primary/10 text-primary">
            <CalendarIcon className="size-4" aria-hidden="true" />
          </span>
          <div>
            <CardTitle className="text-base font-semibold text-foreground">
              {title}
            </CardTitle>
            {description && (
              <CardDescription className="mt-0.5 text-xs text-muted-foreground">
                {description}
              </CardDescription>
            )}
          </div>
        </div>
        {timezone && (
          <Badge variant="outline" className="tabular-nums text-xs">
            {timezone}
          </Badge>
        )}
      </div>
    </CardHeader>
  );
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
  requestId,
  onSubmitInput,
  inputState = 'pending',
}: MeetingTimePickerProps) {
  const resolvedLocale = resolveWidgetLocale(locale);
  const copy = MEETING_COPY[resolvedLocale];
  const catalog = genUiCopy(resolvedLocale);
  const inputCopy = INPUT_COPY[resolvedLocale];

  const resolvedDuration = meetingDuration ?? duration_minutes;
  const displayTitle = title || reasonForScheduling || copy.defaultTitle;
  const displayDescription =
    description ?? subtitle ?? (title && reasonForScheduling ? reasonForScheduling : undefined);

  const normalizedSlots = React.useMemo(
    () => normalizeSlots(slots, timeSlots, resolvedDuration),
    [resolvedDuration, slots, timeSlots],
  );
  const sel = useMeetingSelection({ normalizedSlots, copy, displayTitle, requestId, onSubmitInput, onSendPrompt, inputState });
  const { selectedSlot, locked } = sel;

  return (
    <Card
      data-testid="generative-meeting-time-picker"
      className="my-2 w-full max-w-md border-border bg-surface text-foreground"
    >
      <PickerHeader title={displayTitle} description={displayDescription} timezone={timezone} />

      <CardContent className="space-y-2.5 pt-4">
        <div role="group" aria-label={displayTitle} className="space-y-2">
          {normalizedSlots.map((slot) => (
            <SlotButton
              key={slot.id}
              slot={slot}
              isSelected={sel.selectedSlotId === slot.id}
              isRecommended={slot.recommended === true || (Boolean(selected_slot_id) && slot.id === selected_slot_id)}
              disabled={locked}
              copy={copy}
              onSelect={sel.handleSelectSlot}
            />
          ))}
        </div>

        <MeetingBanners
          confirmedSlot={sel.confirmedSlot}
          showClosed={sel.viaRequest && inputState !== 'pending' && sel.submitState !== 'sent'}
          inputState={inputState}
          submitFailed={sel.submitFailed}
          isDeclined={sel.isDeclined}
          copy={copy}
          inputCopy={inputCopy}
        />
      </CardContent>

      <CardFooter className="flex flex-wrap items-center justify-end gap-2 border-t border-border pt-4">
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={sel.handleDecline}
          disabled={locked}
          data-testid="meeting-decline-button"
        >
          {copy.suggestAlternative}
        </Button>
        <Button
          type="button"
          variant="default"
          size="sm"
          disabled={!selectedSlot || selectedSlot.available === false || locked}
          onClick={sel.handleConfirm}
          data-testid="meeting-confirm-button"
        >
          {catalog.genUiTimeConfirm}
        </Button>
      </CardFooter>
    </Card>
  );
}
