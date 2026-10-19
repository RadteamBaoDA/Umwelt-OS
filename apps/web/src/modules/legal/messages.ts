/** Public policy chrome; canonical policy content lives in docs/legal. */
export const legalMessages = {
  'en-us': { privacy: 'Privacy', terms: 'Terms', refunds: 'Refunds', draft: 'Draft ? operator review required', notice: 'Replace [LEGAL NAME], [CONTACT EMAIL] and [JURISDICTION] before offering this deployment to others.', login: 'Sign in', inventory: 'Services and data flows' },
  'vi-vi': { privacy: 'Quy?n ri?ng t?', terms: '?i?u kho?n', refunds: 'Ho?n ti?n', draft: 'B?n nh?p ? ??n v? v?n h?nh c?n r? so?t', notice: 'Thay [LEGAL NAME], [CONTACT EMAIL] v? [JURISDICTION] tr??c khi cung c?p b?n tri?n khai n?y cho ng??i kh?c.', login: '??ng nh?p', inventory: 'D?ch v? v? lu?ng d? li?u' },
} as const;
export type LegalLocale = keyof typeof legalMessages;
export type PolicyName = 'privacy' | 'terms' | 'refunds';
