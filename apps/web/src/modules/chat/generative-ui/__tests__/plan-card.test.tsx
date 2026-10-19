import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { PlanCard, type PlanStep } from '../components/plan-card';
import { ToolRendering } from '../components/tool-rendering';

const SAMPLE_STEPS: PlanStep[] = [
  {
    id: 'step-1',
    label: 'Retrieve workspace documents',
    description: 'Query hybrid vector and lexical indices.',
    status: 'completed',
  },
  {
    id: 'step-2',
    label: 'Cross-reference entity citations',
    description: 'Verify chunk version provenance.',
    status: 'in_progress',
  },
  {
    id: 'step-3',
    label: 'Synthesize executive summary',
    status: 'pending',
  },
];

describe('PlanCard', () => {
  it('renders plan title, description, steps, and initial progress counter', () => {
    render(
      <PlanCard
        title="Weekly Knowledge Audit"
        description="Three-stage grounded verification workflow."
        steps={SAMPLE_STEPS}
      />,
    );

    expect(screen.getByText('Weekly Knowledge Audit')).toBeInTheDocument();
    expect(screen.getByText('Three-stage grounded verification workflow.')).toBeInTheDocument();
    expect(screen.getByTestId('plan-progress-counter')).toHaveTextContent('1 / 3 completed');
    expect(screen.getByText('Retrieve workspace documents')).toBeInTheDocument();
    expect(screen.getByText('Cross-reference entity citations')).toBeInTheDocument();
    expect(screen.getByText('Synthesize executive summary')).toBeInTheDocument();
  });

  it('toggles step completion interactively and updates the progress counter', async () => {
    const user = userEvent.setup();
    const onStepToggle = vi.fn();

    render(
      <PlanCard
        title="Weekly Knowledge Audit"
        steps={SAMPLE_STEPS}
        onStepToggle={onStepToggle}
      />,
    );

    const step3Checkbox = screen.getByRole('checkbox', { name: 'Synthesize executive summary' });
    expect(step3Checkbox).not.toBeChecked();

    await user.click(step3Checkbox);
    expect(step3Checkbox).toBeChecked();
    expect(onStepToggle).toHaveBeenCalledWith('step-3', 'completed');
    expect(screen.getByTestId('plan-progress-counter')).toHaveTextContent('2 / 3 completed');

    // Toggle a completed step back to pending
    const step1Checkbox = screen.getByRole('checkbox', { name: 'Retrieve workspace documents' });
    await user.click(step1Checkbox);
    expect(step1Checkbox).not.toBeChecked();
    expect(onStepToggle).toHaveBeenCalledWith('step-1', 'pending');
    expect(screen.getByTestId('plan-progress-counter')).toHaveTextContent('1 / 3 completed');
  });

  it('dispatches follow-up prompts when Approve or Refine buttons are clicked', async () => {
    const user = userEvent.setup();
    const onSendPrompt = vi.fn();

    render(
      <PlanCard
        title="Weekly Knowledge Audit"
        steps={SAMPLE_STEPS}
        onSendPrompt={onSendPrompt}
      />,
    );

    await user.click(screen.getByTestId('plan-approve-button'));
    expect(onSendPrompt).toHaveBeenCalledTimes(1);
    expect(onSendPrompt.mock.calls[0][0]).toContain('Weekly Knowledge Audit');
    expect(screen.getByTestId('plan-approved-banner')).toBeInTheDocument();

    await user.click(screen.getByTestId('plan-refine-button'));
    expect(onSendPrompt).toHaveBeenCalledTimes(2);
    expect(onSendPrompt.mock.calls[1][0]).toContain('Weekly Knowledge Audit');
  });

  it('renders localized Vietnamese labels when locale is vi-vi', () => {
    render(
      <PlanCard
        title="Kế hoạch Rà soát"
        steps={SAMPLE_STEPS}
        locale="vi-vi"
      />,
    );

    expect(screen.getByTestId('plan-progress-counter')).toHaveTextContent('1 / 3 hoàn thành');
    expect(screen.getByTestId('plan-approve-button')).toHaveTextContent(
      'Phê duyệt & thực thi kế hoạch',
    );
    expect(screen.getByTestId('plan-refine-button')).toHaveTextContent('Điều chỉnh kế hoạch');
  });

  it('dispatches structured payloads through ToolRendering', () => {
    render(
      <ToolRendering
        payload={{
          kind: 'plan',
          plan: {
            title: 'Dispatched Plan',
            steps: SAMPLE_STEPS,
          },
        }}
      />,
    );

    expect(screen.getByText('Dispatched Plan')).toBeInTheDocument();
  });

  it('normalizes backend Python PlanPayload fields (step.title, step.emoji, key_elements)', () => {
    render(
      <ToolRendering
        payload={
          {
            kind: 'plan',
            surface_id: 'plan-surface-123',
            title: 'Backend Emitted Plan',
            description: 'Grounded execution plan',
            approach: 'Grounded workspace synthesis',
            technology: 'Umwelt-OS Hybrid Search',
            key_elements: ['Hybrid Retrieval', 'Actionable Steps'],
            steps: [
              {
                id: 'step-1',
                title: 'Inspect indexed workspace sources',
                description: 'Verify document citations',
                emoji: '🔎',
                status: 'completed',
              },
              {
                id: 'step-2',
                title: 'Execute verification pass',
                description: 'Confirm schema alignment',
                emoji: '✅',
                status: 'pending',
              },
            ],
          } as unknown as Parameters<typeof ToolRendering>[0]['payload']
        }
      />,
    );

    expect(screen.getByText('Backend Emitted Plan')).toBeInTheDocument();
    expect(screen.getByText('Inspect indexed workspace sources')).toBeInTheDocument();
    expect(screen.getByText('Execute verification pass')).toBeInTheDocument();
    expect(screen.getByText('Hybrid Retrieval')).toBeInTheDocument();
    expect(screen.getByTestId('plan-progress-counter')).toHaveTextContent('1 / 2 completed');
  });
});
