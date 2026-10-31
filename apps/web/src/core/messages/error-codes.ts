/** Bilingual `errorCodes` messages: plain-language labels keyed by backend machine error code. */
export const errorCodesMessages = {
  'en-us': {
    invalid_credential: 'Invalid credential', provider_rejected: 'The provider rejected the request', provider_unavailable: 'The provider is unavailable',
    provider_response_invalid: 'The provider sent an unreadable response', provider_rate_limited: 'The provider is rate limiting requests', rate_limited: 'Rate limited',
    provider_collection_failed: 'Collection from the provider failed', provider_revoked: 'Access was revoked at the provider', quota_exceeded: 'Quota exceeded',
    timeout: 'Timed out', network_error: 'Network error', not_found: 'Not found', source_not_found: 'Source not found', unauthorized: 'Not signed in',
    forbidden: 'Not allowed', configuration_invalid: 'Invalid configuration', parse_failed: 'Could not read the content', executor_error: 'Processing failed',
    collection_unsupported: 'Collection is not supported for this source', revision_changed: 'Changed elsewhere, try again', admission_expired: 'The request expired',
    unknown: 'Unknown error',
  },
  'vi-vi': {
    invalid_credential: 'Thông tin xác thực không hợp lệ', provider_rejected: 'Nhà cung cấp đã từ chối yêu cầu', provider_unavailable: 'Nhà cung cấp không khả dụng',
    provider_response_invalid: 'Nhà cung cấp trả về phản hồi không đọc được', provider_rate_limited: 'Nhà cung cấp đang giới hạn tần suất yêu cầu', rate_limited: 'Bị giới hạn tần suất',
    provider_collection_failed: 'Thu thập từ nhà cung cấp thất bại', provider_revoked: 'Quyền truy cập đã bị thu hồi tại nhà cung cấp', quota_exceeded: 'Vượt hạn mức',
    timeout: 'Hết thời gian chờ', network_error: 'Lỗi mạng', not_found: 'Không tìm thấy', source_not_found: 'Không tìm thấy nguồn', unauthorized: 'Chưa đăng nhập',
    forbidden: 'Không được phép', configuration_invalid: 'Cấu hình không hợp lệ', parse_failed: 'Không đọc được nội dung', executor_error: 'Xử lý thất bại',
    collection_unsupported: 'Nguồn này không hỗ trợ thu thập', revision_changed: 'Đã thay đổi ở nơi khác, hãy thử lại', admission_expired: 'Yêu cầu đã hết hạn',
    unknown: 'Lỗi không xác định',
  },
} as const;
