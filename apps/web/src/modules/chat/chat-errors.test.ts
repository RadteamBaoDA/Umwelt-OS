import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/core/api';
import { chatFailureKey } from './chat-errors';

describe('chatFailureKey', () => {
  it('keeps the fallback for non-API errors and maps codes before status classes', () => {
    vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    expect(chatFailureKey(new Error('x'), 'error.sending')).toBe('error.sending');
    expect(chatFailureKey(new ApiError(422, 'm', { code: 'selection_too_large' }), 'error.sending')).toBe('context.selectionTooLarge');
    expect(chatFailureKey(new ApiError(401, 'm'), 'error.sending')).toBe('sessionExpired');
    expect(chatFailureKey(new ApiError(409, 'm'), 'error.sending')).toBe('error.conflict');
    expect(chatFailureKey(new ApiError(429, 'm'), 'error.sending')).toBe('error.rateLimited');
    expect(chatFailureKey(new ApiError(503, 'm'), 'error.sending')).toBe('error.service');
    expect(chatFailureKey(new ApiError(400, 'm'), 'error.sending')).toBe('error.sending');
  });
});
