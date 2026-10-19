# Plan 13 — Multimodal image input

> Gap: P12, H17 · Mức độ: G2×2 · Wave 3

## 1. Mục tiêu

1. Nội dung message hỗ trợ phần **ảnh** bên cạnh text (`read_file` trả ảnh khi file là ảnh).
2. Adapter provider mã hoá ảnh đúng chuẩn: OpenAI-compatible (`image_url` data URI) và Anthropic (`image` base64 block).
3. Tôn trọng capability: model `capabilities.vision=false` → **downgrade** ảnh thành text placeholder (đường dẫn + kích thước), không lỗi.
4. Kiểm soát kích thước: giới hạn byte/độ phân giải, resize khi có Pillow (tùy chọn), và đếm token ước lượng theo hằng số ảnh.
5. Giữ tương thích: config/model cũ, session cũ (chỉ text) và `Content` đang là `str` vẫn chạy.

## 2. Hiện trạng (bằng chứng)

| Vấn đề | Bằng chứng |
|---|---|
| `Content` bị ép về `str`, list content bị join thành text | `vibe/core/types.py:213-226` (`_content_before`, `Content = Annotated[str, ...]`) |
| `read_file` chỉ đọc text UTF-8, trả string | `vibe/core/tools/builtins/read_file.py:28-57`, `115-147` |
| Backend chỉ gửi text content | `generic.py` OpenAI/Anthropic adapters; `anthropic_sdk.py:74-83` |
| Chưa có cờ `vision` dùng trong quyết định request | plan 05 `ModelCapabilities.vision` (`config.py`), chưa wired cho read |
| Estimator đã chừa chỗ ảnh | plan 01 `IMAGE_CHARS = 4800` |

## 3. Thiết kế

### 3.1 Content parts (`vibe/core/types.py`)

> **Cập nhật khi triển khai:** `LLMMessage.content` giữ nguyên `str` trên toàn
> codebase (compactor, session, UI, ACP đều đọc text). Ảnh được mô hình hoá
> thành field riêng `images: list[ImagePart]` trên `LLMMessage`; adapter gộp
> text + ảnh thành content blocks đúng chuẩn provider. Cách này giữ tương thích
> 100% với message text cũ và tránh sửa hàng chục call site.

```python
class ImagePart(BaseModel):
    type: Literal["image"] = "image"
    media_type: str              # "image/png", "image/jpeg", ...
    data: str = ""               # base64 (không prefix); rỗng khi chỉ là ref
    source_path: str | None = None
    size_bytes: int | None = None
    width: int | None = None
    height: int | None = None

class LLMMessage(BaseModel):
    ...
    images: list[ImagePart] = []
```

- Helper `message_for_storage(message)` bỏ base64 khi ghi session; `describe_image`,
  `load_image_part`, `resolve_messages(vision=...)` nằm trong `vibe/core/images.py`.

### 3.2 `read_file` nhánh ảnh (`vibe/core/tools/builtins/read_file.py`)

```python
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_DIMENSION = 1568        # giới hạn cạnh dài (Anthropic/OpenAI khuyến nghị)

class ReadFileResult(BaseToolResult):
    path: str
    content: str | None
    image: ImageRef | None = None       # ref tới file, không phải base64
    truncated: bool = False
```

- File là ảnh ⇒ trả `image` ref + text mô tả (`path`, kích thước, định dạng).
- Vượt `MAX_IMAGE_BYTES`/`MAX_IMAGE_DIMENSION`: nếu có `pillow` → resize/re-encode; nếu không → từ chối mềm kèm thông báo (không crash).
- `format_for_model` (plan 03): nếu model `vision=true` → trả `ImagePart`; nếu `vision=false` → placeholder text `"[image omitted: model has no vision support] path=..."`.
- Quyết định vision đọc từ `ModelCapabilities.vision` của model active (plan 05), truyền vào qua `ToolState`/context.

### 3.3 Adapter mã hoá ảnh

| Adapter | Định dạng gửi |
|---|---|
| OpenAI-compatible (`openai_chat`) | `{"type":"image_url","image_url":{"url":"data:<media_type>;base64,<data>"}}` |
| Anthropic messages (`anthropic_messages`, `anthropic_sdk`) | `{"type":"image","source":{"type":"base64","media_type":..,"data":..}}` |
| Provider không hỗ trợ | adapter kiểm `capabilities.vision`; nếu false, thay `ImagePart` bằng `TextPart` placeholder trước khi gửi |

