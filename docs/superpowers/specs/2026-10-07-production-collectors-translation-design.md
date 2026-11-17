# Production, workspaces, free collectors và translation — design baseline

Ngày: 2026-10-07. Trạng thái: ghi lại phạm vi người dùng đã chọn; các mặc định kỹ thuật bên dưới là đề xuất cụ thể để review, chưa triển khai.
Baseline source: `512a3fe68895a54f418a85a0ad5e4df3ccad8144`, checkout `D:/Project/Umwelt-OS`.
Spec nền: `specs/personal-intelligence-os-spec-v2.md`, đặc biệt sections 156–166. Quyết định của người dùng trong phiên này thay thế giả định single-user và n8n bắt buộc cho polling chuẩn; các invariant provenance/privacy còn hiệu lực.

## 1. Intent và phạm vi đã chọn

- Review production readiness và hoàn thiện đường thu thập có thể vận hành thực tế.
- Một private workspace mặc định cho mỗi tài khoản; có thể được mời vào nhiều workspace. Người được mời chỉ thấy nội dung chia sẻ rõ ràng; Chat, Memory và dữ liệu cá nhân không tự động chia sẻ.
- Pilot invite-only: 25 tài khoản/default workspaces, mục tiêu 5 workspace hoạt động đồng thời trên 2 core/8 GB. Đây là workload nghiệm thu, không phải capacity đã chứng minh.
- Thu thập tin Việt Nam/quốc tế, tài chính/vĩ mô, crypto, chứng khoán trong phạm vi nguồn miễn phí hợp lệ. Không tự mua gói, dùng trial có billing hoặc fallback trả phí.
- Python + ARQ là backend collection mặc định; giữ n8n tùy chọn cho workflow tùy biến.
- Dịch tự động News ở trang hiện tại và Daily Brief khi bật. Setting chung trong workspace, giữ nguyên bản gốc/citation, dùng OmniRoute và privacy controls.
- Không thực hiện triển khai, migration, restart, commit hoặc push trong công việc viết plan này.

## 2. Hiện trạng đã đọc từ source

| Quan sát | Bằng chứng | Hệ quả |
| --- | --- | --- |
| Owner là singleton | core/auth/models.py: Owner constraint id = 1; settings/models.py owner_id = 1 | Multiworkspace là thay đổi backend xuyên suốt |
| Nguồn và Documents chưa có tenant scope | modules/sources/models.py; modules/knowledge/documents/models.py | Membership không tự tạo data isolation |
| n8n quản lý activation và lịch | modules/connectors/n8n.py; activation.py; provisioning_routes.py | Không thể chỉ bỏ container |
| Native provider-fetch thường gọi lại Python | infrastructure/n8n/workflows/provider-fetch.json | Có thể tái sử dụng collector hiện có |
| REST workflow thực hiện fetch/pagination | infrastructure/n8n/workflows/rest.json | Native cần giữ network/auth/pagination guarantees |
| ARQ đã có | apps/worker/main.py | Dùng queue hiện có, không tạo workflow engine mới |
| Sai dispatch world provider | build_workflow native set thiếu alpha_vantage/open_meteo | Legacy backend có thể chọn REST rồi bị 409 |
| Sai envelope World Data | providers/world_data.py ghi world_data top-level; ingestion/public.py đọc provider_record | Phải sửa trusted normalization trước thêm provider |
| Settings/News/Brief có owner module rõ ràng | modules/settings; modules/news; modules/dashboard/briefs.py | Translation là derived view, không ghi đè bản gốc |
| Docs activation lỗi thời | docs/connectors.md dòng 33; .env.example N8N_API_KEY placeholder | Cập nhật setup cùng implementation |

Ledger hiện ghi P12/P13 code/test stage đã hoàn thành với live/capacity/restore còn thiếu. Đây là evidence lịch sử của baseline; không suy ra thay đổi mới đã đạt production. Không mở lại mọi task cũ hoặc làm mới 34 UI gaps không liên quan.

