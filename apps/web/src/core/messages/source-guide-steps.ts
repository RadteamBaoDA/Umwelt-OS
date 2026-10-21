/**
 * Step-by-step setup guides for sources the owner configures by hand (generic types, scoped and keyed presets).
 * Shape: `guideSteps.<provider>.s1..sN`; ProviderGuide reads them with `t.has` so any provider may have 1-8 steps.
 * Nested objects (not arrays) keep next-intl's message type valid. Built-in toggles need no guide.
 * Keyed guides name the exact field to paste into; no guide claims a quota or licence the catalog does not carry.
 */
export const sourceGuideSteps = {
  'en-us': {
    generic: {
      s1: 'Open the provider’s own documentation and note the endpoint and the scope it needs.',
      s2: 'Fill in the scope fields shown below exactly as the provider spells them.',
      s3: 'Accept the terms below (declared use) so the source is allowed to run.',
      s4: 'Press Test connection to validate the saved draft, then activate the source.',
    },
    genericKeyed: {
      s1: 'Create a free account at the provider and generate an API key.',
      s2: 'Paste the key into the API key field below; it is encrypted and never shown again.',
      s3: 'Fill in the scope fields, then accept the terms below.',
      s4: 'Press Test connection to validate the draft, then activate the source.',
    },
    rss: {
      s1: 'Find the site’s feed address: look for an RSS or Atom link, or try adding /feed or /rss to the site address.',
      s2: 'Paste the full https feed URL into Source URL.',
      s3: 'Choose a schedule; 15 to 60 minutes is enough for most news feeds.',
      s4: 'Press Test connection, then activate the source.',
    },
    rest: {
      s1: 'Find a JSON API endpoint that returns a list of items and open it in a browser to see the shape.',
      s2: 'Paste the https endpoint into API URL and set Items path to the list’s name (for example items).',
      s3: 'Set the ID and content field names to match one item; if the API needs a header key, choose Header authentication and paste the key.',
      s4: 'Press Test connection, then activate the source.',
    },
    web: {
      s1: 'Pick a public page whose text you want to track.',
      s2: 'Paste the https page address into Page URL.',
      s3: 'Keep the page limits low; only enable JavaScript rendering if the page is empty without it.',
      s4: 'Press Test connection, then activate the source.',
    },
    mcp: {
      s1: 'Get the address of an MCP server you trust and the list of tools it exposes.',
      s2: 'Add the server in MCP settings, review its tools, and enable only the read tools you need.',
      s3: 'Create the source against that server and choose a schedule.',
      s4: 'Press Test connection, then activate the source.',
    },
    youtube: {
      s1: 'Open the channel on YouTube, choose Share, then Copy channel ID (it starts with UC and is 24 characters).',
      s2: 'Paste it into Channel ID.',
      s3: 'Press Test connection, then activate the source.',
    },
    arxiv: {
      s1: 'Pick an arXiv category code such as cs.AI or q-fin.GN from arxiv.org/category_taxonomy.',
      s2: 'Paste exactly one category into Category.',
      s3: 'Press Test connection, then activate the source.',
    },
    huggingface: {
      s1: 'Open the author or organisation on huggingface.co and copy the name from the address.',
      s2: 'Paste it into Author; new and updated models by that author are collected.',
      s3: 'Press Test connection, then activate the source.',
    },
    github_releases: {
      s1: 'Open the repository on GitHub and note the owner and repository name from the address.',
      s2: 'Fill in Owner and Repository; public releases need no sign-in.',
      s3: 'Press Test connection, then activate the source.',
    },
    github: {
      s1: 'Connect your GitHub account first (the Connect button), then pick the repository to read.',
      s2: 'Fill in Owner and Repository and tick the kinds of activity to collect.',
      s3: 'Set how many days of history to fetch on the first run (up to 365).',
      s4: 'Press Test connection, then activate the source.',
    },
    telegram: {
      s1: 'Create a bot with @BotFather in Telegram and copy the bot token it gives you.',
      s2: 'Add the bot as an administrator of each channel you want to read.',
      s3: 'Paste the bot token into the credential field and list the channel IDs (negative numbers) in Channel IDs.',
      s4: 'Press Test connection, then activate the source.',
    },
    alpha_vantage: {
      s1: 'Get a free key at alphavantage.co/support/#api-key (a name and email are enough).',
      s2: 'Paste the key into the API key field.',
      s3: 'Enter up to the allowed number of ticker symbols, the currency and the exchange time zone.',
      s4: 'The free plan allows a small number of calls per day, so keep the schedule at once a day.',
      s5: 'Press Test connection, then activate the source.',
    },
    open_meteo: {
      s1: 'Look up the latitude and longitude of the place you want (any map site shows them).',
      s2: 'Enter them in Latitude and Longitude, plus an IANA time zone such as Asia/Ho_Chi_Minh.',
      s3: 'List the hourly metrics you want, separated by commas (for example temperature_2m).',
      s4: 'Open-Meteo is free for non-commercial use and needs no key. Press Test connection, then activate.',
    },
    google_news: {
      s1: 'Decide the search words you want to follow (up to 200 characters).',
      s2: 'Enter them in Search query; do not type site: here, use the site selector instead.',
      s3: 'Optionally restrict to one publisher and choose the language and region.',
      s4: 'Accept the terms below, press Test connection, then activate the source.',
    },
    sec_edgar: {
      s1: 'Find the company’s 10-digit CIK at sec.gov/edgar/searchedgar/companysearch (pad with leading zeros).',
      s2: 'Enter it in CIK, and your own contact email in Contact email; SEC requires it to identify your requests.',
      s3: 'Accept the terms below, press Test connection, then activate the source.',
    },
    openalex: {
      s1: 'Think of a topic or phrase that scholarly works should match.',
      s2: 'Enter it in Search query; no key is needed for OpenAlex basic use.',
      s3: 'Accept the terms below, press Test connection, then activate the source.',
    },
    coingecko: {
      s1: 'Create a free account at coingecko.com/en/api and add a Demo API key in the developer dashboard.',
      s2: 'Copy the key (it starts with CG-) and paste it into the API key field; it is encrypted and never shown again.',
      s3: 'Accept the terms below. Show the “Powered by CoinGecko” credit wherever you share the data.',
      s4: 'Press Test connection, then activate the source.',
    },
    fred: {
      s1: 'Create a free account at fredaccount.stlouisfed.org and request an API key under My Account.',
      s2: 'Paste the 32-character key into the API key field.',
      s3: 'Find the series ID on fred.stlouisfed.org (for example DFF for the fed funds rate, CPIAUCSL for CPI) and enter it in Series ID.',
      s4: 'One source follows one series; create another source for another series. Accept the terms, test, then activate.',
    },
    finnhub_news: {
      s1: 'Create a free account at finnhub.io and copy the API key from the dashboard.',
      s2: 'Paste it into the API key field. The free plan is for personal use.',
      s3: 'Accept the terms below, press Test connection, then activate the source.',
    },
    newsapi: {
      s1: 'Register at newsapi.org/register and copy your API key.',
      s2: 'Paste it into the API key field. The free Developer plan is for development use only and limits requests per day.',
      s3: 'Accept the terms below (this source needs an instance operator review), test, then activate.',
    },
    openweathermap: {
      s1: 'Create a free account at openweathermap.org and copy the key from My API keys (new keys can take a couple of hours to start working).',
      s2: 'Paste it into the API key field.',
      s3: 'Enter the Latitude and Longitude of the place to follow.',
      s4: 'Accept the terms below, press Test connection, then activate the source.',
    },
    openaq: {
      s1: 'Create a free account at explore.openaq.org and copy your API key from the account page.',
      s2: 'Paste it into the API key field.',
      s3: 'Find a monitoring station on the OpenAQ map; the number at the end of its address is the Location ID. Enter it.',
      s4: 'Accept the terms below, press Test connection, then activate the source.',
    },
    nasa_firms: {
      s1: 'Request a free MAP_KEY at firms.modaps.eosdis.nasa.gov/api/map_key (an email is enough).',
      s2: 'Paste the key into the API key field.',
      s3: 'Enter the three-letter ISO country code (for example VNM) in Country code.',
      s4: 'Large countries can return more hotspots than fit one run; only the strongest are kept. Accept the terms, test, then activate.',
    },
  },
  'vi-vi': {
    generic: {
      s1: 'Mở tài liệu của nhà cung cấp và ghi lại endpoint cùng phạm vi cần khai báo.',
      s2: 'Điền các trường phạm vi bên dưới đúng như nhà cung cấp quy định.',
      s3: 'Chấp nhận điều khoản bên dưới (mục đích sử dụng) để nguồn được phép chạy.',
      s4: 'Nhấn Kiểm tra kết nối để xác thực bản nháp đã lưu, rồi kích hoạt nguồn.',
    },
    genericKeyed: {
      s1: 'Tạo tài khoản miễn phí tại nhà cung cấp và tạo một khóa API.',
      s2: 'Dán khóa vào trường Khóa API bên dưới; khóa được mã hóa và không hiển thị lại.',
      s3: 'Điền các trường phạm vi, rồi chấp nhận điều khoản bên dưới.',
      s4: 'Nhấn Kiểm tra kết nối để xác thực bản nháp, rồi kích hoạt nguồn.',
    },
    rss: {
      s1: 'Tìm địa chỉ nguồn cấp của trang: tìm liên kết RSS hoặc Atom, hoặc thử thêm /feed hay /rss vào địa chỉ trang.',
      s2: 'Dán đầy đủ địa chỉ https của nguồn cấp vào URL nguồn.',
      s3: 'Chọn lịch chạy; 15 đến 60 phút là đủ cho hầu hết nguồn tin.',
      s4: 'Nhấn Kiểm tra kết nối, rồi kích hoạt nguồn.',
    },
    rest: {
      s1: 'Tìm một endpoint API JSON trả về danh sách mục và mở nó trong trình duyệt để xem cấu trúc.',
      s2: 'Dán endpoint https vào URL API và đặt Đường dẫn mục là tên của danh sách (ví dụ items).',
      s3: 'Đặt tên trường ID và nội dung khớp với một mục; nếu API cần khóa trong header, chọn Xác thực bằng header và dán khóa.',
      s4: 'Nhấn Kiểm tra kết nối, rồi kích hoạt nguồn.',
    },
    web: {
      s1: 'Chọn một trang công khai có nội dung bạn muốn theo dõi.',
      s2: 'Dán địa chỉ https của trang vào URL trang.',
      s3: 'Giữ giới hạn trang ở mức thấp; chỉ bật hiển thị JavaScript nếu trang trống khi không có nó.',
      s4: 'Nhấn Kiểm tra kết nối, rồi kích hoạt nguồn.',
    },
    mcp: {
      s1: 'Lấy địa chỉ máy chủ MCP bạn tin cậy và danh sách công cụ nó cung cấp.',
      s2: 'Thêm máy chủ trong cài đặt MCP, xem lại các công cụ và chỉ bật các công cụ đọc bạn cần.',
      s3: 'Tạo nguồn trên máy chủ đó và chọn lịch chạy.',
      s4: 'Nhấn Kiểm tra kết nối, rồi kích hoạt nguồn.',
    },
    youtube: {
      s1: 'Mở kênh trên YouTube, chọn Chia sẻ, rồi Sao chép ID kênh (bắt đầu bằng UC, dài 24 ký tự).',
      s2: 'Dán vào ID kênh.',
      s3: 'Nhấn Kiểm tra kết nối, rồi kích hoạt nguồn.',
    },
    arxiv: {
      s1: 'Chọn mã danh mục arXiv như cs.AI hoặc q-fin.GN từ arxiv.org/category_taxonomy.',
      s2: 'Dán đúng một danh mục vào Danh mục.',
      s3: 'Nhấn Kiểm tra kết nối, rồi kích hoạt nguồn.',
    },
    huggingface: {
      s1: 'Mở tác giả hoặc tổ chức trên huggingface.co và sao chép tên từ địa chỉ.',
      s2: 'Dán vào Tác giả; các mô hình mới và được cập nhật của tác giả đó sẽ được thu thập.',
      s3: 'Nhấn Kiểm tra kết nối, rồi kích hoạt nguồn.',
    },
    github_releases: {
      s1: 'Mở kho mã trên GitHub và ghi lại chủ sở hữu và tên kho từ địa chỉ.',
      s2: 'Điền Chủ sở hữu và Kho mã; bản phát hành công khai không cần đăng nhập.',
      s3: 'Nhấn Kiểm tra kết nối, rồi kích hoạt nguồn.',
    },
    github: {
      s1: 'Kết nối tài khoản GitHub trước (nút Kết nối), rồi chọn kho mã cần đọc.',
      s2: 'Điền Chủ sở hữu và Kho mã, đánh dấu các loại hoạt động cần thu thập.',
      s3: 'Đặt số ngày lịch sử cần lấy ở lần chạy đầu (tối đa 365).',
      s4: 'Nhấn Kiểm tra kết nối, rồi kích hoạt nguồn.',
    },
    telegram: {
      s1: 'Tạo bot với @BotFather trên Telegram và sao chép mã bot nhận được.',
      s2: 'Thêm bot làm quản trị viên của từng kênh bạn muốn đọc.',
      s3: 'Dán mã bot vào trường thông tin xác thực và liệt kê ID kênh (số âm) trong ID kênh.',
      s4: 'Nhấn Kiểm tra kết nối, rồi kích hoạt nguồn.',
    },
    alpha_vantage: {
      s1: 'Lấy khóa miễn phí tại alphavantage.co/support/#api-key (chỉ cần tên và email).',
      s2: 'Dán khóa vào trường Khóa API.',
      s3: 'Nhập các mã giao dịch (tối đa theo giới hạn), mã tiền tệ và múi giờ sàn.',
      s4: 'Gói miễn phí chỉ cho phép ít lượt gọi mỗi ngày, nên hãy giữ lịch ở mức một lần mỗi ngày.',
      s5: 'Nhấn Kiểm tra kết nối, rồi kích hoạt nguồn.',
    },
    open_meteo: {
      s1: 'Tra vĩ độ và kinh độ của nơi bạn muốn (trang bản đồ nào cũng hiển thị).',
      s2: 'Nhập vào Vĩ độ và Kinh độ, cùng múi giờ IANA như Asia/Ho_Chi_Minh.',
      s3: 'Liệt kê các chỉ số theo giờ cần lấy, cách nhau bằng dấu phẩy (ví dụ temperature_2m).',
      s4: 'Open-Meteo miễn phí cho mục đích phi thương mại và không cần khóa. Nhấn Kiểm tra kết nối, rồi kích hoạt.',
    },
    google_news: {
      s1: 'Quyết định từ khóa bạn muốn theo dõi (tối đa 200 ký tự).',
      s2: 'Nhập vào Truy vấn tìm kiếm; đừng gõ site: ở đây, hãy dùng bộ chọn trang.',
      s3: 'Có thể giới hạn theo một nhà xuất bản và chọn ngôn ngữ, khu vực.',
      s4: 'Chấp nhận điều khoản bên dưới, nhấn Kiểm tra kết nối, rồi kích hoạt nguồn.',
    },
    sec_edgar: {
      s1: 'Tìm CIK 10 chữ số của công ty tại sec.gov/edgar/searchedgar/companysearch (thêm số 0 ở đầu cho đủ).',
      s2: 'Nhập vào CIK, và email liên hệ của bạn vào Email liên hệ; SEC yêu cầu để nhận diện các yêu cầu của bạn.',
      s3: 'Chấp nhận điều khoản bên dưới, nhấn Kiểm tra kết nối, rồi kích hoạt nguồn.',
    },
    openalex: {
      s1: 'Nghĩ ra một chủ đề hoặc cụm từ mà các công trình học thuật cần khớp.',
      s2: 'Nhập vào Truy vấn tìm kiếm; OpenAlex không cần khóa cho cách dùng cơ bản.',
      s3: 'Chấp nhận điều khoản bên dưới, nhấn Kiểm tra kết nối, rồi kích hoạt nguồn.',
    },
    coingecko: {
      s1: 'Tạo tài khoản miễn phí tại coingecko.com/en/api và thêm khóa API Demo trong trang quản trị nhà phát triển.',
      s2: 'Sao chép khóa (bắt đầu bằng CG-) và dán vào trường Khóa API; khóa được mã hóa và không hiển thị lại.',
      s3: 'Chấp nhận điều khoản bên dưới. Hiển thị ghi công “Powered by CoinGecko” ở mọi nơi bạn chia sẻ dữ liệu.',
      s4: 'Nhấn Kiểm tra kết nối, rồi kích hoạt nguồn.',
    },
    fred: {
      s1: 'Tạo tài khoản miễn phí tại fredaccount.stlouisfed.org và yêu cầu khóa API trong mục My Account.',
      s2: 'Dán khóa 32 ký tự vào trường Khóa API.',
      s3: 'Tìm mã chuỗi trên fred.stlouisfed.org (ví dụ DFF cho lãi suất quỹ liên bang, CPIAUCSL cho CPI) và nhập vào Mã chuỗi.',
      s4: 'Một nguồn theo dõi một chuỗi; hãy tạo nguồn khác cho chuỗi khác. Chấp nhận điều khoản, kiểm tra, rồi kích hoạt.',
    },
    finnhub_news: {
      s1: 'Tạo tài khoản miễn phí tại finnhub.io và sao chép khóa API trong trang quản trị.',
      s2: 'Dán vào trường Khóa API. Gói miễn phí chỉ dành cho mục đích cá nhân.',
      s3: 'Chấp nhận điều khoản bên dưới, nhấn Kiểm tra kết nối, rồi kích hoạt nguồn.',
    },
    newsapi: {
      s1: 'Đăng ký tại newsapi.org/register và sao chép khóa API của bạn.',
      s2: 'Dán vào trường Khóa API. Gói Developer miễn phí chỉ dành cho phát triển và giới hạn số yêu cầu mỗi ngày.',
      s3: 'Chấp nhận điều khoản bên dưới (nguồn này cần người vận hành hệ thống duyệt), kiểm tra, rồi kích hoạt.',
    },
    openweathermap: {
      s1: 'Tạo tài khoản miễn phí tại openweathermap.org và sao chép khóa trong My API keys (khóa mới có thể mất vài giờ mới hoạt động).',
      s2: 'Dán vào trường Khóa API.',
      s3: 'Nhập Vĩ độ và Kinh độ của nơi cần theo dõi.',
      s4: 'Chấp nhận điều khoản bên dưới, nhấn Kiểm tra kết nối, rồi kích hoạt nguồn.',
    },
    openaq: {
      s1: 'Tạo tài khoản miễn phí tại explore.openaq.org và sao chép khóa API trong trang tài khoản.',
      s2: 'Dán vào trường Khóa API.',
      s3: 'Tìm một trạm quan trắc trên bản đồ OpenAQ; số ở cuối địa chỉ của nó là ID địa điểm. Hãy nhập số đó.',
      s4: 'Chấp nhận điều khoản bên dưới, nhấn Kiểm tra kết nối, rồi kích hoạt nguồn.',
    },
    nasa_firms: {
      s1: 'Yêu cầu MAP_KEY miễn phí tại firms.modaps.eosdis.nasa.gov/api/map_key (chỉ cần email).',
      s2: 'Dán khóa vào trường Khóa API.',
      s3: 'Nhập mã quốc gia ISO ba chữ cái (ví dụ VNM) vào Mã quốc gia.',
      s4: 'Quốc gia lớn có thể trả nhiều điểm nóng hơn mức một lần chạy chứa được; chỉ giữ các điểm mạnh nhất. Chấp nhận điều khoản, kiểm tra, rồi kích hoạt.',
    },
  },
} as const;
