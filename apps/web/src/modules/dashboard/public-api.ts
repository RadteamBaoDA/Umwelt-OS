// Non-component public surface of the module (API clients, query keys, types, helpers). Import this from data/helper code
// so value imports do not evaluate the module's React component graph; components stay on the module index.
export {
  type GadgetDefinition,
  type HighlightRule,
  dashboardKeys,
  evaluateGadgetHighlights,
  getGadgetDefinitionUsage,
  highlightRuleErrorKey,
  listGadgetDefinitions,
  listGadgetSources,
  patchGadgetDefinition,
  previewHighlightRules,
} from './api';
export { formatRelativeTime } from './gadgets/relative-time';
