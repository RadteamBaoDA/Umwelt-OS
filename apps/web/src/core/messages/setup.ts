/** Bilingual `setup` messages. Add keys for this area here only. */
export const setupMessages = {
  'en-us': {
      brand: 'Umwelt-OS · Local setup', title: 'Create your owner account', description: 'This account is stored on this server. Choose a password with at least 12 characters.',
      setupToken: 'Setup token', enterSetupToken: 'Enter the setup token.', password: 'Password', passwordHelp: 'Use at least 12 characters.', confirmPassword: 'Confirm password', passwordMismatch: 'Passwords do not match.',
      failure: 'Setup could not be completed.', creating: 'Creating account…', createOwner: 'Create owner',
  },
  'vi-vi': {
      brand: 'Umwelt-OS · Thiết lập cục bộ', title: 'Tạo tài khoản chủ sở hữu', description: 'Tài khoản này được lưu trên máy chủ. Chọn mật khẩu có ít nhất 12 ký tự.',
      setupToken: 'Mã thiết lập', enterSetupToken: 'Nhập mã thiết lập.', password: 'Mật khẩu', passwordHelp: 'Dùng ít nhất 12 ký tự.', confirmPassword: 'Xác nhận mật khẩu', passwordMismatch: 'Mật khẩu không khớp.',
      failure: 'Không thể hoàn tất thiết lập.', creating: 'Đang tạo tài khoản…', createOwner: 'Tạo tài khoản chủ sở hữu',
  },
} as const;
