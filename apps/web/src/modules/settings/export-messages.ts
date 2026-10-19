/** Localized copy for the owner data export settings panel. */
export const exportMessages = {
  'en-us': {
    title: 'Data export',
    description: 'Download a portable copy of supported owner data.',
    portableTitle: 'Portable owner data',
    portableDescription: 'Exports include retained document versions and eligible conversation history. Credentials, raw files, graph state, workflow secrets, and deployment identity are excluded. This is not a restorable instance backup.',
    json: 'Download JSON',
    markdown: 'Download Markdown',
    csv: 'Download CSV',
    downloading: 'Preparing download…',
    downloadFailed: 'The export could not be prepared. Owner data may have changed; retry the download.',
  },
  'vi-vi': {
    title: 'Xuất dữ liệu',
    description: 'Tải bản sao dữ liệu chủ sở hữu được hỗ trợ.',
    portableTitle: 'Dữ liệu chủ sở hữu có thể di chuyển',
    portableDescription: 'Bản xuất gồm phiên bản tài liệu được lưu và lịch sử hội thoại đủ điều kiện. Thông tin xác thực, tệp thô, trạng thái đồ thị, bí mật quy trình và danh tính triển khai sẽ bị loại trừ. Đây không phải bản sao lưu có thể khôi phục toàn bộ hệ thống.',
    json: 'Tải JSON',
    markdown: 'Tải Markdown',
    csv: 'Tải CSV',
    downloading: 'Đang chuẩn bị tải xuống…',
    downloadFailed: 'Không thể chuẩn bị bản xuất. Dữ liệu có thể đã thay đổi; hãy thử lại.',
  },
} as const;
