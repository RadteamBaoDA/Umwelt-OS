import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { InputForm } from '../components/input-form';
import { inputFormPayloadSchema } from '../components/payload-schemas';
import { ToolRendering } from '../components/tool-rendering';
import type { GenerativeUIPayload } from '../sandbox/schema';

afterEach(cleanup);

const FIELDS = inputFormPayloadSchema.parse({
  title: 't',
  fields: [
    { id: 'city', label: 'City', type: 'text', required: true, maxlength: 5 },
    { id: 'days', label: 'Days', type: 'number', min: 1, max: 14 },
    { id: 'pace', label: 'Pace', type: 'select', options: ['Slow', 'Fast'] },
    { id: 'pets', label: 'Pets', type: 'checkbox' },
  ],
}).fields;

describe('InputForm', () => {
  it('shows accessible validation errors and does not submit', async () => {
    const user = userEvent.setup();
    const onSendPrompt = vi.fn();
    render(<InputForm title="Trip" fields={FIELDS} locale="en-us" onSendPrompt={onSendPrompt} />);
    await user.click(screen.getByTestId('form-submit-button'));
    expect(screen.getByRole('alert')).toHaveTextContent('City is required.');
    expect(screen.getByLabelText(/City/)).toHaveAttribute('aria-invalid', 'true');
    await user.type(screen.getByLabelText(/City/), 'Paris');
    await user.type(screen.getByLabelText(/Days/), '20');
    await user.click(screen.getByTestId('form-submit-button'));
    expect(screen.getByRole('alert')).toHaveTextContent('Days must be at most 14.');
    expect(onSendPrompt).not.toHaveBeenCalled();
  });

  it('sends a deterministic message then locks in the submitted state', async () => {
    const user = userEvent.setup();
    const onSendPrompt = vi.fn().mockResolvedValue(undefined);
    render(<InputForm title="Trip" fields={FIELDS} locale="en-us" onSendPrompt={onSendPrompt} />);
    await user.type(screen.getByLabelText(/City/), 'Hue');
    await user.type(screen.getByLabelText(/Days/), '3');
    await user.click(screen.getByRole('combobox'));
    await user.click(screen.getByRole('option', { name: 'Fast' }));
    await user.click(screen.getByRole('checkbox'));
    await user.click(screen.getByTestId('form-submit-button'));
    expect(onSendPrompt).toHaveBeenCalledWith('Form submitted: Trip\n- City: Hue\n- Days: 3\n- Pace: Fast\n- Pets: Yes');
    expect(await screen.findByTestId('form-submitted-banner')).toBeInTheDocument();
    expect(screen.getByTestId('form-submit-button')).toBeDisabled();
    expect(screen.getByLabelText(/City/)).toBeDisabled();
  });

  it('shows a retryable error when the bridge rejects', async () => {
    const user = userEvent.setup();
    const onSendPrompt = vi.fn().mockRejectedValueOnce(new Error('busy')).mockResolvedValueOnce(undefined);
    render(<InputForm title="Trip" fields={FIELDS} locale="en-us" onSendPrompt={onSendPrompt} />);
    await user.type(screen.getByLabelText(/City/), 'Hue');
    await user.click(screen.getByTestId('form-submit-button'));
    expect(await screen.findByTestId('form-send-error')).toBeInTheDocument();
    expect(screen.getByTestId('form-submit-button')).toBeEnabled();
    await user.click(screen.getByTestId('form-submit-button'));
    expect(await screen.findByTestId('form-submitted-banner')).toBeInTheDocument();
    expect(onSendPrompt).toHaveBeenCalledTimes(2);
  });

  it('localizes copy and the prompt header in Vietnamese', async () => {
    const user = userEvent.setup();
    const onSendPrompt = vi.fn();
    render(<InputForm title="Chuyến đi" fields={FIELDS} locale="vi-vi" onSendPrompt={onSendPrompt} />);
    await user.click(screen.getByTestId('form-submit-button'));
    expect(screen.getByRole('alert')).toHaveTextContent('City là bắt buộc.');
    await user.type(screen.getByLabelText(/City/), 'Hue');
    await user.click(screen.getByTestId('form-submit-button'));
    expect(onSendPrompt.mock.calls[0][0]).toMatch(/^Đã gửi biểu mẫu: Chuyến đi\n- City: Hue\n- Days: \(trống\)/);
  });
});

describe('ToolRendering form kind', () => {
  it('renders the backend contract fixture through the zod schema', () => {
    const fixture = JSON.parse(
      readFileSync(resolve(__dirname, '../../../../../../../tests/fixtures/genui/form_payload.json'), 'utf-8'),
    ) as GenerativeUIPayload;
    expect(inputFormPayloadSchema.safeParse(fixture.form).success).toBe(true);
    render(<ToolRendering payload={fixture} locale="en-us" />);
    expect(screen.getByTestId('generative-input-form')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument();
  });

  it('shows the invalid alert for a completed bad payload but nothing while streaming', () => {
    const bad = { kind: 'form', status: 'completed', form: { title: 'x', fields: [] } } as unknown as GenerativeUIPayload;
    const { rerender } = render(<ToolRendering payload={bad} locale="en-us" />);
    expect(screen.getByTestId('generative-ui-invalid-payload')).toBeInTheDocument();
    rerender(<ToolRendering payload={{ ...bad, status: 'streaming' }} locale="en-us" />);
    expect(screen.queryByTestId('generative-ui-invalid-payload')).toBeNull();
  });
});
