import { renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { useDashboardDeepLink } from './use-dashboard-deep-link';

const replace = vi.fn();
let search = new URLSearchParams();

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace }),
  usePathname: () => '/dashboard',
  useSearchParams: () => search,
}));

describe('useDashboardDeepLink', () => {
  beforeEach(() => {
    replace.mockReset();
    search = new URLSearchParams('addTemplate=feed&source=src_1');
  });

  it('keeps the intent in the URL until the dashboard is ready, then delivers and strips it', () => {
    const onAddTemplate = vi.fn();
    const { rerender } = renderHook(({ ready }) => useDashboardDeepLink({ ready, onPreset: vi.fn(), onAddTemplate }), {
      initialProps: { ready: false },
    });
    // A page that mounts before the workspace resolves (and remounts after) must not lose the link.
    expect(replace).not.toHaveBeenCalled();
    expect(onAddTemplate).not.toHaveBeenCalled();
    rerender({ ready: true });
    expect(onAddTemplate).toHaveBeenCalledWith({ templateId: 'feed', sourceId: 'src_1' });
    expect(replace).toHaveBeenCalledWith('/dashboard');
  });
});
