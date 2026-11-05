// Minimal SSE text parser for k6 responses. k6's core http module buffers a response until the stream ends, so
// per-frame arrival times are not observable; the chat scenario therefore measures stream-open and completion times
// (see docs/slo.md, "Chat time to first token") and uses this parser to verify the frames.

/**
 * Splits a text/event-stream body into frames.
 * @param {string} body Whole response body.
 * @returns {{id: string | null, event: string, data: string}[]} Frames in arrival order; comment-only blocks are skipped.
 */
export function parseSse(body) {
  const frames = [];
  for (const block of String(body || '').split(/\n\n+/)) {
    if (!block.trim()) continue;
    let id = null;
    let event = 'message';
    const data = [];
    for (const line of block.split('\n')) {
      if (line.startsWith(':')) continue;
      if (line.startsWith('id:')) id = line.slice(3).trim();
      else if (line.startsWith('event:')) event = line.slice(6).trim();
      else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
    }
    if (data.length || id !== null || event !== 'message') frames.push({ id, event, data: data.join('\n') });
  }
  return frames;
}

/**
 * Tells whether a frame list ended a run: a `message.done` frame, or a `status` frame whose data.status is a failure
 * terminal (failed, cancelled, unavailable, auth_expired), after which the server closes the stream.
 * @param {{event: string}[]} frames Output of parseSse().
 * @returns {boolean}
 */
export function isTerminal(frames) {
  return frames.some((f) => f.event === 'message.done' || (f.event === 'status' && failureStatus(f.data)));
}

const FAILURE_STATUSES = new Set(['failed', 'cancelled', 'unavailable', 'auth_expired']);

/** Returns true when a `status` frame's JSON data carries a failure terminal status. */
function failureStatus(data) {
  try {
    return FAILURE_STATUSES.has(JSON.parse(data).status);
  } catch (error) {
    return false;
  }
}
