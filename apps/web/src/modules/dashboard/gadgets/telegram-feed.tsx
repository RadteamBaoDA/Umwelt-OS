'use client';

import React from 'react';
import type { GadgetInstance } from '../api';
import { FeedGadget, type FeedGadgetProps } from './feed-gadget';

/** Props for the TelegramFeed gadget component. */
export interface TelegramFeedProps extends Omit<FeedGadgetProps, 'instance'> {
  /** Gadget instance configuration projection. */
  instance: GadgetInstance;
}

/**
 * Standard Telegram Feed gadget template.
 * Displays received channel posts and direct broadcast messages with BBD-OS read tracking.
 *
 * @param props Gadget instance configuration and feed handlers.
 * @returns Accessible telegram feed component.
 */
export function TelegramFeed(props: TelegramFeedProps) {
  return <FeedGadget {...props} />;
}