## 3. Mặc định thiết kế cần giữ thống nhất

### Identity và chia sẻ

Giữ bảng SQL `owner` và ID integer để giảm migration churn; bỏ ràng buộc singleton cho account, gọi là user ở API mới. Tạo `workspaces`, `workspace_memberships`, `workspace_invitations`, `workspace_resource_shares`. Mỗi user chỉ có một default workspace; v1 không cung cấp tạo thêm workspace sở hữu hoặc chuyển ownership. User có thể tham gia nhiều workspace của người khác.

Vai trò v1: owner và member. Owner quản lý nguồn, settings, lời mời, nội dung và shares của workspace. Member chỉ đọc Document hoặc saved Brief được chia sẻ cụ thể; không được quản trị Sources, AI, credentials hoặc jobs. Chat/Memory của user sử dụng default workspace của họ; không cung cấp shared Chat/Memory trong workspace được mời.

Chỉ owner được chia sẻ Document/Brief hiện có, không chia sẻ toàn bộ Source ở v1. Chia sẻ Brief chỉ thành công nếu tất cả bằng chứng của đúng revision đều được chia sẻ cho cùng người nhận; không tự chia sẻ bằng chứng. Thu hồi share/membership có hiệu lực cho read, search, SSE, translation cache và export tiếp theo. Bản đã tải về bên ngoài không thể thu hồi; UI nói đúng giới hạn này.

Lời mời dùng email chuẩn hóa, token random lưu hash, hết hạn 7 ngày và single-use; owner copy link để gửi. Hệ thống không tự gửi email. Người mới chỉ đăng ký qua invitation token; đăng nhập có identifier + password hoặc Google identity nếu operator đã cấu hình. Owner cũ giữ đường password-only cho tài khoản bootstrap duy nhất, tài khoản mới không dùng đường đó. Operator bootstrap user 1 quản trị instance; workspace owner không được mở secrets/system backup của instance.

Mỗi request dữ liệu có principal user_id + workspace_id đã xác thực. Dùng X-Workspace-ID trên fetch; GET SSE dùng workspace_id query vì EventSource không hỗ trợ arbitrary header. Cả hai đều phải kiểm tra membership server-side. Không có context thì chỉ cho legacy bootstrap owner truy cập default workspace; user mới nhận 400 workspace_required. Route membership/invite/auth là account-scoped. Job không dùng ambient global owner; payload chứa workspace/user hoặc source ID có scope được tải và đối chiếu lại.

### Collector native và n8n

PostgreSQL giữ desired/applied revision, lịch, durable request và lease. Redis/ARQ chỉ dispatch. Reuse SourceIngestionState collection lease; không tạo lease song song cho cùng nguồn. PostgreSQL giữ thêm hai global admission slots với fencing token để giới hạn hai nguồn, tối đa một source/workspace.

Backend `native | n8n` là thuộc tính provisioning, không nằm trong provider config. Migration gán n8n cho các nguồn đã có workflow; nguồn mới mặc định native khi hỗ trợ. Không tự chuyển backend trên lỗi. API/UI vẫn Save & enable, Collect now, pause, deactivate. Native không kiểm tra N8N_API_KEY. Custom n8n v1 là workflow operator quản lý bằng template/API hiện có; không nhúng editor hoặc cho member nhập workflow JSON.

Trình tự chuyển: tạm ngừng nhận việc mới → tăng revision/fence → đợi hoặc fence lượt đang chạy → xác nhận workflow cũ inactive → kích hoạt backend mới. Deactivation n8n không rõ kết quả thì giữ trạng thái reconciliation_required; không bật native. Rollback là thao tác đối xứng, không quay ngược database tùy tiện.

