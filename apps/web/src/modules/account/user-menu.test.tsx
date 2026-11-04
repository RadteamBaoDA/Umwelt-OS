import { describe, expect, it } from 'vitest';
import { initialsOf } from './user-menu';

describe('initialsOf', () => {
  it('uses first and last word letters', () => {
    expect(initialsOf('Vpacs Demo')).toBe('VD');
    expect(initialsOf('ann marie lee')).toBe('AL');
  });
  it('uses one letter for a single word', () => {
    expect(initialsOf('Owner')).toBe('O');
  });
  it('keeps Vietnamese diacritics', () => {
    expect(initialsOf('Đoàn Bảo')).toBe('ĐB');
  });
  it('is empty for blank input so the icon shows', () => {
    expect(initialsOf('  ')).toBe('');
    expect(initialsOf(null)).toBe('');
  });
});
