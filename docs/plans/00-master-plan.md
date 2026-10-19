# Master Plan — Cải tiến QQcode (bộ plan chi tiết)

Bộ plan này chuyển các phát hiện trong `docs/gap-analysis/` thành kế hoạch triển khai cụ thể cho QQcode. Mỗi plan tự chứa thiết kế, task breakdown, test plan và tiêu chí hoàn thành; có thể giao cho một kỹ sư hoặc agent thực thi độc lập.

## 0. Trạng thái triển khai (cập nhật 2026-10-19)

- **Xong:** Toàn bộ 17/17 plans (`01`–`17`): 01–11, 12 (context store edits/head marker/delta), 13 (image input), 14 (embedding SDK), 15 (keybindings), 16 (deferred exposure + `tool_search`), 17 (sandboxed tool scripting).
- **Còn lại:** Không còn (0/17).
- **Việc nền đã dọn:** gate `ruff`/`ruff format --check`/`pyright`/`pytest` xanh.
- **Lệch plan có ghi chú:** plan 13 dùng field `LLMMessage.images` thay vì union `Content`; plan 16 giữ `mcp_search` cho server MCP cấu hình `deferred` và dùng `tool_search` cho auto-defer/override.

## 1. Nguyên tắc thực thi

1. **Được phép tái sử dụng code tham chiếu.** Đã được chủ repo cho phép copy/port code từ các dự án tham chiếu (pi, MIT) để đạt tương đương chức năng. Giữ license/attribution của code đã copy trong `THIRD_PARTY_NOTICES.md`, không cần ghi nguồn trong commit message hay trong source code.
2. **Commit hygiene.** Commit/PR/branch mô tả **vấn đề của QQcode và giá trị người dùng** bằng ngôn ngữ QQcode; không nhắc tên dự án tham chiếu, không dùng "port/adapted from", không dán link ngoài.
3. **Không phá vỡ tương thích.** Config cũ (`config.toml`), session JSON cũ, CLI flag cũ phải tiếp tục hoạt động; mọi thay đổi format đều có reader tương thích hoặc lệnh migrate.
4. **Mỗi thay đổi có test.** Gate bắt buộc trước khi merge:
   ```bash
   uv run ruff check .
   uv run ruff format --check .
   uv run pyright
   uv run pytest
   ```
5. **Đo lường được.** Mọi plan nêu rõ chỉ số/DoD kiểm chứng được bằng test hoặc log, không đánh giá bằng cảm nhận.
6. **Độ phủ.** Bộ plan bao phủ **toàn bộ** 63 hạng mục gap mức G2/G3 của `docs/gap-analysis` (sau khi bổ sung plan 12–17); các hạng mục G0/G1 còn lại là tùy chọn/backlog, không nằm trong phạm vi bắt buộc.

## 2. Bản đồ plan → gap

| Plan | File | Gap ID (từ gap-analysis) | Mức độ | Wave |
|---|---|---|---|---|
| 01 | `01-context-engineering.md` | X1, X2, X3, X4, X5, X6, X10, X12 | G3×3, G2×5 | W1 |
| 02 | `02-session-store.md` | S1, S2, S3, S4, S6, X15 | G3×4, G2×2 | W1 |
| 03 | `03-tool-runtime.md` | H2, H3, H4, H5, H11, H12, X11 | G2×6, G1×1 | W1 |
| 04 | `04-prompt-architecture.md` | R1, R2, R3, R14, E3, E5 | G2×4, G1×2 | W1 |
| 05 | `05-provider-registry.md` | P1, P3, P4, P6, P7, P11 | G3×2, G2×4 | W2 |
| 06 | `06-provider-adapters-auth-reasoning.md` | P2, P5, P8, P9, P10, P14 | G2×5, G1×1 | W2 |
| 07 | `07-commands-cli.md` | C2, C3, C4, C6, C7, C8, C9, C13, C14, S5 | G3×2, G2×7, G1×1 | W2 |
| 08 | `08-extensibility-hooks-packages.md` | E1, E4, E11, E13, E14, E15, E16, X13 | G3×2, G2×6 | W3 |
| 09 | `09-mcp-hardening.md` | E7, E8, E9, E10 | G2×4 | W3 |
| 10 | `10-security-observability-evals.md` | S9, S10, S11, S12, S13, S14 | G2×5, G1×1 | W3 |
| 11 | `11-subagent.md` | S7, H9 | G3×1, G2×1 | W4 |
| 12 | `12-context-store-edits.md` | X7, X8, X9 | G3×1, G2×2 | W2 |
| 13 | `13-image-input.md` | P12, H17 | G2×2 | W3 |
| 14 | `14-embedding-sdk.md` | C5 | G2×1 | W3 |
| 15 | `15-keybindings.md` | C10 | G2×1 | W2 |
| 16 | `16-deferred-tool-exposure.md` | H15 | G2×1 | W3 |
| 17 | `17-tool-scripting.md` | H14 | G2×1 | W3 |