Polling nguồn thông thường dùng interval hiện có 15/30/60/360/1440 phút. Giá crypto mặc định 15 phút, RSS 30 phút, vĩ mô/FX/Fear&Greed/Alpha Vantage 1440 phút; đây là dashboard thông tin, không phải terminal giao dịch realtime. Retry chỉ network/429/5xx với bounded backoff, tôn trọng Retry-After; auth/schema/terms lỗi là dừng cần sửa. Không tiến cursor nếu batch chưa được durable ingestion chấp nhận.

### Dữ liệu miễn phí

Provider catalog phân biệt endpoint free, key/quota, phạm vi license, attribution, trạng thái code và runtime acceptance. Tất cả preset mới opt-in, không tự fetch khi cài. Deployment use-case là personal/noncommercial/commercial/unknown do owner khai báo; unknown không bật provider bị hạn chế personal/noncommercial. Không dùng khai báo để bỏ qua điều khoản; catalog bắt buộc ghi tài liệu eligibility.

V1 triển khai BBC/VnExpress qua RSS hiện có; World Bank, Frankfurter, ECB, Binance, Alternative.me, USGS bằng adapter nhỏ. CoinPaprika/CoinGecko là tùy chọn sau gate quyền sử dụng. GDELT experimental, không fallback mặc định. HN dùng REST mapping. Alpha Vantage existing EOD adapter được sửa; SEC filings, FRED, Twelve Data và VN stock quote provider giữ research-only, không hứa đã tích hợp.

Vnstock là tài liệu tham khảo về adapter, không cấp quyền dữ liệu upstream. Không dùng Yahoo unofficial làm nguồn cốt lõi. Không cài thêm CCXT/OpenBB/RSSHub chỉ để gọi vài endpoint; tham khảo hợp đồng và endpoint, giữ dependency footprint hiện có.

### Translation

Settings riêng, không gắn với UI locale: `enabled=false`, `target_language=vi`; v1 hỗ trợ `vi|en`. Cả News và Brief luôn cùng setting workspace; owner sửa bằng expected_revision, member chỉ được đọc trạng thái cần thiết để hiển thị bản dịch của nội dung họ được xem.

Batch News nhận tối đa 25 ID đang hiển thị (trang lớn chia batch), server tự tải nội dung đã lọc quyền. Mỗi Story chỉ dịch title/excerpt. Brief dịch content của đúng immutable revision; không tạo revision Brief mới chỉ vì đổi ngôn ngữ. Không nhận arbitrary text từ client. Không dịch toàn bộ kho hoặc dịch citation/URL/symbol/numeric values.

Lưu derived cache PostgreSQL, khóa gồm workspace + actor/visibility fingerprint + resource revision/content hash + target + settings/privacy/model revision + prompt version. Kiểm tra quyền trước cache hit, trước mỗi gateway attempt và trước publish. Job stale hoặc revoked không publish. Không cache cross-workspace. Dùng ModelGateway.structured, reasoning-small; không bypass privacy và không deploy model/gateway mới.

UI hiển thị original ngay; dịch chạy nền, trạng thái pending/ready/blocked/failed/unchanged. Có xem bản gốc. Model lỗi/không hỗ trợ structured hoặc privacy chặn giữ original. Translation không trở thành nguồn citation hoặc indexed source text. Cleanup cache đi qua owner deletion hooks; expiry 30 ngày, không thể resurrect nội dung sau purge.

## 4. Acceptance và ranh giới

Code/build, fixture integration, live endpoint, full encrypted restore và đo capacity là các cột độc lập. Nếu thiếu key/điều kiện provider/host thì ghi blocked ở cột tương ứng; không đổi thành PASS. Nội dung miễn phí không suy ra OmniRoute/inference miễn phí.

Nghiệm thu phải có: invite/revoke/cross-workspace isolation; collector không n8n; migration backend không double-run; source purge trong khi collect/translate; số liệu/citation nguyên vẹn; setup sạch; migration upgrade + safe downgrade rejection; restart recovery; snapshot/restore; test tải 25 users/5 active workspaces.

Các plan con trong master là implementation detail của baseline này. Chưa có task nào được thực thi.

