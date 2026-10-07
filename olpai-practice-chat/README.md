# OlpAI Practice Chat

Web chat cá nhân gọi **Cloudflare Workers AI** với model cố định `@cf/deepseek-ai/deepseek-r1-distill-qwen-32b`. Máy chỉ chạy giao diện, backend và tokenizer; không chạy model local. OpenRouter vẫn là lựa chọn cấu hình thủ công; không tự chuyển nhà cung cấp.

## Chạy trên Windows

Trong PowerShell:

```powershell
cd D:\OlpAI\olpai-practice-chat
.\run.ps1
```

Mở http://127.0.0.1:8765. Virtualenv và tokenizer đã được chuẩn bị trong workspace này.

Chạy demo để xem giao diện/kiểm tra thao tác mà không có key:

```powershell
.\run.ps1 -Demo -Port 8766
```

Demo trả lời giả, có nhãn rõ ràng; không gọi DeepSeek và lưu lịch sử trong database riêng.

## Cấu hình Cloudflare

Trong [Cloudflare dashboard](https://dash.cloudflare.com/), vào **Workers AI → Use REST API → Create a Workers AI API Token**, tạo token theo mẫu và copy **Account ID**. Nếu tạo token thủ công, cấp **Workers AI Read và Edit** cho đúng tài khoản. Xem [hướng dẫn chính thức](https://developers.cloudflare.com/workers-ai/get-started/rest-api/).

File `.env` hiện có đã được bổ sung các mục Cloudflare, giữ nguyên các cấu hình/key cũ. Mở file:

```powershell
notepad .env
```

Điền:

```dotenv
OLP_BACKEND=cloudflare
CLOUDFLARE_ACCOUNT_ID=account_id_32_ky_tu_cua_ban
CLOUDFLARE_API_TOKEN=token_cua_ban
```

Lưu file, dừng server cũ bằng `Ctrl+C`, chạy lại `.\run.ps1`, mở http://127.0.0.1:8765 rồi bấm **Kiểm tra kết nối**. Nút này gọi danh mục model có xác thực, chưa sinh câu trả lời và chưa chứng minh quyền inference/quota còn đủ. Gửi tin nhắn đầu tiên mới gọi model. Token sai/thiếu quyền/quota hoặc thiếu usage sẽ được báo lỗi; không tự retry hay đổi model.

Endpoint: `https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/v1/chat/completions`, theo [API tương thích](https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/). Payload chỉ gồm model cố định, messages, max_tokens, temperature, stream và yêu cầu usage. Không có tools/plugins/search/AI Gateway.

Không gửi token vào chat hoặc đưa lên Git. Có thể dùng biến môi trường thay cho `.env`. `.env` không được commit, token không gửi xuống trình duyệt và không có trong export. Khi cài mới và chưa có `.env`, copy `.env.example` sang `.env` trước khi điền.

Cloudflare có context dịch vụ 80K; **app vẫn giới hạn 2.048 token** theo yêu cầu luyện tập. Cùng tên model không bảo đảm giống tuyệt đối BTC vì cấu hình phục vụ/tokenizer/sampling của BTC chưa được công bố đầy đủ. Xem [model chính thức](https://developers.cloudflare.com/workers-ai/models/deepseek-r1-distill-qwen-32b/).

## OpenRouter tùy chọn

Chỉ khi muốn dùng OpenRouter, đặt `OLP_BACKEND=openrouter` và điền `OPENROUTER_API_KEY` rồi restart. Không dùng token Cloudflare cho OpenRouter.

Ngày 04/10/2026, API catalog của OpenRouter trả `endpoints: []` cho cả model này và bản `:free`. Nút **Kiểm tra kết nối** kiểm tra lại endpoint công khai; chưa phải kiểm thử key/completion. Nếu vẫn trống, app báo lỗi và không dùng model thay thế. Có key không làm model chưa có endpoint trở nên khả dụng.

Nếu sau này có endpoint, có thể điền slug vào `OPENROUTER_PROVIDER` để cố định provider. Nếu để trống, OpenRouter chọn một provider của cùng model cho mỗi request; fallback provider bị tắt. Khả năng lặp lại kết quả chưa được đảm bảo.

## Quy định mô phỏng

- Model cố định đúng 32B. Không web search, `:online`, tools, plugins, RAG, đọc file hoặc chạy code.
- URL chỉ là văn bản; không tải trang, link preview hay ảnh bên ngoài. HTML trong câu trả lời không được thực thi.
- Context mặc định 2.048 token: token lịch sử/câu hỏi theo chat template chính thức + ngân sách sinh + 64 token dự phòng. Output target mặc định 768; khi context gần đầy, output giảm. Câu trả lời có thể hết ngân sách trong lúc reasoning hoặc code chưa hoàn chỉnh.
- Context được đếm bằng tokenizer DeepSeek cố định revision, không dùng ký tự/4. Chưa thể chứng minh hard cap upstream trước khi kiểm thử provider thật. App so usage thực tế với 2K; nếu thiếu usage hoặc sai lệch quá dự phòng, khóa phiên và báo lỗi.
- Hai chính sách cho phiên mới: **yêu cầu phiên mới** khi đầy hoặc **bỏ cặp hỏi/đáp cũ**. Tin bị bỏ vẫn lưu để xem, được đánh dấu ngoài context. Không tự tóm tắt. Chính sách của một bài thi thử được cố định khi bắt đầu.
- Thi thử kéo dài 6 giờ, AI dùng 5 giờ đầu. Backend khóa theo thời gian lưu trong SQLite; tạo phiên mới/reload/khởi động lại app không đặt lại giờ. Request đang sinh bị dừng ở hạn AI. Hủy request không đảm bảo provider ngừng tính phí ngay.
- Phiên cũ không được dùng để vượt hạn trong bài thi đang hoạt động. Kết thúc bài chuyển các phiên thi thành chỉ đọc; bắt đầu bài khác tạo lượt thi mới có lịch sử riêng.
- Không tự retry completion có phí. Lỗi/hủy stream giữ bản nháp và không đưa bản nháp vào context các lượt sau.
- Chỉ chạy một worker và bind `127.0.0.1`. Đây là app luyện tập cá nhân, không phải hệ thống chống gian lận; không kiểm soát các app/tab khác hoặc việc sửa đồng hồ hệ điều hành/database local.

Nguồn: `D:/OlpAI/Đề thi VOAI2025.pdf` trang 2-3. PDF nêu model, truy cập trình duyệt/offline, mốc 5/6 giờ; không nêu context 2K, cách xử lý phiên đầy hoặc sampling. Những mục đó là cấu hình luyện tập. Gọi API Cloudflare/OpenRouter cần Internet, nên không giống hạ tầng offline vật lý của BTC.

Ứng dụng không cung cấp notebook/GPU huấn luyện, private test, hệ thống nộp/chấm bài hoặc mở khóa dataset. Bạn tự chạy giải pháp trong môi trường làm bài.

## Cài lại trên máy khác

Python 3.12 hoặc tương thích:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe prepare_tokenizer.py
.\run.ps1
```

`prepare_tokenizer.py` tải hai file tokenizer (~7 MB), không tải trọng số. Chuẩn bị trước buổi thi; lúc chat không kết nối tới Hugging Face. Dependencies đã kiểm thử được lưu trong `requirements.lock.txt` nếu cần tái tạo cùng phiên bản.

## Kiểm thử

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

37 kiểm thử đã pass: tokenizer tiếng Việt/code/template, biên 2K, hai chính sách context, khóa 5 giờ qua restart/phiên mới, nhiều tab, hủy stream, thiếu endpoint/key, usage vượt giới hạn, chặn search/tool payload, Cloudflare catalog/stream/reasoning/lỗi xác thực và bảo vệ key. Completion dùng mock, không tính phí. Đã gọi model Cloudflare thật và nhận usage. Bản sửa đồng bộ lịch sử assistant gửi API với template local: bỏ phần reasoning trước </think> khỏi request lượt sau, vẫn giữ nguyên trong lịch sử hiển thị/export. Các phiên khóa nhầm do lỗi này được mở lại khi restart nếu usage thực vẫn trong 2K; phiên vượt 2K hoặc thiếu usage vẫn bị khóa. context_verified vẫn false vì các kiểm thử thực tế chưa chứng minh mọi loại prompt.

Lịch sử nằm ở `data/practice.sqlite3`, demo ở `data/demo.sqlite3`; hai database không được commit. **Xuất lịch sử** lưu JSON gồm tin nhắn, usage và trạng thái; không có API key. Mỗi lượt mới lưu backend/model; lượt cũ trước bản Cloudflare để hai trường này trống vì không có thông tin xác thực nguồn. Trường model/backend ở đầu export mô tả cấu hình hiện tại.
