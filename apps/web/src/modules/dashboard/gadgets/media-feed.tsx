'use client';

import type { GadgetInstance } from '../api';
import { MediaCollection } from './media-parts';

/** Media shelf reads paginated owner-validated projections; renderer state survives frame expansion. */
export function MediaFeedGadget({ instance }: { instance: GadgetInstance }) {
  return <MediaCollection instance={instance} />;
}
