/** Bilingual `routeErrors` messages for the error, not-found and loading route boundaries. */
export const routeErrorsMessages = {
  'en-us': {
    errorTitle: 'Something went wrong',
    errorBody: 'This page could not be displayed. Try again, or reload if the problem continues.',
    retry: 'Try again',
    notFoundTitle: 'Page not found',
    notFoundBody: 'The page you opened does not exist or has moved.',
    home: 'Back to Umwelt-OS',
    loading: 'Loading…',
  },
  'vi-vi': {
    errorTitle: 'Đã xảy ra lỗi',
    errorBody: 'Không thể hiển thị trang này. Hãy thử lại hoặc tải lại nếu sự cố vẫn tiếp diễn.',
    retry: 'Thử lại',
    notFoundTitle: 'Không tìm thấy trang',
    notFoundBody: 'Trang bạn mở không tồn tại hoặc đã được chuyển.',
    home: 'Quay lại Umwelt-OS',
    loading: 'Đang tải…',
  },
} as const;