## 3. Wave & dependency

```
W1 (nền tảng, làm trước)
  01 context-engineering ──┐
  02 session-store ────────┤──> W2
  03 tool-runtime ─────────┤
  04 prompt-architecture ──┘

W2 (provider + điều khiển)
  05 provider-registry ──> 06 adapters/auth/reasoning
  07 commands-cli (cần 02 cho export/fork, cần 05 cho /model)
  12 context-store-edits (cần 02 entry log)
  15 keybindings (độc lập TUI)

W3 (hệ sinh thái)
  08 extensibility (cần 03 hook point, cần 04 prompt sections; thêm before/after compact)
  09 mcp-hardening (cần 08 hook tối thiểu để gate; có thể song song)
  10 security/observability (cần 02 usage entries; cần 05 cost metadata)
  13 image-input (cần 05 capabilities.vision, cần 03 read/truncate)
  14 embedding-sdk (cần 07 event bus, cần 02 session store)
  16 deferred-tool-exposure (cần 03 tool runtime)
  17 tool-scripting (cần 03 tool runtime; độc lập 16)

W4
  11 subagent (cần 01 context budget, cần 03 tool runtime, cần 08 hook)
```

Quy tắc: plan sau **không được** bắt đầu khi plan trước trong cùng chuỗi chưa merge; plan song song chỉ khi không chạm cùng file (kiểm tra cột "File" trong task breakdown).

## 4. Quy ước chung

### 4.1 Cấu trúc module mới

```
vibe/core/
├── context/            # 01: tokens.py, cut.py, compactor.py, overflow.py
│                       # 12: builder.py (context derive từ entry log)
├── sessions/           # 02: store.py, entries.py, migrate.py
├── tools/truncate.py   # 03
├── tools/search.py     # 16: BM25 cho deferred tool
├── tools/script_sandbox.py  # 17: sandbox (experimental)
├── prompts/            # 04: sections.py, templates.py (mở rộng)
├── llm/capabilities.py # 05
├── llm/backends/       # 06: adapter registry (giữ package cũ để tránh vỡ import)
│                       # 13: encode ảnh trong adapter
├── hooks/registry.py   # 08
├── mcp/manager.py      # 09 (chuyển logic từ tools/mcp.py, giữ shim)
├── evals/              # 10: harness nội bộ
├── events.py           # 14: EventBus chung (CLI/SDK/VSCode)
└── images.py           # 13: resize/encode ảnh (Pillow optional)

vibe/sdk/               # 14: API nhúng public (options.py, agent.py, events.py)
vibe/cli/keybindings.py # 15: action registry + build bindings
```

### 4.2 Format commit

Dùng Conventional Commits, mô tả hành vi:

```
feat(context): estimate token usage before each request
feat(context): keep recent turns when compacting history
fix(context): recover automatically from provider context overflow
feat(sessions): append conversation entries to a jsonl log
feat(tools): execute independent tool calls in parallel
refactor(prompt): rebuild system prompt from named sections
```

Cấm trong commit/PR: tên dự án khác, từ "port", "adapted from", link ngoài, so sánh với sản phẩm khác. Mô tả bằng ngôn ngữ QQcode (`Agent`, `middleware`, `BaseTool`, ...).

### 4.3 Định nghĩa Done chung

Một task chỉ được coi là xong khi:
- [ ] Code + docstring (không comment thừa), tuân thủ `AGENTS.md` (Python 3.12, match-case, type hints hiện đại, pathlib, không inline ignore).
- [ ] Test mới chạy xanh, test cũ không đổi hành vi.
- [ ] `ruff`, `pyright`, `pytest` xanh.
- [ ] Config cũ vẫn load (có test migration nếu format đổi).
- [ ] Cập nhật `README.md`/docs nếu là tính năng người dùng.
- [ ] Commit message theo mục 4.2.

### 4.4 Ước lượng

| Ký hiệu | Nghĩa |
|---|---|
| S | ≤ 0.5 ngày |
| M | 1–2 ngày |
| L | 3–5 ngày |
| XL | > 5 ngày (nên tách) |

## 5. Cách dùng bộ plan

- Mỗi plan có "Task breakdown" dạng bảng; tạo issue/branch theo từng task, không gộp nhiều task lớn vào một PR.
- Thứ tự task trong plan đã được sắp theo dependency nội bộ; không đảo thứ tự nếu bảng ghi rõ phụ thuộc.
- Khi phát hiện thiết kế cần lệch khỏi plan: cập nhật plan trong cùng PR với code để tài liệu không lệch thực tế.
