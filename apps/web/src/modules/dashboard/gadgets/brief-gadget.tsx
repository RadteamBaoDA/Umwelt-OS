'use client';

import React from 'react';
import type { GadgetInstance } from '../api';
import { DailyBrief } from '../daily-brief';

/** Props for the daily brief gadget. */
export interface BriefGadgetProps {
  /** The gadget instance with its configuration projection. */
  instance: GadgetInstance;
}

/**
 * Daily brief gadget: shows the selected day's saved, cited brief revision and the current-record
 * summaries (tasks, goals, stories, events). Content comes from `/api/v1/context/daily`; nothing is mocked.
 *
 * @param _props Gadget instance (the brief has no per-instance configuration yet).
 * @returns The daily brief panel.
 */
export function BriefGadget(_props: BriefGadgetProps) {
  return <DailyBrief />;
}
