import { describe, expect, it, vi } from 'vitest';
import { settleWidgetPrompt } from '../bridge/widget-prompt';

describe('settleWidgetPrompt', () => {
  it('rejects without sending when busy', async () => {
    const send = vi.fn().mockResolvedValue(true);
    await expect(settleWidgetPrompt(true, send, 'busy', 'failed')).rejects.toThrow('busy');
    expect(send).not.toHaveBeenCalled();
  });

  it('rejects when the send was not accepted', async () => {
    await expect(settleWidgetPrompt(false, async () => false, 'busy', 'failed')).rejects.toThrow('failed');
  });

  it('resolves when the send was accepted', async () => {
    await expect(settleWidgetPrompt(false, async () => true, 'busy', 'failed')).resolves.toBeUndefined();
  });
});
