# Thông báo quyền riêng tư

Bản nháp ngày 19 tháng 10 năm 2026. Đơn vị vận hành: **[LEGAL NAME]**. Liên hệ: **[CONTACT EMAIL]**. Khu vực pháp lý: **[JURISDICTION]**. Cần hoàn thiện các thông tin này và rà soát pháp lý trước khi cung cấp bản triển khai cho người khác.

## Ai kiểm soát bản triển khai

Umwelt-OS là phần mềm tự lưu trữ. Cá nhân hoặc tổ chức vận hành bản triển khai quyết định mục đích xử lý, hạ tầng, quyền truy cập, tích hợp và thời hạn lưu trữ. Dự án phần mềm không mặc nhiên là đơn vị vận hành bản triển khai của bạn. Hãy hỏi đơn vị vận hành về cơ sở pháp lý, quyền theo luật, việc chuyển dữ liệu ra nước ngoài, vị trí lưu trữ và cơ quan tiếp nhận khiếu nại. Đơn vị vận hành phải cung cấp thông báo riêng theo khu vực pháp lý và xin sự đồng ý khi luật yêu cầu.

## Dữ liệu trong không gian làm việc

Bản triển khai lưu mã định danh tài khoản, mật khẩu đã băm, phiên đăng nhập, tùy chọn, tư cách thành viên và lời mời. Nếu bật đăng nhập Google, hệ thống lưu nhà phát hành định danh, mã chủ thể và email; đăng nhập này không cấp quyền đọc nội dung Google. Hệ thống xử lý nội dung và nguồn gốc được nhập, tệp gốc, văn bản trích xuất, đoạn văn, vector nhúng, thực thể và quan hệ, chỉ mục tìm kiếm, cuộc trò chuyện, tệp đính kèm, kết quả AI, bản dịch, bản tin, ghi chú, tác vụ, tự động hóa, cấu hình nguồn, trạng thái kết nối và hồ sơ vận hành. Một số nguồn có thể chứa dữ liệu của người khác. Chỉ nhập dữ liệu mà bạn có quyền xử lý.

Dữ liệu này phục vụ xác thực, thu thập, tìm kiếm, trích dẫn, hỗ trợ AI, tính năng không gian làm việc, phân quyền và vận hành. Dữ liệu dẫn xuất có thể chứa lại nội dung gốc. Nhật ký và siêu dữ liệu có thể chứa mã định danh; việc che thông tin không bảo đảm mọi nhật ký của máy chủ đều không có dữ liệu cá nhân.

## Cookie và trình duyệt

Cookie phục vụ phiên đăng nhập và chống giả mạo yêu cầu. Bộ nhớ trình duyệt lưu tùy chọn giao diện/ngôn ngữ và có thể giữ bộ đệm ứng dụng, bản nháp và nội dung không gian làm việc. Kho mã này không thêm cookie quảng cáo hay SDK phân tích quảng cáo. Máy chủ, proxy hoặc tiện ích trình duyệt có thể xử lý thêm dữ liệu; đơn vị vận hành phải công bố riêng. Đăng xuất hoặc xóa tài khoản không xóa được ảnh chụp màn hình, lịch sử trình duyệt, tệp tải xuống hoặc bản xuất của người khác. Hãy kiểm tra bộ nhớ trang và tệp tải xuống trên thiết bị dùng chung.

## Thông tin xác thực và bảo mật

Khóa API của nhà cung cấp, thông tin OAuth, token kết nối và bí mật hạ tầng được dùng trong cấu hình máy chủ hoặc nơi lưu trữ backend được bảo vệ. Khi bạn nhập khóa qua biểu mẫu, khóa được gửi đến bản triển khai. Đơn vị vận hành chịu trách nhiệm về bí mật triển khai, khóa mã hóa, quyền truy cập, TLS, bản vá và nhật ký. Không phần mềm nào bảo đảm an toàn tuyệt đối. Không đưa bí mật vào prompt, URL công khai hoặc tài liệu nhập.

## Tích hợp tùy chọn và bên nhận dữ liệu

Khi bật nhà cung cấp AI, endpoint đó có thể nhận prompt, lịch sử trò chuyện và trích đoạn nguồn được chọn, đầu vào/kết quả công cụ trong tin nhắn, văn bản tạo vector nhúng, truy vấn/tài liệu để xếp hạng lại, lược đồ công cụ và tham số sinh nội dung, tùy tính năng được bật. Cổng trung gian và đơn vị vận hành mô hình phía sau là các bên nhận riêng; đơn vị vận hành phải xác định cả hai. Công cụ và tích hợp có thể gửi tham số hoặc nội dung trong phạm vi yêu cầu. Tìm kiếm web gửi nguyên văn truy vấn và tùy chọn; một máy chủ SearXNG có thể chuyển tiếp đến các công cụ tìm kiếm được chọn trong cấu hình riêng của máy chủ đó. Bộ thu thập gửi mã nguồn, bộ lọc, URL và thông tin xác thực cần thiết. Đăng nhập Google trao đổi dữ liệu ủy quyền với Google, không cấp quyền thu thập Gmail, Calendar hay Drive. Yêu cầu đến SEC EDGAR chứa email liên hệ do đơn vị vận hành nhập trong User-Agent.

