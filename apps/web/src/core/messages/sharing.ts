/** Bilingual `sharing` messages for document and brief sharing. Add keys for this area here only. */
export const sharingMessages = {
  'en-us': {
    share: 'Share', shareAction: 'Share', revoke: 'Revoke', shared: 'Shared', members: 'Workspace members',
    titleDocument: 'Share document', titleBrief: 'Share brief', loading: 'Loading…', noMembers: 'No other members in this workspace yet.',
    memberFallback: 'Member {id}', shareWith: 'Share with {member}', revokeFor: 'Revoke access for {member}',
    semanticsDocument: 'Choose members one by one. A share includes the current version and later versions of this document while the share is active. Revoking removes access immediately.',
    semanticsBrief: 'Choose members one by one. A share binds this saved brief (revision {revision}) only; a regenerated brief is not shared automatically.',
    evidenceNotShared: 'Share these cited documents with this member first, then share the brief.',
    errorRole: 'Only the workspace owner can change sharing.', errorInvisible: 'This item is no longer available.',
    errorRefresh: 'Something changed in the meantime. The list was refreshed; try again.', errorFailed: 'Could not complete the sharing request.',
    downloadFailed: 'Could not download the original file.',
    sharedBriefs: 'Briefs shared with you', noSharedBriefs: 'No briefs have been shared with you for this day.', briefMeta: 'Revision {revision} · {time}',
  },
  'vi-vi': {
    share: 'Chia sẻ', shareAction: 'Chia sẻ', revoke: 'Thu hồi', shared: 'Đã chia sẻ', members: 'Thành viên không gian làm việc',
    titleDocument: 'Chia sẻ tài liệu', titleBrief: 'Chia sẻ bản tóm tắt', loading: 'Đang tải…', noMembers: 'Chưa có thành viên nào khác trong không gian làm việc này.',
    memberFallback: 'Thành viên {id}', shareWith: 'Chia sẻ với {member}', revokeFor: 'Thu hồi quyền của {member}',
    semanticsDocument: 'Chọn từng thành viên. Khi còn hiệu lực, lượt chia sẻ bao gồm phiên bản hiện tại và các phiên bản sau của tài liệu này. Thu hồi sẽ gỡ quyền truy cập ngay lập tức.',
    semanticsBrief: 'Chọn từng thành viên. Lượt chia sẻ chỉ gắn với bản tóm tắt đã lưu này (bản sửa {revision}); bản tóm tắt tạo lại sẽ không tự động được chia sẻ.',
    evidenceNotShared: 'Hãy chia sẻ các tài liệu được trích dẫn dưới đây cho thành viên này trước, rồi chia sẻ bản tóm tắt.',
    errorRole: 'Chỉ chủ sở hữu không gian làm việc mới có thể thay đổi chia sẻ.', errorInvisible: 'Mục này không còn khả dụng.',
    errorRefresh: 'Đã có thay đổi trong lúc đó. Danh sách đã được làm mới; hãy thử lại.', errorFailed: 'Không thể hoàn tất yêu cầu chia sẻ.',
    downloadFailed: 'Không thể tải tệp gốc.',
    sharedBriefs: 'Bản tóm tắt được chia sẻ với bạn', noSharedBriefs: 'Chưa có bản tóm tắt nào được chia sẻ với bạn cho ngày này.', briefMeta: 'Bản sửa {revision} · {time}',
  },
} as const;
