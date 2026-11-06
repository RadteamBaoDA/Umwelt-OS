// Chat scenario against the fake model (tests/integration/fake_model.py): create a conversation, send a message,
// read the SSE stream until the run is done. Provider latency is excluded by construction.
// Run alone: k6 run -e API_URL=... -e PUBLIC_ORIGIN=... tests/load/chat.js
import http from 'k6/http';
import { check } from 'k6';
import { API_URL, dur } from './lib/config.js';
import { sloThresholds } from './lib/thresholds.js';
import { setupRun, ensureVuSession, writeHeaders } from './lib/run.js';
import { parseSse, isTerminal } from './lib/sse.js';
import { chatSendMs, chatSseOpenMs, chatTtftMs, chatCompletionMs } from './lib/metrics.js';

/** Scenario definitions, exported so all.js can compose them. */
export const chatScenarios = {
  // The API budget is 30 chat sends per minute (require_budget("chat_send", 30, 60)); stay at two thirds of it.
  chat: {
    executor: 'constant-arrival-rate',
    exec: 'chatExec',
    rate: 20,
    timeUnit: '1m',
    duration: dur(180),
    preAllocatedVUs: 4,
    maxVUs: 10,
  },
};

// noCookiesReset keeps the session cookies installed once per VU across iterations.
export const options = { scenarios: chatScenarios, thresholds: sloThresholds, noCookiesReset: true };

/** k6 setup: logs in once and points the chat role at the fake model. */
export function setup() {
  return setupRun({ fakeModel: true });
}

/**
 * Sends one message and reads its SSE stream to the end.
 * @param {object} auth Session material from setup().
 * @param {number} tokens Number of tokens the fake model streams (the marker `[fake:tokens=N]`).
 * @returns {{ok: boolean, sendMs: number, openMs: number, totalMs: number}}
 */
function chatOnce(auth, tokens) {
  const headers = writeHeaders(auth, { 'Content-Type': 'application/json' });
  const created = http.post(`${API_URL}/api/v1/conversations`, JSON.stringify({}), { headers, tags: { endpoint: 'chat_create' } });
  if (!check(created, { 'conversation 201': (r) => r.status === 201 || r.status === 200 })) return { ok: false };
  const started = Date.now();
  const sent = http.post(
    `${API_URL}/api/v1/conversations/${created.json('id')}/messages`,
    JSON.stringify({ content: `load test [fake:tokens=${tokens}]`, client_request_id: `k6-${started}-${Math.random().toString(36).slice(2)}` }),
    { headers, tags: { endpoint: 'chat_send' } },
  );
  if (!check(sent, { 'send 202': (r) => r.status === 202 })) return { ok: false };
  const sendMs = Date.now() - started;
  chatSendMs.add(sendMs);
  const stream = http.get(`${API_URL}/api/v1/responses/${sent.json('response_id')}/events`, {
    headers: { Accept: 'text/event-stream' },
    tags: { endpoint: 'chat_events' },
    timeout: '45s',
  });
  const frames = parseSse(stream.body);
  const done = frames.some((f) => f.event === 'message.done');
  const ok = check(stream, {
    'events 200': (r) => r.status === 200,
    'stream finished': () => isTerminal(frames),
    'stream completed': () => done,
    'tokens streamed': () => frames.filter((f) => f.event === 'message.delta').length >= 1,
  });
  return { ok, sendMs, openMs: stream.timings.waiting, totalMs: Date.now() - started };
}

/**
 * One chat iteration. Every fifth iteration streams 20 tokens to exercise the delta path; the rest stream a single
 * token so the whole-stream time is the time to first token (k6 core cannot timestamp individual SSE frames).
 * @param {object} auth Session material from setup().
 */
export function chatExec(auth) {
  ensureVuSession(auth);
  const multi = __ITER % 5 === 4;
  const result = chatOnce(auth, multi ? 20 : 1);
  if (!result.ok) return;
  chatSseOpenMs.add(result.openMs);
  if (multi) chatCompletionMs.add(result.totalMs);
  else chatTtftMs.add(result.totalMs);
}

export default chatExec;
