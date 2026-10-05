'use client';

import { ChevronLeft, ChevronRight } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { addDays, useSelectedDay } from './selected-day';

/**
 * Day navigation for day-aware gadgets: previous/next day, a native date input and a jump to today.
 * Changing the day only changes the shared selected day; it never edits an existing conversation.
 */
export function DateSelector() {
  const t = useTranslations('daily');
  const { date, today, setDate } = useSelectedDay();
  return (
    <div role="group" aria-label={t('dateSelector')} className="flex flex-wrap items-center gap-1.5">
      <Button type="button" size="icon" variant="outline" aria-label={t('previousDay')} onClick={() => setDate(addDays(date, -1))}>
        <ChevronLeft className="h-4 w-4" aria-hidden />
      </Button>
      <Input
        type="date"
        aria-label={t('selectedDate')}
        value={date}
        onChange={(event) => event.target.value && setDate(event.target.value)}
        className="w-auto"
      />
      <Button type="button" size="icon" variant="outline" aria-label={t('nextDay')} onClick={() => setDate(addDays(date, 1))}>
        <ChevronRight className="h-4 w-4" aria-hidden />
      </Button>
      <Button type="button" size="sm" variant="secondary" disabled={date === today} onClick={() => setDate(today)}>
        {t('today')}
      </Button>
    </div>
  );
}
