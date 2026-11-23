import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { getCatalog } from '@locales';
import { AskRecordButton } from './ask-record-button';

const mocks = vi.hoisted(() => ({ isOwner: true, openDrawer: vi.fn() }));
vi.mock('@/core/app-shell/chat-controller', () => ({ useChatController: () => ({ openDrawer: mocks.openDrawer }) }));
vi.mock('@/core/workspace-context', () => ({ useWorkspace: () => ({ isOwner: mocks.isOwner }) }));

const record = { sourceId: 's1', documentId: 'd1', documentVersionId: 'v1' };

/** Renders the shared button with the dashboard translations used by every gadget row. */
function renderButton(props: { allowNonOwner?: boolean } = {}) {
  return render(
    <NextIntlClientProvider locale="en-US" messages={{ dashboard: getCatalog('en-us', 'dashboard') }}>
      <AskRecordButton record={record} {...props} />
    </NextIntlClientProvider>,
  );
}

describe('AskRecordButton', () => {
  beforeEach(() => {
    mocks.isOwner = true;
    mocks.openDrawer.mockReset();
  });

  it('opens an anchored one-item selection for the record when the owner clicks it', async () => {
    renderButton();
    await userEvent.click(screen.getByRole('button', { name: 'Ask AI' }));
    expect(mocks.openDrawer).toHaveBeenCalledWith({
      context: { kind: 'selection', anchor: true, items: [record] },
    });
  });

  it('renders nothing for a non-owner', () => {
    mocks.isOwner = false;
    renderButton();
    expect(screen.queryByRole('button', { name: 'Ask AI' })).toBeNull();
  });

  it('renders for a non-owner when the caller allows non-owner access', () => {
    mocks.isOwner = false;
    renderButton({ allowNonOwner: true });
    expect(screen.getByRole('button', { name: 'Ask AI' })).toBeTruthy();
  });
});
