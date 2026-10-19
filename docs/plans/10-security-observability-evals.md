# Plan 10 — Security, Observability & Evals

> Gap: S9, S10, S11, S12, S13, S14 · Mức độ: G2×5, G1×1 · Wave 3

## 1. Mục tiêu

1. **Project trust**: chưa trust thì không tự động nạp config/tools/hooks/MCP của project.
2. Hardening permission: allowlist theo path cho write/edit, audit log các quyết định.
3. Observability: usage/cost/context hiển thị đầy đủ; log chẩn đoán có cấu trúc; local metrics opt-in.
4. Eval harness chạy scenario cố định bằng `FakeBackend` để chống regression prompt/tool/context.
5. Tài liệu sandbox/containerization cho người dùng cần ranh giới mạnh.

## 2. Hiện trạng (bằng chứng)

| Vấn đề | Bằng chứng |
|---|---|
| Tự nạp `.qqcode/config.toml` project không điều kiện | `vibe/core/config.py:38-46` |
| Tool discovery project không gate | `tools/manager.py:67-72` |
| Không audit log | chỉ `vibe.log` chung (`utils.py:144-154`) |
| Cost/context chỉ ở `/status` | `textual_ui/app.py:810-831`; `types.py:74-89` |
| Không eval harness | `tests/` unit + snapshot; không scenario metrics |
| Không tài liệu sandbox | README không nhắc container |

## 3. Thiết kế

### 3.1 Project trust (`vibe/core/trust.py`)

- Khoá trust: `~/.qqcode/trust.toml`
  ```toml
  [projects]
  "/home/user/proj" = "trusted"          # trusted | untrusted
  ```
- `--trust` / `--no-trust` CLI; `default_project_trust: bool = False` trong config global.
- Khi chạy trong thư mục có `.qqcode/config.toml` (hoặc `.qqcode/tools`, `.qqcode/hooks`, `.qqcode/mcp`), nếu chưa trust:
  1. tương tác: hỏi "Trust project? [Y]es/[n]o/[a]lways" (Textual modal; ACP trả lỗi rõ nếu client không hỗ trợ).
  2. `--no-trust`/headless: bỏ qua toàn bộ resource project, dùng global config; in cảnh báo.
- Áp dụng cho: config project, tool_paths project, hooks project, MCP project, prompt/context files project (context files vẫn nạp vì không thực thi code — ghi rõ trong docs).
- Lưu quyết định vào `qr-code` trust store khi chọn "always".

### 3.2 Permission hardening

- `ReadFileToolConfig`/`EditConfig`/`WriteFileConfig` thêm `path_allowlist`/`path_denylist` (glob tuyệt đối sau resolve) — đã có `denylist` cho read; bổ sung cho write/edit và áp dụng trong `check_allowlist_denylist`.
- Audit log `~/.qqcode/logs/audit.jsonl`, mỗi dòng:
  ```json
  {"ts":"...","session_id":"...","tool":"bash","args_hash":"...","decision":"approved|denied|denylist","mode":"interactive","user":"..."}
  ```
  Không log full args cho tool nhạy cảm (bash/read) — chỉ log command/path rút gọn + hash; config `audit.log_args: bool = False`.
- UI approval hiển thị đường dẫn resolve và cảnh báo `..`/absolute ngoài workdir.

### 3.3 Observability

- `/status` và footer hiển thị: model, context `% (tokens/window)`, chi phí (kèm cache read/write nếu có), số turn, tool stats.
- `UsageEntry` (plan 02) đã lưu per-turn; thêm lệnh `qqcode sessions stats <id>` in tổng hợp.
- Structured debug log: `~/.qqcode/logs/debug.jsonl` (opt-in `debug.enabled`), ghi request meta (model, message count, estimated tokens, duration) — **không** ghi nội dung message mặc định.
- Local metrics opt-in (`telemetry.mode = "off" | "local"`; default `off`): đếm counter (turns, compactions, overflows, tool failures) ra file `~/.qqcode/logs/metrics.json`; không gửi mạng. Remote telemetry **ngoài phạm vi**.

### 3.4 Eval harness (`vibe/core/evals/harness.py`)

```python
@dataclass
class EvalScenario:
    name: str
    user_prompt: str
    fixture_dir: Path            # thư mục tạm copy làm workdir
    scripted_turns: list[ScriptedTurn]   # tool call / text cho FakeBackend
    expected: EvalExpectation

@dataclass
class EvalExpectation:
    max_turns: int | None = None
    required_tools: list[str] = field(default_factory=list)
    forbidden_tools: list[str] = field(default_factory=list)
    final_contains: list[str] = field(default_factory=list)
    max_context_tokens: int | None = None
    must_compact: bool = False
    must_recover_overflow: bool = False

async def run_scenario(scenario: EvalScenario) -> EvalResult: ...
```

