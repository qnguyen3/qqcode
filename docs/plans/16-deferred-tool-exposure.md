# Plan 16 — Deferred tool exposure & tool search

> Gap: H15 · Mức độ: G2×1 · Wave 3

## 1. Mục tiêu

1. Khi có nhiều tool (đặc biệt MCP proxy), mặc định **không** gửi hết vào request — chỉ gửi tool "direct" + những tool đã được expose.
2. Tool exposure per tool: `direct | model_only | deferred | hidden` (mặc định `direct` cho builtin).
3. Builtin `tool_search` tìm theo BM25 trên name/description của các tool `deferred`, expose match cho **request kế tiếp**.
4. Tiết kiệm token schema (đo được) mà không mất khả năng gọi tool; model luôn thấy `tool_search` để tự khám phá.
5. Giữ tương thích: cấu hình/hành vi cũ (tất cả tool hiện diện) là mặc định khi tắt tính năng.

## 2. Hiện trạng (bằng chứng)

| Vấn đề | Bằng chứng |
|---|---|
| Tool active = tất cả tool discover được, lọc enabled/disabled | `vibe/core/format.py:77-102` |
| Gửi toàn bộ schema mỗi request | `vibe/core/format.py:147-161` (`get_available_tools`) |
| MCP tool sinh proxy hàng loạt, tên `{alias}_{tool}` | `vibe/core/tools/mcp.py:126-207` |
| Không có khái niệm exposure/deferred | `vibe/core/tools/base.py` (chỉ `execution_mode`) |
| Không có tool search | không có |

## 3. Thiết kế

### 3.1 Exposure trên tool (`vibe/core/tools/base.py`)

```python
ToolExposure = Literal["direct", "model_only", "deferred", "hidden"]

class BaseTool(Generic[...]):
    ...
    exposure: ClassVar[ToolExposure] = "direct"
```

- `direct`: luôn gửi schema model (mặc định, tương thích cũ).
- `model_only`: model thấy schema nhưng không hiện trong UI palette? (giữ đơn giản: gửi cho model, ẩn khỏi một số UI listing).
- `deferred`: **không** gửi mặc định; chỉ được thêm vào request sau khi `tool_search` match.
- `hidden`: không bao giờ gửi model (chỉ nội bộ/UI).

Cấu hình override theo tên/glob trong `config.toml`:

```toml
[tools.exposure]
"mcp.serena_*" = "deferred"
"mcp.fs_*" = "deferred"
```

### 3.2 `ToolRegistry` exposure (`vibe/core/tools/manager.py`)

```python
class ToolRegistry:
    def direct_tools(self) -> list[AvailableTool]: ...
    def deferred_pool(self) -> list[ToolDescriptor]: ...
    def expose(self, names: list[str]) -> None:
        """Đánh dấu tool deferred được expose cho request kế tiếp; tự hết hiệu lực
        sau khi request hoàn tất (one-shot) trừ khi config `sticky=True`."""
```

- Theo dõi `_exposed: set[str]` với cờ one-shot; `get_available_tools` = direct ∪ exposed.
- Đếm/đo: log `schema_tokens(before/after)` để chứng minh tiết kiệm.

### 3.3 Builtin `tool_search` (`vibe/core/tools/builtins/tool_search.py`)

```python
class ToolSearchArgs(BaseModel):
    query: str
    limit: int = 8

class ToolSearchResult(BaseToolResult):
    matches: list[ToolMatch]     # name, description, score
    exposed: list[str]
```

- BM25 trên token hoá `name + description` của `deferred_pool` (thuật toán thuần Python, không dependency mới; corpus nhỏ nên build index mỗi lần gọi là đủ).
- Sau khi trả kết quả, gọi `registry.expose(matches)` → schema của các tool này có mặt trong **request kế tiếp**.
- Prompt `tool_search.md` hướng dẫn model: "khi cần tool không thấy, gọi `tool_search`".

### 3.4 Tích hợp