Nếu bật Langfuse và cho phép gửi dữ liệu ra ngoài, bộ xuất này gửi siêu dữ liệu có giới hạn của lần gọi mô hình (tên mô hình, số token, thời gian và mã đối chiếu), không gửi nội dung prompt, tài liệu hay câu trả lời qua bộ xuất đó. Mỗi bên nhận kết nối mạng cũng có thể thấy IP nguồn, thời điểm, header và siêu dữ liệu yêu cầu; đó là dữ liệu tách biệt với nội dung ứng dụng gửi đi. Endpoint mô hình được gọi là cục bộ chỉ thực sự ở tại chỗ nếu cấu hình mạng và endpoint của bạn giữ dữ liệu tại chỗ.

Mở **Dịch vụ và luồng dữ liệu** bên dưới để xem danh mục triển khai bằng tiếng Anh. Có mã triển khai không có nghĩa dịch vụ đã được cấu hình hoặc đang hoạt động. Hosting, DNS, chứng chỉ TLS, tunnel và sao lưu tùy thuộc đơn vị vận hành. Cấu hình Docker đi kèm tắt telemetry Next.js và chẩn đoán/cá nhân hóa n8n; công cụ phát triển hoặc cách triển khai khác có thể khác.

## Lưu trữ, xuất và xóa

Nội dung và dữ liệu dẫn xuất được giữ đến khi xóa qua chức năng được hỗ trợ, quy trình xóa tài khoản hoặc quy trình lưu trữ của đơn vị vận hành. Chưa có thời hạn chung cho mọi loại dữ liệu. Bạn có thể xuất dữ liệu hiện được hỗ trợ; bản xuất có thể chứa dữ liệu gốc và dữ liệu dẫn xuất nhạy cảm. Bạn tự bảo vệ và xóa các bản xuất của mình.

Xóa tài khoản cần xác minh danh tính và điều kiện được phép, sau đó vô hiệu hóa truy cập thông thường, thu hồi phiên và dọn dẹp bất đồng bộ. Biên nhận trạng thái cho biết tiến độ hoặc lỗi; không được coi là xóa xong nếu bước bắt buộc thất bại. Giữ kín biên nhận. Nếu mất biên nhận, đơn vị vận hành có thể xem tiến độ đã ẩn thông tin và chạy lại bước lỗi, nhưng không thể tạo lại quyền xem bằng cookie biên nhận trong trình duyệt. Dữ liệu của không gian làm việc chung và tài khoản khác cần xử lý theo quyền sở hữu, không xóa toàn bộ dữ liệu của họ. Tài khoản khởi tạo để lại dấu kỹ thuật đã làm sạch và vô hiệu hóa để giữ cổng thiết lập công khai đóng. Người có quyền quản trị máy chủ có thể tạo quyền quản trị mới với không gian làm việc rỗng sau khi xóa hoàn tất; thao tác này không khôi phục nội dung hoặc phiên cũ. Biên nhận tồn tại bảy ngày sau khi hoàn tất.

Kho đồ thị giữ vĩnh viễn một dấu xóa kỹ thuật chỉ gồm mã phân vùng không mang nội dung và cờ đã xóa, không có nội dung nguồn hay liên kết đến tài khoản/không gian làm việc. Kho tệp gốc cũng giữ khóa/dấu xóa một byte vĩnh viễn, chỉ định danh bằng UUID không gian làm việc, không chứa thông tin tài khoản hay nội dung nguồn. Các dấu này ngăn yêu cầu đến trễ tạo lại nội dung đã xóa; bỏ dấu sẽ làm mất cơ chế bảo vệ đó.

Việc xóa áp dụng cho kho dữ liệu đang hoạt động được ứng dụng hỗ trợ và dọn bộ nhớ ứng dụng trong trình duyệt. Bản sao lưu, snapshot, nhật ký ngoài phạm vi ứng dụng, bí mật môi trường, tệp tải/xuất và dữ liệu ở nhà cung cấp ngoài cần xử lý riêng. Khôi phục bản sao cũ có thể đưa dữ liệu đã xóa trở lại; đơn vị vận hành phải áp dụng lại quyết định xóa khi khôi phục. Không có cam kết về xóa tại nhà cung cấp ngoài, loại trừ dữ liệu khỏi huấn luyện AI hoặc thời hạn lưu trữ của họ. Hãy xem điều khoản và cấu hình của từng bên trước khi bật tích hợp.

## Lựa chọn và quyền của bạn

Bạn có thể tránh tích hợp tùy chọn, giới hạn nguồn và quyền công cụ, chọn mô hình cục bộ phù hợp, xuất dữ liệu hoặc yêu cầu xóa. Liên hệ **[CONTACT EMAIL]** để yêu cầu truy cập, chỉnh sửa, phản đối, hạn chế, chuyển dữ liệu, xóa và các quyền theo luật khác. Đơn vị vận hành phải giải thích ngoại lệ hợp pháp và thời hạn phản hồi áp dụng; bản nháp này không tự đặt thời hạn hay từ bỏ quyền bắt buộc.
