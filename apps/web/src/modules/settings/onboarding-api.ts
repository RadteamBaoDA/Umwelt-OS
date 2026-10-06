import { apiRequest, csrfHeaders } from '@/core/api';

/** Supported persisted onboarding steps; completion is recorded only after an explicit Finish action. */
export type OnboardingStep = 'ai_privacy' | 'capability' | 'sources' | 'sample_or_import' | 'indexing' | 'complete';

/** Server-owned onboarding progress; no credentials or provider readiness claims are persisted. */
export type OnboardingState = {
  configuration_revision: number;
  current_step: OnboardingStep;
  data_choice: 'sample' | 'personal_import' | null;
  completed_at: string | null;
};

/** Reads the authenticated owner's persisted onboarding progress. */
export function getOnboardingState(signal?: AbortSignal) {
  return apiRequest<OnboardingState>('/api/v1/settings/onboarding', { signal });
}

/** Saves one current step using revision fencing and the caller's session CSRF token. */
export function saveOnboardingStep(step: OnboardingStep, revision: number, csrfToken: string, dataChoice?: 'sample' | 'personal_import' | null) {
  return apiRequest<OnboardingState>('/api/v1/settings/onboarding', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...csrfHeaders(csrfToken) },
    body: JSON.stringify({ expected_revision: revision, current_step: step, ...(dataChoice ? { data_choice: dataChoice } : {}) }),
  });
}
