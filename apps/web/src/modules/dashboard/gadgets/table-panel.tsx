'use client';

import React from 'react';
import type { GadgetInstance } from '../api';
import { WatchlistGadget, type WatchlistGadgetProps } from './watchlist-gadget';

/** Props for the TablePanel gadget component. */
export interface TablePanelProps extends Omit<WatchlistGadgetProps, 'instance'> {
  /** Gadget instance configuration projection. */
  instance: GadgetInstance;
}

/**
 * Standard Table Panel gadget template.
 * Specializes WatchlistGadget for tabular data grids and condition monitoring.
 *
 * @param props Gadget instance configuration and table props.
 * @returns Accessible table panel component.
 */
export function TablePanel(props: TablePanelProps) {
  return <WatchlistGadget {...props} />;
}
