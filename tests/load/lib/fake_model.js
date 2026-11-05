// Points the owner's chat role at tests/integration/fake_model.py through the real settings API (idempotent),
// mirroring tests/e2e/chat-live.spec.ts, so chat load measures the platform and not a provider.
import http from 'k6/http';
import { check, fail } from 'k6';
import { API_URL, FAKE_MODEL_URL } from './config.js';
import { writeHeaders } from './auth.js';

/**
 * Configures the fake model provider, grants reasoning consent and runs the streaming capability probe.
 * Safe to call repeatedly: returns early when the fake provider is already the chat default.
 * @param {{csrfToken: string}} auth Authenticated session material from login().
 */
export function configureFakeModel(auth) {
  const headers = writeHeaders(auth, { 'Content-Type': 'application/json' });
  const current = http.get(`${API_URL}/api/v1/settings/ai`, { tags: { endpoint: 'settings_ai' } }).json();
  const ready = current.defaults && current.defaults.chat && current.defaults.chat.model === 'fake-chat'
    && (current.providers || []).some((p) => p.id === 'default' && p.base_url === FAKE_MODEL_URL);
  if (ready) return;
  const common = {
    defaults: {
      chat: { model: 'fake-chat', destination: 'remote', provider_id: 'default' },
      brief: { model: 'fake-chat', destination: 'remote', provider_id: 'default' },
    },
    request_timeout_seconds: 30,
  };
  const created = http.put(`${API_URL}/api/v1/settings/ai`, JSON.stringify({
    ...common,
    providers: [{
      id: 'default', kind: 'custom', label: 'fake', base_url: FAKE_MODEL_URL, credential_action: 'replaced',
      api_key: 'fake-model-key', models: [{ id: 'fake-chat', type: 'llm' }],
    }],
    expected_revision: current.configuration_revision,
  }), { headers, tags: { endpoint: 'settings_ai' } });
  check(created, { 'provider created': (r) => r.status === 200 }) || fail(`create provider: ${created.status} ${created.body}`);
  const body = created.json();
  const destination = body.providers.find((p) => p.id === 'default').destination_id;
  const saved = http.put(`${API_URL}/api/v1/settings/ai`, JSON.stringify({
    ...common,
    privacy: {
      allow_remote_reasoning: true, reasoning_destinations: [destination],
      allow_remote_embeddings: false, embedding_destinations: [],
    },
    expected_revision: body.configuration_revision,
  }), { headers, tags: { endpoint: 'settings_ai' } });
  check(saved, { 'consent saved': (r) => r.status === 200 }) || fail(`grant consent: ${saved.status} ${saved.body}`);
  // Same capabilities as tests/e2e/chat-live.spec.ts so the chat worker sees identical capability evidence.
  for (const capability of ['streaming', 'tools']) {
    const probe = http.post(`${API_URL}/api/v1/settings/models/chat/test`, JSON.stringify({ capability }), {
      headers, tags: { endpoint: 'settings_probe' },
    });
    check(probe, { 'probe supported': (r) => r.status === 200 && r.json('result') === 'supported' })
      || fail(`probe ${capability}: ${probe.status} ${probe.body}`);
  }
}
