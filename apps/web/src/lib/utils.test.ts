import { describe, expect, it } from 'vitest';
import { cn } from '@/lib/utils';

describe('cn', () => {
  it('joins conditional classes and drops falsy values', () => {
    expect(cn('a', false && 'b', undefined, ['c'], { d: true, e: false })).toBe('a c d');
  });

  it('lets the last conflicting Tailwind utility win but keeps unrelated and custom classes in order', () => {
    expect(cn('input p-2 text-sm', 'p-4 text-muted-foreground')).toBe('input text-sm p-4 text-muted-foreground');
    expect(cn('select-content', undefined)).toBe('select-content');
  });
});
