/** Public policy chrome; canonical policy content lives in docs/legal. */
export const legalMessages = {
  'en-us': { privacy: 'Privacy', terms: 'Terms', refunds: 'Refunds', draft: 'Draft — operator review required', notice: 'Replace [LEGAL NAME], [CONTACT EMAIL] and [JURISDICTION] before offering this deployment to others.', login: 'Sign in', inventory: 'Services and data flows', language: 'Language', policies: 'Policies' },
  'vi-vi': { privacy: 'Quyền riêng tư', terms: 'Điều khoản', refunds: 'Hoàn tiền', draft: 'Bản nháp — đơn vị vận hành cần rà soát', notice: 'Thay [LEGAL NAME], [CONTACT EMAIL] và [JURISDICTION] trước khi cung cấp bản triển khai này cho người khác.', login: 'Đăng nhập', inventory: 'Dịch vụ và luồng dữ liệu', language: 'Ngôn ngữ', policies: 'Chính sách' },
} as const;
export type LegalLocale = keyof typeof legalMessages;
export type PolicyName = 'privacy' | 'terms' | 'refunds';
