'use client';

import * as React from 'react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import type { AppLocaleId } from '@/core/i18n';
import type { InputRequest } from '../../api';
import { INPUT_COPY, resolveWidgetLocale } from '../copy';
import { MeetingTimePicker } from './meeting-time-picker';

/** Lifecycle of one request as the transcript sees it. */
export type InputRequestState = 'pending' | 'answered' | 'expired' | 'cancelled';

/** Props of {@link UserInputRequest}. */
export interface UserInputRequestProps {
  /** The `message.input.request` payload. */
  request: InputRequest;
  /** Current lifecycle; only 'pending' accepts input. */
  state: InputRequestState;
  /** Human-readable answer shown once answered (from `message.input.resolved`). */
  answeredLabel?: string;
  /** Application locale ('en-us' | 'vi-vi'). */
  locale?: AppLocaleId;
  /** Sends the answer to the parked run; a rejection re-enables the card with an error. */
  onSubmitInput: (requestId: string, answer: string) => Promise<void> | void;
}

/**
 * Renders a human-in-the-loop `request_user_input` as the right control: the meeting time picker for
 * `time_slots` (answering through the request path, not a new message), option buttons for `choice`, a text
 * field for `text`. After an answer is accepted, or once the request expired or was cancelled, the card is
 * locked and shows the outcome.
 *
 * @param props - Request payload, lifecycle state and the submit callback.
 * @returns The request card.
 */
export function UserInputRequest({
  request,
  state,
  answeredLabel,
  locale,
  onSubmitInput,
}: UserInputRequestProps) {
  const copy = INPUT_COPY[resolveWidgetLocale(locale)];
  const [sending, setSending] = React.useState(false);
  const [failed, setFailed] = React.useState(false);
  const [text, setText] = React.useState('');
  const locked = sending || state !== 'pending';

  /** Submits one answer, locking while in flight and surfacing a retryable error on rejection. */
  const submit = React.useCallback(
    async (answer: string) => {
      setSending(true);
      setFailed(false);
      try {
        await onSubmitInput(request.request_id, answer);
      } catch {
        setSending(false);
        setFailed(true);
      }
    },
    [onSubmitInput, request.request_id],
  );

  if (request.kind === 'time_slots') {
    return (
      <MeetingTimePicker
        title={request.prompt}
        slots={request.slots}
        locale={locale}
        requestId={request.request_id}
        onSubmitInput={onSubmitInput}
        inputState={state === 'expired' || state === 'cancelled' ? state : 'pending'}
      />
    );
  }

  const banner =
    state === 'expired'
      ? copy.expired
      : state === 'cancelled'
        ? copy.cancelled
        : state === 'answered'
          ? copy.answered(answeredLabel ?? '')
          : null;

  return (
    <Card data-testid="user-input-request" className="my-2 w-full max-w-md border-border bg-surface shadow-sm">
      <CardHeader>
        <CardTitle className="text-base font-semibold text-foreground">{request.prompt}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {request.kind === 'choice' ? (
          <div role="group" aria-label={request.prompt} className="flex flex-wrap gap-2">
            {request.options.map((option) => (
              <Button
                key={option.id}
                type="button"
                variant="outline"
                size="sm"
                disabled={locked}
                data-testid={`input-option-${option.id}`}
                onClick={() => void submit(option.id)}
              >
                {option.label}
              </Button>
            ))}
          </div>
        ) : (
          <form
            className="flex gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              if (text.trim()) void submit(text.trim());
            }}
          >
            <Input
              value={text}
              maxLength={2000}
              disabled={locked}
              placeholder={copy.textPlaceholder}
              aria-label={request.prompt}
              data-testid="input-text"
              onChange={(event) => setText(event.target.value)}
            />
            <Button type="submit" size="sm" disabled={locked || !text.trim()} data-testid="input-submit">
              {copy.submit}
            </Button>
          </form>
        )}
        {failed && (
          <p role="alert" className="text-xs text-destructive" data-testid="input-failed">
            {copy.failed}
          </p>
        )}
        {banner && (
          <p role="status" className="text-xs text-muted-foreground" data-testid="input-state-banner">
            {banner}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
