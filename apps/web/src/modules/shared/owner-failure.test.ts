import { describe, expect, it } from 'vitest';
import { ApiError } from '@/core/api';
import { ownerFailureKey } from './owner-failure';

describe('ownerFailureKey', () => {
  it('maps 404 to the missing key, 401/403 to access denied and everything else to the fallback', () => {
    expect(ownerFailureKey(new ApiError(404, 'x'), 'taskOwnerMissing', 'taskReadFailed')).toBe('taskOwnerMissing');
    expect(ownerFailureKey(new ApiError(401, 'x'), 'taskOwnerMissing', 'taskReadFailed')).toBe('ownerAccessDenied');
    expect(ownerFailureKey(new ApiError(403, 'x'), 'taskOwnerMissing', 'taskReadFailed')).toBe('ownerAccessDenied');
    expect(ownerFailureKey(new ApiError(500, 'x'), 'taskOwnerMissing', 'taskReadFailed')).toBe('taskReadFailed');
    expect(ownerFailureKey(new Error('network'), 'taskOwnerMissing', 'taskReadFailed')).toBe('taskReadFailed');
  });
});
