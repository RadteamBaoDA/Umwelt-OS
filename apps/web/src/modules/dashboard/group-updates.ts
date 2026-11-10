'use client';

import { useEffect, useMemo, useState } from 'react';
import type { Source } from '@/modules/sources';
import type { GadgetInstance } from './api';

/** Stable empty list for the not-yet-loaded dashboard. */
const EMPTY_INSTANCES: GadgetInstance[] = [];
const SEEN_PREFIX = 'umwelt.dashboard.groupSeen.';

/**
 * Pure: ids of groups whose bound sources succeeded after the group was last visited.
 * A group never visited (no stored time) is not flagged, so a fresh browser shows no dots; the active group is
 * never flagged because the owner is looking at it.
 *
 * @param instances Gadget instances of the dashboard (group and bound source ids).
 * @param sources Sources already loaded for the dashboard.
 * @param seenAt Last-visit ISO time per group id.
 * @param activeGroupId Group currently shown.
 */
export function computeUpdatedGroupIds(
  instances: Pick<GadgetInstance, 'group_id' | 'definition'>[],
  sources: Pick<Source, 'id' | 'last_success_at'>[],
  seenAt: Record<string, string | null | undefined>,
  activeGroupId: string,
): Set<string> {
  const successById = new Map(sources.map((s) => [s.id, s.last_success_at ? Date.parse(s.last_success_at) : NaN]));
  const updated = new Set<string>();
  for (const instance of instances) {
    const seen = Date.parse(seenAt[instance.group_id] ?? '');
    if (instance.group_id === activeGroupId || updated.has(instance.group_id) || !Number.isFinite(seen)) continue;
    if (instance.definition.source_ids.some((id) => (successById.get(id) ?? NaN) > seen)) updated.add(instance.group_id);
  }
  return updated;
}

/** Reads a group's stored last-visit time (null when absent or storage fails). */
function readSeen(groupId: string): string | null {
  try { return window.localStorage.getItem(SEEN_PREFIX + groupId); } catch { return null; }
}

/**
 * Tracks per-browser last-visit times of dashboard groups and returns the set with newer source data.
 * The visit is recorded when a tab is entered (first time) and again when it is left, so data arriving while the
 * owner views a tab does not flag it afterwards. Uses already-loaded sources; no queries.
 *
 * @param instances Instances of the active dashboard.
 * @param sources Dashboard sources from `useDashboardData`.
 * @param activeGroupId Currently selected group id ('' while loading).
 */
export function useUpdatedGroups(
  instances: GadgetInstance[] | undefined,
  sources: Source[],
  activeGroupId: string,
): ReadonlySet<string> {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!activeGroupId) return undefined;
    const record = () => { try { window.localStorage.setItem(SEEN_PREFIX + activeGroupId, new Date().toISOString()); } catch { /* storage unavailable */ } };
    if (readSeen(activeGroupId) === null) record();
    return () => { record(); setTick((n) => n + 1); };
  }, [activeGroupId]);
  return useMemo(() => {
    const seenAt: Record<string, string | null> = {};
    for (const instance of instances ?? EMPTY_INSTANCES) seenAt[instance.group_id] = readSeen(instance.group_id);
    return computeUpdatedGroupIds(instances ?? EMPTY_INSTANCES, sources, seenAt, activeGroupId);
    // tick forces a storage re-read after a visit was recorded.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instances, sources, activeGroupId, tick]);
}
