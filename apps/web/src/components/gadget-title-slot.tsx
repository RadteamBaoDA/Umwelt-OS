'use client';

import { createContext, useContext, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

/** Frame hosts a gadget can portal into: the title bar (right side) and the foot row's left note. */
export interface GadgetSlotHosts {
  /** Title-bar slot element, or null until it mounts. */
  title: HTMLElement | null;
  /** Foot-row note element, or null until it mounts. */
  foot: HTMLElement | null;
}

/**
 * Where a gadget's chrome renders. `undefined` = not inside a gadget frame slot (render in place, e.g. standalone
 * pages and the expanded dialog); otherwise the frame's hosts, each null until it mounts.
 */
const GadgetSlotContext = createContext<GadgetSlotHosts | undefined>(undefined);

/** Provider set by the gadget frame around its renderer subtree. */
export const GadgetSlotProvider = GadgetSlotContext.Provider;

/**
 * Shared portal rule: inside a gadget frame, portal into the chosen host (render nothing until the
 * host mounts, avoiding a first-frame flash in the body); everywhere else render in place.
 */
function SlotPortal({ which, children }: { which: keyof GadgetSlotHosts; children: ReactNode }) {
  const hosts = useContext(GadgetSlotContext);
  if (hosts === undefined) return <>{children}</>;
  const host = hosts[which];
  return host ? createPortal(children, host) : null;
}

/**
 * Portals `children` into the gadget frame's title bar so a gadget's toolbar sits in the pane
 * title like the design. React state, handlers and tab order stay with the gadget component.
 *
 * @param props.children Toolbar controls.
 */
export function TitleBarSlot({ children }: { children: ReactNode }) {
  return <SlotPortal which="title">{children}</SlotPortal>;
}

/**
 * Portals `children` (a short note such as "some stories may be incomplete") into the left of
 * the gadget frame's foot row, opposite the "updated ..." text.
 *
 * @param props.children Note content.
 */
export function FootBarSlot({ children }: { children: ReactNode }) {
  return <SlotPortal which="foot">{children}</SlotPortal>;
}
