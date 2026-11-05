// Non-component public surface of the module (API clients, query keys, types, helpers). Import this from data/helper code
// so value imports do not evaluate the module's React component graph; components stay on the module index.
export { type Entity, type MemoryPrivacyConfig, type MemoryPurgeRequest, type MemoryPurgeResponse, type ShareRow, type WorkspaceMember, documentKeys, entityKeys, getDocument, getEntity, getEntityNeighbors, getEntityTimeline, getGraphStatuses, getMemoryPrivacyConfig, getRelationshipEvidence, getVersion, grantShare, listEntities, listEntityEvidence, listGadgetDocumentProjections, memoryKeys, purgeMemoryData, setGadgetDocumentInteraction, updateMemoryPrivacyConfig } from './api';
export { shareErrorKey } from './document-sharing';
