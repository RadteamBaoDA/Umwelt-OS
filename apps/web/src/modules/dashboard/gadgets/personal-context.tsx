'use client';

import React from 'react';
import type { GadgetInstance } from '../api';
import { EntityGadget, type EntityGadgetProps } from './entity-gadget';

/** Props for the PersonalContext gadget component. */
export interface PersonalContextProps extends Omit<EntityGadgetProps, 'instance'> {
  /** Gadget instance configuration projection. */
  instance: GadgetInstance;
}

/**
 * Standard Personal Context gadget template.
 * Specializes EntityGadget for owner personal spotlight and associated project entities.
 *
 * @param props Gadget instance configuration and entity items.
 * @returns Accessible personal context component.
 */
export function PersonalContext(props: PersonalContextProps) {
  return <EntityGadget {...props} />;
}