- `format.get_available_tools` đọc từ `ToolRegistry` (direct ∪ exposed) thay vì toàn bộ active.
- MCP discovery: đánh dấu mặc định `deferred` khi số MCP tool vượt ngưỡng (`tools.defer_when_more_than = 30`), cấu hình được.
- Reset `exposed` khi bắt đầu turn mới (one-shot) trừ `sticky`.
- `tool_search` là `direct` và luôn có mặt khi có ít nhất một tool `deferred`.

### 3.5 UI

- `/tools` (nếu có) hiển thị exposure; tool call của tool deferred vẫn render bình thường.
- `/status` thêm số tool direct/deferred (tùy chọn).

## 4. Task breakdown

| # | Task | File | Ước lượng | Phụ thuộc |
|---|---|---|---|---|
| 16.1 | `exposure` ClassVar + config override | `tools/base.py`, `config.py` | S | — |
| 16.2 | `ToolRegistry` direct/deferred/expose + one-shot | `tools/manager.py`, `format.py` | M | 16.1 |
| 16.3 | BM25 index + `tool_search` builtin | `tools/builtins/tool_search.py` (mới), `tools/search.py` | M | 16.2 |
| 16.4 | Prompt `tool_search.md` + hướng dẫn model | `tools/builtins/prompts/` | S | 16.3 |
| 16.5 | MCP auto-defer theo ngưỡng | `tools/mcp.py`, `config.py` | S | 16.2 |
| 16.6 | Đo schema tokens + log | `format.py`, `agent.py` | S | 16.2 |
| 16.7 | UI/status exposure + tests | `textual_ui/`, `cli/` | S | 16.2 |
| 16.8 | Docs `docs/tools.md` mục exposure/deferred | docs | S | 16.3 |

## 5. Test plan

| Test | Nội dung |
|---|---|
| `tests/tools/test_exposure_config.py` | glob override; mặc định direct; config cũ không có field vẫn chạy |
| `tests/tools/test_tool_search.py` | BM25 xếp hạng hợp lý; `limit`; query rỗng |
| `tests/test_agent_deferred.py` | FakeBackend: gọi `tool_search` → request kế tiếp chứa schema tool match; turn sau lại ẩn (one-shot) |
| `tests/tools/test_mcp_autodefer.py` | > ngưỡng → deferred; dưới ngưỡng → direct |
| `tests/test_schema_tokens.py` | số token schema giảm khi defer N tool |

## 6. DoD

- [x] Bật deferred: request mặc định không chứa schema tool deferred; `tool_search` có mặt.
- [x] Sau `tool_search`, model gọi được tool vừa expose trong request kế tiếp.
- [x] Đo được giảm token schema (test/log) khi có nhiều MCP tool.
- [x] Tắt tính năng (`tools.defer_when_more_than` lớn / exposure=direct) → hành vi cũ nguyên vẹn.
- [x] `ruff`, `pyright`, `pytest` xanh.

Ghi chú triển khai: exposure của server MCP đặt `deferred` vẫn dùng `mcp_search`
(plan 09) như trước; auto-defer theo ngưỡng và override `[tools.exposure]` dùng
chung `tool_search`. Một lần hợp nhất hai cơ chế này nằm ngoài phạm vi plan.

## 7. Rủi ro & rollback

| Rủi ro | Giảm thiểu |
|---|---|
| Model "quên" tool vì bị ẩn, gọi sai | luôn có `tool_search` + prompt hướng dẫn; một số tool quan trọng để `direct` |
| One-shot exposure gây thêm round-trip | `sticky` per-tool cho tool hay dùng; đo tỷ lệ hit |
| BM25 kém với tên tool viết tắt | index cả description + token phân tách `_`; test query thực tế |
| Deferred làm vỡ permission/allowlist | permission vẫn theo tên tool, độc lập exposure; test |

Rollback: `tools.deferred=false` hoặc để mọi tool `direct` → gửi toàn bộ schema như cũ.

## 8. Commit plan

```
feat(tools): add per-tool exposure modes
feat(tools): expose deferred tools on demand
feat(tools): search deferred tools with a tool_search builtin
feat(mcp): defer large tool sets by default
```
