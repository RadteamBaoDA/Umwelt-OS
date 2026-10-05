'use client';

import React from 'react';
import type { GadgetInstance } from '../api';
import { MetricsChart, type MetricsChartProps } from './metrics-chart';

/** Props for the FinanceChart gadget component. */
export interface FinanceChartProps extends Omit<MetricsChartProps, 'instance'> {
  /** Gadget instance configuration projection. */
  instance: GadgetInstance;
}

/**
 * Standard Finance Chart gadget template.
 * Specializes MetricsChart for equities, crypto, commodities, and index series.
 *
 * @param props Gadget instance configuration and chart options.
 * @returns Accessible finance chart component.
 */
export function FinanceChart(props: FinanceChartProps) {
  return <MetricsChart {...props} />;
}
