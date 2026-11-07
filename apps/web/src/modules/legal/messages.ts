/** Public policy chrome; canonical policy content lives in docs/legal. */
export const legalMessages = {
  'en-us': { privacy: 'Privacy', terms: 'Terms', refunds: 'Refunds', draft: 'Operator review', notice: 'The operator must confirm deployment-specific legal obligations and service details before offering this instance to others.', login: 'Sign in', inventory: 'Services and data flows', language: 'Language', policies: 'Policies' },
  'vi-vi': { privacy: 'Quyền riêng tư', terms: 'Điều khoản', refunds: 'Hoàn tiền', draft: 'Đơn vị vận hành cần rà soát', notice: 'Đơn vị vận hành cần xác nhận nghĩa vụ pháp lý và thông tin dịch vụ riêng của bản triển khai trước khi cung cấp cho người khác.', login: 'Đăng nhập', inventory: 'Dịch vụ và luồng dữ liệu', language: 'Ngôn ngữ', policies: 'Chính sách' },
} as const;
export type LegalLocale = keyof typeof legalMessages;
export type PolicyName = 'privacy' | 'terms' | 'refunds';
