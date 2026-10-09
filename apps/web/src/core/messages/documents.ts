/** Bilingual `documents` messages for the document detail page. Add keys for this area here only. */
export const documentsMessages = {
  'en-us': {
    brand: 'Knowledge', back: '← Documents', loadingDocument: 'Loading document', unavailable: 'Document unavailable', loadFailed: 'Could not load document.', retry: 'Retry',
    sourceLine: 'Source: {source} · Version {version} · Updated {updated}', inspectOriginal: 'Inspect original file and provenance',
    parsedTextTruncated: 'This file was too large to index in full; only the first 10 MiB of text is searchable.',
    deleteDocument: 'Delete document', deleteTitle: 'Permanently delete “{title}”?', deleteBody: 'This also deletes its entire version history. This cannot be undone.', cancel: 'Cancel', deleteFailed: 'Could not delete document.',
    citedUnavailable: 'Cited evidence is no longer available under the current source access policy.', citedChunk: 'Cited chunk · Version {version}',
    content: 'Content', loadingContent: 'Loading content…', contentLoadFailed: 'Could not load current version.', savedVersion: 'Saved version {version}',
    newerVersion: 'A newer version exists. Your draft is still here; review the latest version before saving again.', saveContentFailed: 'Could not save content.', useLatest: 'Use latest version number', saveContent: 'Save content',
    details: 'Details', titleLabel: 'Title', metadataLabel: 'Metadata (JSON object)', saveDetails: 'Save details', saveDetailsFailed: 'Could not save details.',
    metadataNotObject: 'Metadata must be a JSON object.', enterTitle: 'Enter a title.', invalidMetadata: 'Invalid metadata JSON.',
    versionHistory: 'Version history', loadingVersions: 'Loading versions…', versionsLoadFailed: 'Could not load versions.', version: 'Version {version}', loadMoreVersions: 'Load more versions', loadingVersion: 'Loading version…', versionLoadFailed: 'Could not load version.',
    unauthorized: 'Please sign in again to continue.', forbidden: 'This action is not allowed for this account.', conflict: 'The document changed elsewhere. Reload and try again.', serviceUnavailable: 'The local service is unavailable. Try again shortly.', requestFailed: 'The request could not be completed.',
  },
  'vi-vi': {
    brand: 'Tri thức', back: '← Tài liệu', loadingDocument: 'Đang tải tài liệu', unavailable: 'Tài liệu không khả dụng', loadFailed: 'Không thể tải tài liệu.', retry: 'Thử lại',
    sourceLine: 'Nguồn: {source} · Phiên bản {version} · Cập nhật {updated}', inspectOriginal: 'Xem tệp gốc và nguồn gốc',
    parsedTextTruncated: 'Tệp quá lớn để lập chỉ mục đầy đủ; chỉ 10 MiB văn bản đầu tiên có thể tìm kiếm.',
    deleteDocument: 'Xóa tài liệu', deleteTitle: 'Xóa vĩnh viễn “{title}”?', deleteBody: 'Toàn bộ lịch sử phiên bản của tài liệu cũng sẽ bị xóa. Không thể hoàn tác.', cancel: 'Hủy', deleteFailed: 'Không thể xóa tài liệu.',
    citedUnavailable: 'Bằng chứng được trích dẫn không còn khả dụng theo chính sách truy cập nguồn hiện tại.', citedChunk: 'Đoạn được trích dẫn · Phiên bản {version}',
    content: 'Nội dung', loadingContent: 'Đang tải nội dung…', contentLoadFailed: 'Không thể tải phiên bản hiện tại.', savedVersion: 'Phiên bản đã lưu {version}',
    newerVersion: 'Đã có phiên bản mới hơn. Bản nháp của bạn vẫn còn; hãy xem phiên bản mới nhất trước khi lưu lại.', saveContentFailed: 'Không thể lưu nội dung.', useLatest: 'Dùng số phiên bản mới nhất', saveContent: 'Lưu nội dung',
    details: 'Chi tiết', titleLabel: 'Tiêu đề', metadataLabel: 'Siêu dữ liệu (đối tượng JSON)', saveDetails: 'Lưu chi tiết', saveDetailsFailed: 'Không thể lưu chi tiết.',
    metadataNotObject: 'Siêu dữ liệu phải là một đối tượng JSON.', enterTitle: 'Hãy nhập tiêu đề.', invalidMetadata: 'JSON siêu dữ liệu không hợp lệ.',
    versionHistory: 'Lịch sử phiên bản', loadingVersions: 'Đang tải các phiên bản…', versionsLoadFailed: 'Không thể tải các phiên bản.', version: 'Phiên bản {version}', loadMoreVersions: 'Tải thêm phiên bản', loadingVersion: 'Đang tải phiên bản…', versionLoadFailed: 'Không thể tải phiên bản.',
    unauthorized: 'Hãy đăng nhập lại để tiếp tục.', forbidden: 'Tài khoản này không được phép thực hiện thao tác này.', conflict: 'Tài liệu đã thay đổi ở nơi khác. Hãy tải lại và thử lại.', serviceUnavailable: 'Dịch vụ cục bộ hiện không khả dụng. Hãy thử lại sau.', requestFailed: 'Không thể hoàn tất yêu cầu.',
  },
} as const;
