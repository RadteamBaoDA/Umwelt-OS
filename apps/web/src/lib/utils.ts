import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

/**
 * Joins conditional class names (clsx) and resolves conflicting Tailwind utilities (tailwind-merge) so the
 * last conflicting class wins; non-conflicting classes keep their order.
 * @param inputs Class names, arrays, or `{ name: condition }` maps.
 * @returns A single space-separated class string.
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
