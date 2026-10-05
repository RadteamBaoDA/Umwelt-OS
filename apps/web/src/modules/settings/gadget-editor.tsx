'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertCircle,
  Bell,
  Edit2,
  FolderPlus,
  Layout,
  Plus,
  RotateCw,
  Settings,
  Trash2,
} from 'lucide-react';
import { useTranslations } from 'next-intl';
import React, { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useWorkspaceSession } from '@/core/app-shell/workspace-shell';
import {
  dashboardKeys,
  deleteGadgetDefinition,
  listGadgetDefinitions,
  patchGadgetDefinition,
  type GadgetDefinition,
} from '@/modules/dashboard/api';

/** Props for the GadgetEditor settings component. */
export interface GadgetEditorProps {
  /** Optional initial definitions list. */
  initialDefinitions?: GadgetDefinition[];
}

/**
 * Settings view for browsing, inspecting, and managing owner-scoped reusable gadget definitions.
 *
 * @param props Optional initial definition rows.
 * @returns Accessible settings panel component.
 */
export function GadgetEditor({ initialDefinitions }: GadgetEditorProps) {
  const t = useTranslations('dashboard');
  const session = useWorkspaceSession();
  const queryClient = useQueryClient();

  const [selectedDefId, setSelectedDefId] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // Fetch definitions
  const defsQuery = useQuery({
    queryKey: dashboardKeys.definitions,
    queryFn: ({ signal }) => listGadgetDefinitions(100, signal),
  });

  const definitions = defsQuery.data ?? initialDefinitions ?? [];

  // Delete definition mutation
  const deleteMutation = useMutation({
    mutationFn: async (def: GadgetDefinition) => {
      setErrorMessage(null);
      await deleteGadgetDefinition(def.id, def.revision, session.csrfToken);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: dashboardKeys.definitions });
    },
    onError: (err) => {
      setErrorMessage(err instanceof Error ? err.message : 'Failed to delete definition');
    },
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between border-b border-border pb-3">
        <div>
          <h2 className="text-base font-bold text-foreground">Saved Gadget Definitions</h2>
          <p className="text-xs text-muted-foreground">
            Manage reusable templates, filters, and rule triggers available across all dashboards.
          </p>
        </div>
        <button
          type="button"
          onClick={() => defsQuery.refetch()}
          disabled={defsQuery.isFetching}
          className="p-1.5 rounded text-muted-foreground hover:text-foreground transition-colors"
          title="Refresh definitions"
        >
          <RotateCw className={`w-4 h-4 ${defsQuery.isFetching ? 'animate-spin' : ''}`} />
        </button>
      </div>

      {errorMessage && (
        <div className="p-2.5 rounded-lg bg-destructive/15 border border-destructive/30 text-destructive text-xs flex items-center gap-2">
          <AlertCircle className="w-4 h-4 shrink-0" />
          <span>{errorMessage}</span>
        </div>
      )}

      {defsQuery.isLoading && definitions.length === 0 && (
        <div className="space-y-2 animate-pulse">
          <div className="h-16 bg-muted/20 rounded-lg" />
          <div className="h-16 bg-muted/20 rounded-lg" />
        </div>
      )}

      {!defsQuery.isLoading && definitions.length === 0 && (
        <div className="p-8 text-center border border-dashed border-border rounded-xl text-muted-foreground">
          <Layout className="w-8 h-8 mb-2 opacity-50 mx-auto" />
          <p className="text-xs font-semibold text-foreground mb-1">No saved definitions</p>
          <p className="text-[11px] max-w-sm mx-auto text-muted-foreground">
            Definitions created while configuring gadgets on your dashboards will be listed here.
          </p>
        </div>
      )}

      {definitions.length > 0 && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {definitions.map((def) => (
            <div
              key={def.id}
              className="p-3.5 rounded-xl border border-border bg-card space-y-2 shadow-xs hover:border-accent/50 transition-colors"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="space-y-0.5 min-w-0">
                  <h4 className="text-xs font-bold text-foreground truncate">{def.name}</h4>
                  <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground font-mono">
                    <span className="uppercase">{def.renderer}</span>
                    <span>•</span>
                    <span>rev.{def.revision}</span>
                  </div>
                </div>

                <button
                  type="button"
                  onClick={() => deleteMutation.mutate(def)}
                  disabled={deleteMutation.isPending}
                  className="p-1 text-muted-foreground hover:text-destructive transition-colors"
                  title="Delete unused definition"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
              </div>

              {def.filters?.keywords && def.filters.keywords.length > 0 && (
                <div className="text-[11px] text-muted-foreground">
                  <span>Keywords: </span>
                  <span className="text-foreground font-medium">
                    {def.filters.keywords.join(', ')}
                  </span>
                </div>
              )}

              {def.highlight_rules && def.highlight_rules.length > 0 && (
                <div className="flex items-center gap-1 text-[10px] text-muted-foreground">
                  <Bell className="w-3 h-3 text-accent" />
                  <span>{def.highlight_rules.length} highlight rules configured</span>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
