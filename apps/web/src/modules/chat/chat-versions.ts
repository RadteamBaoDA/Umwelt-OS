import type { ChatMessage } from './api';

/** One prompt and the answer that followed it (the answer is absent while pending or after a failed run). */
interface Turn {
  user: ChatMessage;
  assistant?: ChatMessage;
}

/** Position of a message inside a revision chain; drives the `‹ 2/3 ›` control. */
export interface VersionNav {
  /** Id of the chain's first user message; key of the selection map. */
  chainId: string;
  /** Zero-based index of the shown version. */
  index: number;
  count: number;
  /** User-message id of every version, oldest first (the selection value). */
  versionIds: string[];
}

/** One message to render plus its version facts. */
export interface RenderEntry {
  msg: ChatMessage;
  /** True for the newest version of its chain (single messages are always latest); edit/regenerate are offered only there. */
  latest: boolean;
  /** Set on the one message of a multi-version chain that hosts the version control. */
  nav?: VersionNav;
}

/**
 * Pairs each user message with the assistant message that followed it. Anything that does not fit (system rows,
 * assistant rows without a preceding prompt) is kept as a lone message.
 */
function toTurns(messages: ChatMessage[]): Array<Turn | ChatMessage> {
  const out: Array<Turn | ChatMessage> = [];
  let open: Turn | null = null;
  for (const msg of messages) {
    if (msg.role === 'user') {
      open = { user: msg };
      out.push(open);
    } else if (msg.role === 'assistant' && open && !open.assistant) {
      open.assistant = msg;
    } else {
      out.push(msg);
    }
  }
  return out;
}

/** True for a paired prompt/answer entry (as opposed to a lone message). */
function isTurn(entry: Turn | ChatMessage): entry is Turn {
  return 'user' in entry;
}

/**
 * Chain id (root user-message id) of every turn. A turn revises the turn that owns the message its prompt (edit: the
 * edited prompt, regenerate: the previous answer) or its answer points at, matching the backend supersede rule.
 */
function chainRoots(turns: Turn[]): Map<Turn, string> {
  const owner = new Map<string, Turn>();
  for (const turn of turns) {
    owner.set(turn.user.id, turn);
    if (turn.assistant) owner.set(turn.assistant.id, turn);
  }
  const parentOf = (turn: Turn): Turn | undefined => {
    const target = turn.user.revision_of_message_id ?? turn.assistant?.revision_of_message_id;
    const parent = target ? owner.get(target) : undefined;
    return parent === turn ? undefined : parent;
  };
  const roots = new Map<Turn, string>();
  for (const turn of turns) {
    let root = turn;
    // The hop cap protects against a (corrupt) revision cycle.
    for (let hops = 0; hops < turns.length; hops += 1) {
      const parent = parentOf(root);
      if (!parent) break;
      root = parent;
    }
    roots.set(turn, root.user.id);
  }
  return roots;
}

/**
 * Collapses revision chains to the selected version and flattens the result into render entries.
 *
 * Each chain (edit and regenerate revisions of the same turn) shows only its selected version, by default the newest, at
 * the position of the newest one so the transcript stays chronological. The `‹ n/m ›` control sits on the user message
 * when the versions differ in prompt text (edits), otherwise on the answer (regenerations).
 *
 * @param messages - Conversation messages in order, including every superseded one.
 * @param selected - Chosen version (user-message id) per chain id; missing or unknown entries mean "newest".
 * @returns Entries to render in order.
 */
export function buildRenderEntries(messages: ChatMessage[], selected: Record<string, string>): RenderEntry[] {
  const entries = toTurns(messages);
  const turns = entries.filter(isTurn);
  const roots = chainRoots(turns);
  const chains = new Map<string, Turn[]>();
  for (const turn of turns) {
    const id = roots.get(turn)!;
    chains.set(id, [...(chains.get(id) ?? []), turn]);
  }
  const out: RenderEntry[] = [];
  for (const entry of entries) {
    if (!isTurn(entry)) {
      out.push({ msg: entry, latest: true });
      continue;
    }
    const chainId = roots.get(entry)!;
    const versions = chains.get(chainId)!;
    const newest = versions[versions.length - 1];
    // Older versions are emitted only through the newest one's slot.
    if (entry !== newest) continue;
    const shown = versions.find((turn) => turn.user.id === selected[chainId]) ?? newest;
    const nav: VersionNav | undefined = versions.length > 1
      ? { chainId, index: versions.indexOf(shown), count: versions.length, versionIds: versions.map((turn) => turn.user.id) }
      : undefined;
    const editsDiffer = versions.some((turn) => turn.user.content !== versions[0].user.content);
    const onUser = Boolean(nav) && (editsDiffer || !shown.assistant);
    out.push({ msg: shown.user, latest: shown === newest, nav: onUser ? nav : undefined });
    if (shown.assistant) out.push({ msg: shown.assistant, latest: shown === newest, nav: nav && !onUser ? nav : undefined });
  }
  return out;
}
