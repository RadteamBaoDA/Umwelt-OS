import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/core/api';
import { chatFailureKey } from './chat-errors';

describe('chatFailureKey', () => {
  it('keeps the fallback for non-API errors and maps codes before status classes', () => {
    vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    expect(chatFailureKey(new Error('x'), 'errorSending')).toBe('errorSending');
    expect(chatFailureKey(new ApiError(422, 'm', { code: 'selection_too_large' }), 'errorSending')).toBe('contextSelectionTooLarge');
    expect(chatFailureKey(new ApiError(401, 'm'), 'errorSending')).toBe('sessionExpired');
    expect(chatFailureKey(new ApiError(409, 'm'), 'errorSending')).toBe('errorConflict');
    expect(chatFailureKey(new ApiError(503, 'm'), 'errorSending')).toBe('errorService');
    expect(chatFailureKey(new ApiError(400, 'm'), 'errorSending')).toBe('errorSending');
  });
});
