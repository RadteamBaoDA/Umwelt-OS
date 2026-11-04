'use client';

import { useCallback, useId, useRef, useState } from 'react';
import type { ConnectorDraft } from './use-connector-draft';

/** Generations captured when an editor request starts; a completion publishes state only while all of them still match. */
export type RequestToken = {
  generation: number;
  editorGeneration: string;
  sourceId: string | null;
  authGeneration: number;
  controller: AbortController;
  draftVersion: number;
};

/** Display-preference handle that tells whether an authentication generation is still the active session. */
export type AuthGenerations = { authGeneration: number; isCurrentGeneration: (generation: number) => boolean };

/**
 * Single-flight request fence of the connector editor. `beginRequest` serializes actions (one at a time, aborting the previous
 * controller), and `requestIsCurrent` rejects completions from an old editor, source, auth session or aborted request.
 * `acceptLeave` and `closeEditor` advance `requestGeneration` so everything in flight is dropped.
 */
export function useConnectorRequests(draft: ConnectorDraft, display: AuthGenerations) {
  const editorGenerationId = useId();
  const editorGenerationRef = useRef(editorGenerationId);
  const requestGeneration = useRef(0);
  const requestController = useRef<AbortController | null>(null);
  const busyRef = useRef('');
  const verifyController = useRef<AbortController | null>(null);
  const [busyAction, setBusyAction] = useState('');
  const [actionsBusy, setActionsBusy] = useState(false);
  const { sourceId, draftVersion } = draft;

  /** Starts a serialized editor request and captures the generations needed to reject stale completion. */
  function beginRequest(action: string): RequestToken | null {
    if (busyRef.current) return null;
    requestController.current?.abort();
    const controller = new AbortController();
    requestController.current = controller;
    busyRef.current = action;
    setBusyAction(action);
    return {
      generation: requestGeneration.current,
      editorGeneration: editorGenerationRef.current,
      sourceId: sourceId || null,
      authGeneration: display.authGeneration,
      controller,
      draftVersion: draftVersion.current,
    };
  }
  /** Checks that a request still belongs to the active editor, source, and authenticated session. */
  function requestIsCurrent(token: RequestToken): boolean {
    // A response from an old editor, source, auth session, or aborted request must not publish state.
    return (
      token.generation === requestGeneration.current &&
      token.editorGeneration === editorGenerationRef.current &&
      display.isCurrentGeneration(token.authGeneration) &&
      !token.controller.signal.aborted
    );
  }
  /** Clears the busy state only when the completing request still owns the active editor generation. */
  function endRequest(token: RequestToken) {
    if (!requestIsCurrent(token)) return;
    busyRef.current = '';
    setBusyAction('');
  }
  /** Drops everything in flight: advances the generation, aborts the controller and releases the busy state (leave, close). */
  const dropInFlight = useCallback(() => {
    requestGeneration.current += 1;
    requestController.current?.abort();
    requestController.current = null;
    busyRef.current = '';
    setBusyAction('');
  }, []);
  /** Unmount cleanup: advances the generation and aborts both the request and the verify controllers (latest values). */
  const abortOnUnmount = useCallback(() => {
    requestGeneration.current += 1;
    requestController.current?.abort();
    verifyController.current?.abort();
  }, []);
  return {
    requestGeneration,
    requestController,
    busyRef,
    verifyController,
    busyAction,
    setBusyAction,
    actionsBusy,
    setActionsBusy,
    beginRequest,
    requestIsCurrent,
    endRequest,
    dropInFlight,
    abortOnUnmount,
  };
}
export type ConnectorRequests = ReturnType<typeof useConnectorRequests>;
