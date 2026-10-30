import type { GadgetRenderer } from './api';

/**
 * Builds the Settings > Sources URL pre-filtered to datasources a renderer can read.
 *
 * @param renderer Renderer descriptor; its `accepted_shapes` order is preserved in the `shapes` list.
 * @returns `/settings/sources?shapes=<comma list>&for=<renderer id>`, or plain `/settings/sources` when the renderer accepts any shape.
 */
export function sourcesLinkFor(renderer: GadgetRenderer): string {
  if (renderer.accepted_shapes === null) return '/settings/sources';
  return `/settings/sources?shapes=${renderer.accepted_shapes.join(',')}&for=${renderer.id}`;
}
