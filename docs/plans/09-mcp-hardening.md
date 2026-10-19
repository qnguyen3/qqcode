# Plan 09 — MCP Hardening

> Gap: E7, E8, E9, E10 · Mức độ: G2×4 · Wave 3

## 1. Mục tiêu

1. Kết nối MCP **bền vững** (một session/connection per server) thay vì connect lại mỗi lần discover/call.
2. Cache tool list; xử lý thay đổi tool list; timeout cấu hình theo server.
3. Hỗ trợ **resources** (list/read) và **OAuth** cho server HTTP.
4. CLI quản lý: `qqcode mcp list/add/remove/login/logout`.
5. Giữ nguyên naming `{alias}_{tool}` và permission hiện có; thêm lớp exposure đơn giản (ẩn tool theo nhu cầu) khi danh sách quá dài.

## 2. Hiện trạng (bằng chứng)

| Vấn đề | Bằng chứng |
|---|---|
| Connect lại cho mọi thao tác | `vibe/core/tools/mcp.py:102-123`, `210-227` |
| Discovery chạy đồng bộ lúc khởi tạo ToolManager | `tools/manager.py:145-171` |
| Chỉ tools; không resources/prompts | `mcp.py` không có `list_resources`/`read_resource` |
| Auth chỉ header/api_key_env | `config.py:181-219` |
| Không CLI MCP | — |
| Không timeout/enabled/retry | `config.py:167-246` |

## 3. Thiết kế

### 3.1 Config mở rộng (`vibe/core/config.py`)

```python
class _MCPBase(BaseModel):
    name: str
    prompt: str | None = None
    enabled: bool = True
    timeout: float = 60.0
    tool_exposure: Literal["direct", "deferred", "hidden"] = "direct"

class _MCPHttpFields(BaseModel):
    ...  # url, headers, api_key_env, api_key_header, api_key_format giữ nguyên
    oauth: MCPOAuthConfig | None = None

class MCPOAuthConfig(BaseModel):
    client_id: str | None = None
    client_secret: str | None = None
    callback_port: int | None = None
    auth_server_url: str | None = None   # override discovery
    scopes: list[str] = Field(default_factory=list)
```

### 3.2 Client manager (`vibe/core/mcp/manager.py`)

Chuyển logic từ `tools/mcp.py` sang module quản lý có vòng đời:

```python
class MCPServerClient:
    """Giữ một ClientSession mở; reconnect exponential khi lỗi."""
    def __init__(self, config: MCPServer) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def list_tools(self, *, refresh: bool = False) -> list[RemoteTool]: ...
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolResult: ...
    async def list_resources(self) -> list[RemoteResource]: ...
    async def read_resource(self, uri: str) -> str: ...
    def on_tools_changed(self, callback: Callable[[], None]) -> None: ...   # server notification

class MCPManager:
    """Sở hữu các client, cung cấp tool list cho ToolManager và refresh động."""
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def tool_classes(self) -> list[type[BaseTool]]: ...
```

- Vòng đời gắn với `VibeApp`/Agent: start một lần khi app khởi động (không còn trong `ToolManager.__init__`). `ToolManager` nhận `mcp_manager` optional; nếu không có, giữ hành vi cũ một thời gian ngắn (test/shim).
- Discovery có timeout = `server.timeout`; retry 2 lần cho lỗi mạng (408/429/5xx pattern cho HTTP).
- Tool list cache + invalidation khi nhận `notifications/tools/list_changed`; cập nhật `ToolManager._available` runtime và phát event `tools_changed` cho UI.
- Khi gọi tool: dùng connection đang mở; nếu session chết → reconnect 1 lần rồi retry call; lỗi vẫn raise `ToolError` như cũ.

### 3.3 Resources

Ba tool mới (tên theo alias server):
- `mcp__<server>__list_resources` (read-only): trả danh sách uri/name/mimeType, truncation chuẩn.
- `mcp__<server>__list_resource_templates`.
- `mcp__<server>__read_resource {uri}`: nội dung text; binary trả metadata + temp file path.

Đây là tool `direct` expose như MCP tools thường, permission mặc định `ask` (đọc dữ liệu có thể nhạy cảm).

### 3.4 OAuth

- Dùng phần auth có sẵn của SDK `mcp` (package `mcp` đã có trong dependencies) qua `oauth_client_metadata`/token storage; nếu SDK không đủ, tự implement PKCE + callback server cục bộ (loopback) theo OAuth 2.1 + DCR.
- Token lưu `~/.qqcode/mcp-auth.json` (chmod 600), key theo server name + URL.
- Lệnh `qqcode mcp login <server>`: mở browser (hoặc in URL), chờ callback, lưu token; `logout` xoá token.
- Khi request 401/403: nếu có `oauth` config → thử refresh token; nếu thất bại → thông báo rõ "run qqcode mcp login <server>".

