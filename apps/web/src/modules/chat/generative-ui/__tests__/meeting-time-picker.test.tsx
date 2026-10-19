import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { MeetingTimePicker, type MeetingTimeSlot } from '../components/meeting-time-picker';

const SAMPLE_SLOTS: MeetingTimeSlot[] = [
  {
    id: 'slot-a',
    date: 'Tuesday, Oct 20',
    time: '09:30 AM - 10:00 AM',
    label: '30 min',
    available: true,
  },
  {
    id: 'slot-b',
    date: 'Wednesday, Oct 21',
    time: '02:00 PM - 02:30 PM',
    label: '30 min',
    available: false,
  },
];

describe('MeetingTimePicker', () => {
  it('allows selecting an available slot, confirming selection, and dispatching onSendPrompt', async () => {
    const user = userEvent.setup();
    const onSendPrompt = vi.fn();

    render(
      <MeetingTimePicker
        title="Architecture Sync"
        description="Select a 30-minute review window"
        timezone="UTC"
        slots={SAMPLE_SLOTS}
        onSendPrompt={onSendPrompt}
      />,
    );

    const confirmButton = screen.getByTestId('meeting-confirm-button');
    expect(confirmButton).toBeDisabled();

    // Unavailable slot is disabled
    const unavailableSlot = screen.getByTestId('meeting-slot-slot-b');
    expect(unavailableSlot).toBeDisabled();

    // Select available slot
    const availableSlot = screen.getByTestId('meeting-slot-slot-a');
    await user.click(availableSlot);
    expect(availableSlot).toHaveAttribute('aria-pressed', 'true');
    expect(confirmButton).not.toBeDisabled();

    // Confirm selection
    await user.click(confirmButton);
    expect(screen.getByTestId('meeting-confirmed-banner')).toHaveTextContent(
      'Confirmed for Tuesday, Oct 20 at 09:30 AM - 10:00 AM.',
    );
    expect(onSendPrompt).toHaveBeenCalledTimes(1);
    expect(onSendPrompt.mock.calls[0][0]).toContain('Tuesday, Oct 20');
    expect(onSendPrompt.mock.calls[0][0]).toContain('09:30 AM - 10:00 AM');
  });

  it('supports requesting alternative times and Vietnamese localization', async () => {
    const user = userEvent.setup();
    const onSendPrompt = vi.fn();

    render(
      <MeetingTimePicker
        title="Họp Kiến trúc"
        slots={SAMPLE_SLOTS}
        locale="vi-vi"
        onSendPrompt={onSendPrompt}
      />,
    );

    expect(screen.getByTestId('meeting-confirm-button')).toHaveTextContent('Xác nhận khung giờ');

    await user.click(screen.getByTestId('meeting-decline-button'));
    expect(screen.getByTestId('meeting-declined-banner')).toBeInTheDocument();
    expect(onSendPrompt).toHaveBeenCalledTimes(1);
    expect(onSendPrompt.mock.calls[0][0]).toContain('Họp Kiến trúc');
  });

  it('normalizes backend Python TimeSlot fields (start_time, end_time, recommended, subtitle, duration_minutes)', async () => {
    const user = userEvent.setup();
    const onSendPrompt = vi.fn();

    render(
      <MeetingTimePicker
        title="Schedule: Design Review"
        subtitle="Select a 45-minute window (UTC)"
        duration_minutes={45}
        timezone="UTC"
        selected_slot_id="slot-1"
        slots={[
          {
            id: 'slot-1',
            date: '2026-10-20',
            start_time: '09:30',
            end_time: '10:15',
            available: true,
            recommended: true,
          },
        ]}
        onSendPrompt={onSendPrompt}
      />,
    );

    expect(screen.getByText('Select a 45-minute window (UTC)')).toBeInTheDocument();
    expect(screen.getByText('09:30 - 10:15')).toBeInTheDocument();
    expect(screen.getByText('Recommended')).toBeInTheDocument();
    expect(screen.getByText('45 min')).toBeInTheDocument();

    await user.click(screen.getByTestId('meeting-slot-slot-1'));
    await user.click(screen.getByTestId('meeting-confirm-button'));
    expect(onSendPrompt).toHaveBeenCalledTimes(1);
    expect(onSendPrompt.mock.calls[0][0]).toContain('09:30 - 10:15');
  });
});

