'use client';

import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Card, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card';
import { SettingsPageHeader } from '@/components/settings-layout';

/**
 * Data sources > Personal data permissions tab body.
 * No dedicated backend exists, so it summarizes what each linked area controls and links to the real settings pages (read-only, no writes).
 */
export function PersonalDataPermissionsPage() {
  const t = useTranslations('shell');
  const areas = [
    {
      id: 'memory',
      title: t('permissions.memoryTitle'),
      body: t('permissions.memoryBody'),
      href: '/settings/memory',
      action: t('open.memoryPrivacy'),
    },
    { id: 'mcp', title: t('permissions.mcpTitle'), body: t('permissions.mcpBody'), href: '/settings/sources/mcp', action: t('open.mcp') },
  ];
  return (
    <div className="flex flex-col gap-5">
      <SettingsPageHeader eyebrow={t('settings')} title={t('permissions.unavailableTitle')} description={t('permissions.unavailable')} />
      <div className="grid gap-4 md:grid-cols-2">
        {areas.map((area) => (
          <Card key={area.id}>
            <CardHeader>
              <CardTitle>{area.title}</CardTitle>
              <CardDescription>{area.body}</CardDescription>
            </CardHeader>
            <CardFooter>
              <Button asChild variant="outline">
                <Link href={area.href}>{area.action}</Link>
              </Button>
            </CardFooter>
          </Card>
        ))}
      </div>
    </div>
  );
}