- Dùng `tests/stubs/fake_backend.py` (đã có) + fixture dir; không gọi mạng.
- Scenarios khởi điểm (`evals/scenarios/`):
  | Scenario | Kiểm tra |
  |---|---|
  | `compact_keeps_recent` | history dài, compact xảy ra, tool result gần nhất còn nguyên |
  | `overflow_recovery` | backend lỗi context length → agent compact + retry thành công |
  | `parallel_tools` | 2 tool call độc lập chạy song song |
  | `plan_mode_readonly` | write_file bị chặn trong plan mode |
  | `truncation_notice` | bash output lớn → notice + temp file |
  | `hook_gate` | hook chặn bash nguy hiểm (sau plan 08) |
- Chạy: `uv run pytest tests/evals -q` hoặc `uv run qqcode-evals` (script mới); CI job riêng optional.
- Kết quả in bảng pass/fail + metrics (turns, tokens, duration).

### 3.5 Sandbox docs

- `docs/security.md`: permission model, trust, audit, best practices.
- `docs/sandboxing.md`: 3 pattern (chạy qqcode trong container; tách workdir; chạy user hạn chế) kèm Dockerfile mẫu `examples/docker/Dockerfile.qqcode`.
- Không built-in sandbox process ở plan này.

## 4. Task breakdown

| # | Task | File | Ước lượng | Phụ thuộc |
|---|---|---|---|---|
| 10.1 | Trust store + decision flow + CLI flags | `vibe/core/trust.py` (mới), `entrypoint.py` | L | plan 02 |
| 10.2 | Gate config/tools/hooks/MCP theo trust | `config.py`, `tools/manager.py`, hooks loader | M | 10.1 |
| 10.3 | UI modal trust | `textual_ui/` | M | 10.1 |
| 10.4 | Path allowlist cho write/edit | tool configs | M | plan 03 |
| 10.5 | Audit logger | `vibe/core/audit.py` (mới), `agent.py` | M | plan 02 |
| 10.6 | `/status` + footer usage mới | `app.py`, widgets | M | plan 05 |
| 10.7 | `sessions stats` CLI | `entrypoint.py` | S | plan 02 |
| 10.8 | Debug log + local metrics opt-in | `vibe/core/metrics.py` (mới) | M | — |
| 10.9 | Eval harness + 6 scenario | `vibe/core/evals/`, `evals/` | L | plan 01/03 |
| 10.10 | Docs security + sandboxing + Dockerfile mẫu | docs, examples | M | — |

## 5. Test plan

| Test | Nội dung |
|---|---|
| `tests/core/test_trust.py` | project chưa trust → config project bị bỏ; `--trust` áp dụng; trust store persist |
| `tests/tools/test_path_allowlist.py` | write/edit ngoài allowlist bị deny |
| `tests/core/test_audit.py` | dòng audit ghi đúng cho approved/denied; không lộ args khi log_args=false |
| `tests/evals/test_harness.py` | harness chạy 1 scenario, expectation fail/pass đúng |
| `tests/test_agent_auto_compact.py` | scenario compact tương ứng eval |
| `tests/core/test_metrics.py` | counters tăng; file ghi atomic |

## 6. DoD

- [ ] Chạy trong project lạ có `.qqcode/config.toml` không tự áp config khi chưa trust (test).
- [ ] Trust một lần, lần sau không hỏi; `--no-trust` cho CI/headless.
- [ ] Audit log ghi mọi quyết định tool; không chứa secret theo mặc định.
- [ ] Eval harness chạy 6 scenario trong CI test thường (< 10s, không mạng).
- [ ] Docs security/sandboxing có ví dụ Docker chạy được `qqcode -p "..."`.

## 7. Rủi ro & rollback

| Rủi ro | Giảm thiểu |
|---|---|
| Trust prompt phá trải nghiệm lần đầu | chỉ hỏi khi project có resource cần gate; global config không gate |
| Audit log phình to | rotate theo kích thước (5MB), giữ 3 file |
| Eval brittle theo prompt | assert trên cấu trúc (tool dùng, số turn) thay vì chuỗi dài; `final_contains` ngắn |
| Metrics ảnh hưởng hiệu năng | in-memory counters, flush mỗi 5s hoặc khi thoát |

Rollback: `trust.enabled=false` (cảnh báo rõ); audit/metrics tắt mặc định (metrics local opt-in); eval chỉ là test.

## 8. Commit plan

```
feat(security): require trust before loading project resources
feat(security): restrict file writes with path allowlists
feat(security): record tool decisions in an audit log
feat(ui): show context usage and cost in the status area
feat(cli): add session stats command
feat(metrics): record local counters and optional debug logs
test(evals): add scripted scenarios for agent behavior
docs(security): document trust, permissions and sandboxing
```
