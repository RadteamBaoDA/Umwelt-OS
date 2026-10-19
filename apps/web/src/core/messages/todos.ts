/** Bilingual `todos` messages for the conversation task panel. */
export const todosMessages = {
  'en-us': {
    title: 'Tasks', count: '{done} of {total} done', empty: 'No tasks yet. The assistant can add some, or add your own.',
    loading: 'Loading tasks', loadFailed: 'Could not load tasks.', retry: 'Retry', mutateFailed: 'Could not save the change. It was undone.',
    expand: 'Show tasks', collapse: 'Hide tasks', addLabel: 'New task', addPlaceholder: 'Add a task', add: 'Add',
    complete: 'Mark "{title}" as done', reopen: 'Mark "{title}" as not done', edit: 'Edit "{title}"', save: 'Save', cancel: 'Cancel',
    remove: 'Delete "{title}"', editLabel: 'Task title', limit: 'A conversation can hold up to {max} tasks.',
  },
  'vi-vi': {
    title: 'Công việc', count: 'Đã xong {done}/{total}', empty: 'Chưa có công việc. Trợ lý có thể thêm, hoặc bạn tự thêm.',
    loading: 'Đang tải công việc', loadFailed: 'Không thể tải công việc.', retry: 'Thử lại', mutateFailed: 'Không thể lưu thay đổi. Đã hoàn tác.',
    expand: 'Hiện công việc', collapse: 'Ẩn công việc', addLabel: 'Công việc mới', addPlaceholder: 'Thêm công việc', add: 'Thêm',
    complete: 'Đánh dấu "{title}" là đã xong', reopen: 'Đánh dấu "{title}" là chưa xong', edit: 'Sửa "{title}"', save: 'Lưu', cancel: 'Hủy',
    remove: 'Xóa "{title}"', editLabel: 'Tiêu đề công việc', limit: 'Một cuộc trò chuyện có tối đa {max} công việc.',
  },
} as const;
