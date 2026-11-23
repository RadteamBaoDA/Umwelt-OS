# Dashboard: dịch tin tối giản + header gọn

## Context
Owner thấy UI dịch tin trên dashboard rối: nút "Dịch" luôn hiện trên mọi dòng tin (7 loại gadget), thanh "Dịch tất cả" luôn hiện trên mỗi gadget, header dashboard có 5 nút trong đó "Dịch: VI" và Density là thao tác cài đặt ít dùng. Mục tiêu: user tự chọn tin cần dịch (thủ công), ngôn ngữ đích đặt 1 lần ở Settings; dashboard chỉ để lộ thao tác hay dùng.

Quyết định của owner:
- Chọn tin để dịch, ngôn ngữ cố định (workspace setting, không đổi backend).
- Nút dịch từng tin: hiện khi hover/focus của dòng đó (cảm ứng: luôn hiện). Đã dịch → nút "Gốc/Bản dịch" luôn hiện.
- "Dịch tất cả": rời thân gadget, thành mục trong menu ⋯ của gadget.

Đề xuất của Claude (owner sửa được): header giữ Edit (+ Add gadget skin terminal); Density + Manage topics gom vào 1 nút ⋯; bỏ nút "Dịch: VI" khỏi header (đã có `/settings/translation`).

Non-goals: không đổi API/cache/worker dịch; không chọn ngôn ngữ mỗi lần; không đổi brief translate (`brief-text.tsx`).
Anti-goal: thao tác dịch khó tìm trên cảm ứng hoặc bàn phím (phải giữ `focus-visible`, `pointer-coarse`).

## Approaches đã cân
- Hover reveal (chọn): sửa 1 component dùng chung, diff nhỏ.
- Menu ⋯ mỗi dòng: gọn hơn nhưng thêm click + component mới ở 7 gadget. Loại.
- Checkbox chọn nhiều tin: chỉ feed có sẵn bulk bar; loại.

## Thay đổi

### 1. Nút dịch từng tin — `apps/web/src/modules/translations/translation-controls.tsx`
- `ItemTranslateButton` (:100): nhánh chưa dịch (`TranslateActionButton`) bọc class reveal theo dòng: `opacity-0 group-hover/item:opacity-100 group-focus-within/item:opacity-100 focus-visible:opacity-100 pointer-coarse:opacity-100`; khi `pending` hoặc có `issue` thì luôn hiện. Nhánh "Gốc/Bản dịch" giữ nguyên (luôn hiện).
- Thêm `group/item` vào phần tử gốc của dòng ở các nơi gọi: `feed-parts.tsx:49`, `table-panel.tsx:198`, `text-panel.tsx:73`, `video-panel.tsx:400`, `media-parts.tsx:490`, `job-list.tsx:89`, `news/story-list.tsx` (TranslateActionButton per story).
- Mẫu class tham khảo: `REVEAL_ON_HOVER` ở `gadget-frame.tsx:82` (đang dùng group/gadget).

### 2. "Dịch tất cả" vào menu ⋯ gadget
- Hiện `EditMenu` (`gadget-frame.tsx:388`) chỉ hiện ở edit mode → cần menu ⋯ ở view mode.
- Thêm context body→frame theo mẫu `gadget-freshness.tsx` (Provider ở frame, hook ở renderer): ví dụ `gadget-actions.tsx` với `useReportGadgetActions(actions)`; frame render nút ⋯ (`REVEAL_ON_HOVER`, cạnh Ask/Expand trong `HeaderActions` :425) chỉ khi có action. Export hook qua `dashboard/public-api.ts`.
- Mỗi gadget đang dùng `CollectionTranslationBar` gọi hook với action "Dịch tất cả" (disabled khi `!controller.enabled` hoặc không còn tin chưa dịch; label tiến độ khi pending).
- `CollectionTranslationBar` (:150): bỏ nút Translate all; còn `TranslatedBadge` (khi có kết quả) + `TranslationRunProgress` (khi đang chạy). `TranslationStatusBanner` giữ nguyên.
- News `story-list.tsx` có đường translateAll riêng → chuyển sang cùng hook.

### 3. Header dashboard — `apps/web/src/modules/dashboard/dashboard-header.tsx:191`
- Bỏ `{props.translationControl}`; bỏ inject ở `dashboard-page.tsx:160`. `DashboardTranslationControl` (translation-controls.tsx:274): xoá nếu impact cho thấy không còn caller (giữ link Settings ở chỗ khác nếu cần).
- `DensityToggle` + nút "Manage topics" → 1 `DropdownMenu` ⋯ (Density là item toggle, Manage chỉ khi `isOwner`). Giữ Edit + Add gadget.
- i18n: key mới cho menu (`dashboard.moreActions`, …) ở `apps/web/locales/{en-us,vi-vi}/`; dùng lại `translations.translateAll`.

## Quy trình repo
- GitNexus `impact` upstream trước khi sửa: `ItemTranslateButton`, `CollectionTranslationBar`, `DashboardTranslationControl`, `HeaderActions`, `DashboardHeader`, `DensityToggle`; báo HIGH/CRITICAL.
- JSDoc cho mọi component/hook mới hoặc đổi. Token ngữ nghĩa, shadcn primitives (skill `umwelt-os-ui-system`).
- Lưu design ngắn vào `docs/superpowers/specs/2026-11-23-dashboard-translate-minimal-design.md`.
- Implement: agent `implementer` (Haiku). Commit local, không push.

## Verification
- Vitest: cập nhật/ thêm test `translation-controls` (nút ẩn có class reveal, hiện khi pending/issue), `gadget-frame.test.tsx` (menu ⋯ view mode chỉ hiện khi gadget báo action, click gọi translateAll), header test (Density/Manage trong menu, không còn nút Dịch). Chạy `pnpm vitest` các file liên quan, `tsc`, `eslint`.
- Dev server (preview_start): hover dòng tin → nút Dịch hiện; không hover → ẩn; Tab bàn phím tới được; menu ⋯ gadget → Dịch tất cả chạy, badge + tiến độ hiện; header còn Edit + ⋯; resize mobile → nút dịch luôn hiện. Screenshot làm bằng chứng.
- `detect_changes` trước commit.
