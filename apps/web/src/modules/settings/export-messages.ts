/** Localized copy for the owner data export settings panel. */
export const exportMessages = {
  'en-us': {
    title: 'Data export',
    description: 'Exports include your documents (with their saved versions) and eligible chat history. Passwords, API keys, original files and workflow secrets are not included. This is a copy of your content, not a full backup you can restore from.',
    json: 'Download JSON',
    markdown: 'Download Markdown',
    csv: 'Download CSV',
    downloading: 'Preparing download…',
    downloadFailed: 'The export could not be prepared. Owner data may have changed; retry the download.',
  },
  'vi-vi': {
    title: 'Xuất dữ liệu',
    description: 'Bản xuất gồm tài liệu của bạn (kèm các phiên bản đã lưu) và lịch sử trò chuyện đủ điều kiện. Mật khẩu, khóa API, tệp gốc và bí mật quy trình không được đưa vào. Đây là bản sao nội dung, không phải bản sao lưu đầy đủ để khôi phục.',
    json: 'Tải JSON',
    markdown: 'Tải Markdown',
    csv: 'Tải CSV',
    downloading: 'Đang chuẩn bị tải xuống…',
    downloadFailed: 'Không thể chuẩn bị bản xuất. Dữ liệu có thể đã thay đổi; hãy thử lại.',
  },
} as const;
