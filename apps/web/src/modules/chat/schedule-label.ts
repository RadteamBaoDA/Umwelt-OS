import { useTranslations } from 'next-intl';
import { describeSchedule } from '@/modules/automations/public-api';

/**
 * Returns a formatter for a scheduled task's cron as owner-facing text ("Every day at 07:00" / "Weekdays at 07:00"),
 * falling back to the raw cron for any other expression. Reads only the automations helpers and catalog, so chat
 * surfaces do not pull the Scheduled tasks page and editors into the chat bundle.
 */
export function useScheduleLabel(): (cron: string) => string {
  const t = useTranslations('automations');
  return (cron) => {
    const schedule = describeSchedule(cron);
    if (!schedule) return cron;
    return schedule.days === 'daily'
      ? t('scheduledTasks.schedule.daily', { time: schedule.time })
      : t('scheduledTasks.schedule.weekdays', { time: schedule.time });
  };
}