### 3.5 CLI

```
qqcode mcp list [--json]                 # server, transport, status, số tool
qqcode mcp add <name> --url URL [--oauth] [--header K=V] [--env K=V]
qqcode mcp add <name> -- cmd args...     # stdio
qqcode mcp remove <name> [--local]
qqcode mcp login <name> | logout <name>
```
- Ghi vào `~/.qqcode/config.toml` (global) hoặc `.qqcode/config.toml` (local) qua `VibeConfig.save_updates` (đã có).
- `add` validate kết nối + list tools trước khi ghi (fail sớm).

### 3.6 Exposure đơn giản

- `tool_exposure="hidden"` → không expose schema cho model, nhưng tool vẫn được đăng ký và gọi được theo tên (dùng cho server có tool ít dùng).
- `tool_exposure="deferred"` → ẩn khỏi tool list mặc định; thêm tool `mcp_search {query}` (BM25 đơn giản trên name+description) trả về top tool match và đánh dấu active cho phần còn lại của lượt hiện tại; hết lượt thì ẩn lại (one-shot).
- Mặc định `direct` để tương thích hoàn toàn.

## 4. Task breakdown

| # | Task | File | Ước lượng | Phụ thuộc |
|---|---|---|---|---|
| 9.1 | Config fields mới + migration test | `vibe/core/config.py` | S | — |
| 9.2 | `MCPServerClient` (connect/reconnect/list/call) | `vibe/core/mcp/client.py` (mới) | L | 9.1 |
| 9.3 | `MCPManager` + vòng đời app; ToolManager nhận manager | `vibe/core/mcp/manager.py`, `tools/manager.py` | L | 9.2 |
| 9.4 | Tool list cache + list_changed + event | `mcp/manager.py`, events | M | 9.3 |
| 9.5 | Resources tools | `mcp/resources.py` (mới) | M | 9.3 |
| 9.6 | OAuth flow + token store | `mcp/oauth.py` (mới) | L | 9.2 |
| 9.7 | CLI `mcp list/add/remove/login/logout` | `cli/mcp_cli.py` (mới), `entrypoint.py` | M | 9.3, 9.6 |
| 9.8 | Deferred exposure + `mcp_search` | `mcp/exposure.py` (mới), `tools/format.py` | L | 9.3 |
| 9.9 | Docs `docs/mcp.md` + cập nhật README | docs | M | 9.7 |

## 5. Test plan

| Test | Nội dung |
|---|---|
| `tests/mcp/test_client.py` | connect/list/call qua stdio fake server; reconnect sau lỗi |
| `tests/mcp/test_manager.py` | cache tool list; list_changed cập nhật; stop() đóng sạch |
| `tests/mcp/test_resources.py` | list/read resource; binary → temp file |
| `tests/mcp/test_oauth.py` | PKCE exchange với server giả; refresh; logout xoá token |
| `tests/mcp/test_cli.py` | add ghi config đúng; remove; login fail sớm khi không kết nối được |
| `tests/tools/test_manager_get_tool_config.py` | giữ nguyên hành vi khi không có MCP |
| `tests/mcp/test_exposure.py` | hidden ẩn; deferred chỉ lộ sau search |

## 6. DoD

- [ ] Hai tool call liên tiếp cùng server dùng một connection (test đếm số lần spawn/connect).
- [ ] Server chết giữa chừng → reconnect một lần rồi báo lỗi tool result (loop tiếp tục).
- [ ] `qqcode mcp add/list/remove` hoạt động end-to-end với stdio fake server.
- [ ] Resources đọc được; OAuth flow hoàn tất được với mock server.
- [ ] Config MCP cũ (không có field mới) vẫn load; tests cũ xanh.

## 7. Rủi ro & rollback

| Rủi ro | Giảm thiểu |
|---|---|
| Giữ connection lâu gây treo khi server im lặng | timeout per-call + health check nhẹ; stop() khi app thoát |
| OAuth callback port bận | cho phép cấu hình callback_port; fallback copy-paste code |
| Tool list_changed phá thứ tự prompt | cập nhật tool list + rebuild prompt section (plan 04); phát event UI |
| Deferred search kém chính xác | BM25 đơn giản + ngưỡng; luôn cho user gọi trực tiếp nếu biết tên |

Rollback: `enabled=false` per server; `tool_exposure="direct"` mặc định; `MCPManager` optional.

## 8. Commit plan

```
feat(mcp): keep persistent connections to servers
feat(mcp): cache tool lists and refresh on change
feat(mcp): expose server resources as tools
feat(mcp): sign in to protected http servers
feat(cli): manage mcp servers from the command line
feat(mcp): hide rarely used tools behind a search tool
```
