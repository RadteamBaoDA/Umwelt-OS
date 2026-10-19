import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import type { InputRequest } from '../../api';
import { MeetingTimePicker } from '../components/meeting-time-picker';
import { UserInputRequest } from '../components/user-input-request';

const SLOTS = [
  { id: 's1', label: '30 min', date: '2026-10-20', start_time: '10:00', end_time: '10:30', available: true },
  { id: 's2', label: '30 min', date: '2026-10-21', start_time: '11:00', end_time: '11:30', available: true },
];

const TIME_REQUEST: InputRequest = {
  request_id: 'req-1',
  kind: 'time_slots',
  prompt: 'When can we meet?',
  options: [],
  slots: SLOTS,
  expires_at: '2026-10-20T09:05:00+00:00',
};

describe('MeetingTimePicker request path', () => {
  it('answers through onSubmitInput with the slot id and never calls onSendPrompt', async () => {
    const user = userEvent.setup();
    const onSubmitInput = vi.fn().mockResolvedValue(undefined);
    const onSendPrompt = vi.fn();
    render(
      <MeetingTimePicker title="Sync" slots={SLOTS} requestId="req-1" onSubmitInput={onSubmitInput} onSendPrompt={onSendPrompt} />,
    );
    await user.click(screen.getByTestId('meeting-slot-s2'));
    await user.click(screen.getByTestId('meeting-confirm-button'));
    expect(onSubmitInput).toHaveBeenCalledWith('req-1', 's2');
    expect(onSendPrompt).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByTestId('meeting-confirmed-banner')).toBeInTheDocument());
    // Disabled after answering: no double submit.
    expect(screen.getByTestId('meeting-confirm-button')).toBeDisabled();
    expect(screen.getByTestId('meeting-slot-s1')).toBeDisabled();
  });

  it('decline sends the reserved marker; a rejected submit unlocks the card with an error', async () => {
    const user = userEvent.setup();
    const onSubmitInput = vi.fn().mockRejectedValueOnce(new Error('409')).mockResolvedValue(undefined);
    render(<MeetingTimePicker slots={SLOTS} requestId="req-1" onSubmitInput={onSubmitInput} />);
    await user.click(screen.getByTestId('meeting-decline-button'));
    expect(onSubmitInput).toHaveBeenCalledWith('req-1', '__decline__');
    await waitFor(() => expect(screen.getByTestId('meeting-input-failed')).toBeInTheDocument());
    expect(screen.getByTestId('meeting-decline-button')).not.toBeDisabled();
  });

  it('shows the expired state and locks the card', () => {
    render(<MeetingTimePicker slots={SLOTS} requestId="req-1" onSubmitInput={vi.fn()} inputState="expired" />);
    expect(screen.getByTestId('meeting-input-state-banner')).toHaveTextContent('expired');
    expect(screen.getByTestId('meeting-slot-s1')).toBeDisabled();
    expect(screen.getByTestId('meeting-decline-button')).toBeDisabled();
  });

  it('localizes the cancelled banner in Vietnamese', () => {
    render(
      <MeetingTimePicker slots={SLOTS} locale="vi-vi" requestId="req-1" onSubmitInput={vi.fn()} inputState="cancelled" />,
    );
    expect(screen.getByTestId('meeting-input-state-banner')).toHaveTextContent('đã bị hủy');
  });
});

describe('UserInputRequest', () => {
  it('renders the picker for time_slots and submits the slot id', async () => {
    const user = userEvent.setup();
    const onSubmitInput = vi.fn().mockResolvedValue(undefined);
    render(<UserInputRequest request={TIME_REQUEST} state="pending" onSubmitInput={onSubmitInput} />);
    expect(screen.getByText('When can we meet?')).toBeInTheDocument();
    await user.click(screen.getByTestId('meeting-slot-s1'));
    await user.click(screen.getByTestId('meeting-confirm-button'));
    expect(onSubmitInput).toHaveBeenCalledWith('req-1', 's1');
  });

  it('choice: one click submits the option id, then the card is disabled', async () => {
    const user = userEvent.setup();
    const onSubmitInput = vi.fn().mockResolvedValue(undefined);
    const request: InputRequest = {
      ...TIME_REQUEST,
      kind: 'choice',
      slots: [],
      options: [
        { id: 'a', label: 'Alpha' },
        { id: 'b', label: 'Beta' },
      ],
    };
    render(<UserInputRequest request={request} state="pending" onSubmitInput={onSubmitInput} />);
    await user.click(screen.getByTestId('input-option-b'));
    expect(onSubmitInput).toHaveBeenCalledWith('req-1', 'b');
    expect(screen.getByTestId('input-option-a')).toBeDisabled();
  });

  it('text: submits trimmed text; answered and expired states are locked with a banner (vi)', async () => {
    const user = userEvent.setup();
    const onSubmitInput = vi.fn().mockResolvedValue(undefined);
    const request: InputRequest = { ...TIME_REQUEST, kind: 'text', slots: [] };
    const { rerender } = render(
      <UserInputRequest request={request} state="pending" locale="vi-vi" onSubmitInput={onSubmitInput} />,
    );
    await user.type(screen.getByTestId('input-text'), '  Ada ');
    await user.click(screen.getByTestId('input-submit'));
    expect(onSubmitInput).toHaveBeenCalledWith('req-1', 'Ada');
    rerender(
      <UserInputRequest request={request} state="answered" answeredLabel="Ada" locale="vi-vi" onSubmitInput={onSubmitInput} />,
    );
    expect(screen.getByTestId('input-state-banner')).toHaveTextContent('Đã gửi câu trả lời: Ada');
    rerender(<UserInputRequest request={request} state="expired" locale="vi-vi" onSubmitInput={onSubmitInput} />);
    expect(screen.getByTestId('input-state-banner')).toHaveTextContent('hết hạn');
    expect(screen.getByTestId('input-text')).toBeDisabled();
  });
});
