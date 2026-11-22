import { Suspense } from 'react';
import { ScheduledTasksWorkspace } from '@/modules/automations';

/** Settings › Scheduled tasks: owner list, templates and the Daily brief editor; Suspense lets `?edit=&run=` read search params during static rendering. */
export default function ScheduledTasksPage() {
  return (
    <Suspense fallback={null}>
      <ScheduledTasksWorkspace />
    </Suspense>
  );
}
