import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider } from 'next-intl';
import { describe, expect, it } from 'vitest';
import { Tabs } from '@/components/ui/tabs';
import { dashboardMessages } from '@/core/messages/dashboard';
import type { DashboardGroup } from './api';
import { GroupTabs } from './dashboard-panels';

const group = (id: string, name: string, position: number): DashboardGroup =>
  ({ id, name, position, is_overview: false }) as DashboardGroup;

describe('GroupTabs', () => {
  it('marks only updated groups with an accessible "Updated" label', () => {
    render(
      <NextIntlClientProvider locale="en-US" messages={{ dashboard: dashboardMessages['en-us'] }}>
        <Tabs value="a">
          <GroupTabs groups={[group('a', 'News', 0), group('b', 'Work', 1)]} onAdd={() => undefined} canAdd={false} updatedGroupIds={new Set(['b'])} />
        </Tabs>
      </NextIntlClientProvider>,
    );
    expect(screen.getAllByText('Updated')).toHaveLength(1);
    expect(screen.getByRole('tab', { name: /Work/ })).toHaveTextContent('Updated');
    expect(screen.getByRole('tab', { name: /News/ })).not.toHaveTextContent('Updated');
  });
});