- Chuyển đổi nằm trong adapter (plan 06), không rải ở `agent.py`; `generic.py` đã chọn adapter theo `api_style`.
- Base64 nạp **lười**: chỉ đọc file tại thời điểm build request, cache theo `(path, mtime)` trong một LRU nhỏ.

### 3.4 Session & token

- Session entry lưu `ImageRef{path, media_type, size, sha256}`, không lưu base64. Replay đọc lại file nếu còn; file biến mất ⇒ placeholder text.
- Estimator (plan 01): `estimate_message_tokens` cộng `IMAGE_CHARS` cho mỗi `ImagePart` (đã có hằng số).
- Cảnh báo nếu context nhiều ảnh: cộng dồn ước lượng, dùng chung ngưỡng compact.

### 3.5 UI

- Tool renderer hiển thị tên file + kích thước + thumbnail nếu terminal/TUI hỗ trợ; nếu không, chỉ đường dẫn.
- `/status` không đổi.

## 4. Task breakdown

| # | Task | File | Ước lượng | Phụ thuộc |
|---|---|---|---|---|
| 13.1 | `TextPart`/`ImagePart`/`Content` + helper, giữ tương thích | `vibe/core/types.py` | M | — |
| 13.2 | `read_file` nhánh ảnh + ref + giới hạn kích thước | `read_file.py` | M | 13.1 |
| 13.3 | `format_for_model` cho result ảnh + vision gate | `tools/base.py`, `read_file.py` | M | 13.2, plan 05 |
| 13.4 | Adapter encode ảnh OpenAI + Anthropic | `llm/backend/adapters/openai_chat.py`, `anthropic_messages.py`, `anthropic_sdk.py` | M | 13.1 |
| 13.5 | Resize/encode với Pillow (tùy chọn, có fallback) | `vibe/core/images.py` (mới) | M | 13.2 |
| 13.6 | Session lưu `ImageRef`, replay đọc lại | `sessions/entries.py`, `sessions/recorder.py` | S | 13.1 |
| 13.7 | Estimator + UI renderer ảnh | `context/tokens.py`, `textual_ui/renderers/` | S | 13.1 |
| 13.8 | Docs + ví dụ (`docs/images.md`) | docs | S | 13.4 |

## 5. Test plan

| Test | Nội dung |
|---|---|
| `tests/core/test_content_parts.py` | string/list text cũ giữ nguyên; list có image roundtrip; `content_text` |
| `tests/tools/test_read_image.py` | đọc png/jpg → ImageRef; file > giới hạn bị chặn/resize; không có Pillow vẫn không crash |
| `tests/backend/test_image_encoding.py` | respx: OpenAI sinh `image_url` data URI đúng; Anthropic sinh `image` base64 block |
| `tests/backend/test_vision_downgrade.py` | `vision=false` → placeholder text, không có ImagePart trong payload |
| `tests/sessions/test_image_ref.py` | log lưu ref không base64; replay khi file mất → placeholder |
| `tests/context/test_image_tokens.py` | mỗi ảnh cộng `IMAGE_CHARS` |

## 6. DoD

- [x] `read_file` ảnh hoạt động end-to-end với ít nhất một provider vision (mock/respx) và downgrade đúng khi không vision.
- [x] File `.jsonl` không nhúng base64; kích thước log không tăng vọt khi đọc nhiều ảnh.
- [x] Config/model cũ (chỉ text) chạy không đổi.
- [x] Có giới hạn byte/độ phân giải và thông báo rõ khi vượt.
- [x] `ruff`, `pyright`, `pytest` xanh; Pillow là optional import.

## 7. Rủi ro & rollback

| Rủi ro | Giảm thiểu |
|---|---|
| Ảnh lớn làm tràn context/cost | giới hạn byte/độ phân giải + resize + estimator tính `IMAGE_CHARS` |
| Provider không hỗ trợ format ảnh của adapter | gate theo `capabilities.vision` + test respx từng adapter |
| Base64 phình log | chỉ lưu ref; base64 sinh lúc build request |
| Pillow là dependency nặng | import optional; không có vẫn chạy (chỉ không resize) |

Rollback: `tools.read.images=false` (hoặc capability `vision=false`) → hành vi text-only như cũ; `Content` vẫn nhận string nên không phá gì.

## 8. Commit plan

```
feat(core): model text and image message content
feat(tools): read image files and return image content
feat(llm): encode image content for openai and anthropic
feat(llm): downgrade images when the model has no vision
feat(sessions): store image references instead of inline data
```
