# 07 — Sessions, Durability, Security, Telemetry & Evals

> Nguồn: **QQcode** `@ be6a96c` · **Pi** `@ 636703a0a`. Bằng chứng dạng `file:line`.

## 1. Tóm tắt điều hành

QQcode lưu session dạng **một file JSON snapshot ghi đè toàn bộ** mỗi lần tương tác (`interaction_logger.py:155-204`), resume được nhưng **không** branch/fork/tree, **không** ghi tăng dần, **không** phục hồi sau crash giữa turn. Pi dùng **JSONL tree append** với entry types + labels + usage + compaction; kèm package `durable` cho task graph, crash recovery và subagent.

Về **bảo mật**, QQcode mạnh hơn Pi ở tầng built-in (permission 3 mức, approval UI, allow/deny, plan mode, cảnh báo thư mục nguy hiểm), nhưng thiếu **project trust**, **sandbox/containerization**, và hệ sinh thái extension gate. Pi ngược lại: không có permission built-in (README nói rõ) nhưng có trust store + tài liệu containerization + extension permission-gate.

Về **vận hành**, QQcode hoàn toàn không có telemetry/evals; Pi có package telemetry, evals, test suite với faux provider, và install telemetry opt-out.

### Bảng điểm trục Sessions / Security / Ops

| # | Hạng mục | QQcode | Pi | Mức độ |
|---|---|---|---|---|
| S1 | Định dạng session | JSON snapshot, overwrite | JSONL append tree v3 | G3 |
| S2 | Incremental write | Không | Có | G2 |
| S3 | Tree/fork/clone | Không | Có | G3 |
| S4 | Entry types (model change, compaction, label, usage) | Không (chỉ messages + metadata) | Có | G2 |
| S5 | Export/import/share | Không | HTML/JSONL/gist | G2 |
| S6 | Crash recovery | Không | Durable commit + reopen | G3 |
| S7 | Subagent/task graph | Không | Durable + extension | G3 |
| S8 | Permission built-in | Có | Không | G0 |
| S9 | Project trust | Không | Có | G2 |
| S10 | Sandbox/containerization | Không | Tài liệu + extension Gondolin/Docker/OpenShell | G2 |
| S11 | Telemetry | Không | Package + install ping + analytics opt-in | G2 |
| S12 | Evals | Không (chỉ unit test) | Package evals + suite faux provider | G2 |
| S13 | Cost/usage hiển thị | Status + stats cơ bản | Footer đầy đủ + `/session` + cache waste | G1 |
| S14 | Rate limit handling | Retry 3 lần | Retry-After + retry events UI | G1 |
| S15 | Log chẩn đoán | `~/.qqcode/vibe.log` | `pi-debug.log` + `/bug` + crash log | G1 |

---

## 2. QQcode hiện có gì

### 2.1 Session format

`InteractionLogger.save_interaction` (`interaction_logger.py:155-204`) ghi **toàn bộ**:

```python
interaction_data = {
    "metadata": {
        **self.session_metadata.model_dump(),
        "end_time": ...,
        "stats": stats.model_dump(),
        "total_messages": len(messages),
        "tools_available": tools_available,
        "agent_config": config.model_dump(mode="json"),
        "additional_directory_contexts": [...],
    },
    "messages": [m.model_dump(exclude_none=True) for m in messages],
}
```

- File name `session_YYYYMMDD_HHMMSS_<id8>.json` (`:72-78`), thư mục `~/.qqcode/logs/session` (`config.py:133-148`).
- Ghi đè bằng `aiofiles.open(..., "w")` mỗi lần `save_interaction` — được gọi rất thường xuyên: sau mỗi LLM turn, sau mỗi tool, lúc middleware stop, lúc lỗi (`agent.py:273-275`, `346-348`, `352-355`, `369-371`, `374-377`, `1074-1076`, `1120-1122`, `1230-1232`).
- Metadata gồm git commit/branch/username/workdir (`interaction_logger.py:118-132`).
- Resume: `--continue` lấy file mtime mới nhất (`:235-247`); `--resume` khớp id (`:249-275`); `load_session` parse lại messages (`:348-356`).
- `--list-sessions`/`--get-session` phục vụ VSCode extension (`entrypoint.py:233-277`).
- `clear_history` chỉ giữ `messages[:1]` (system) và reset session (`agent.py:1051-1069`).
- Compact reset session id → file mới, file cũ còn nhưng không có UI quay lại (`agent.py:1119`).

Hệ quả: file phình theo toàn bộ hội thoại; ghi lại từ đầu là O(n) mỗi event; không có cấu trúc để branch; không có ghi atomic/fsync nên crash giữa ghi có thể làm hỏng JSON.

### 2.2 Bảo mật hiện có

- `ToolPermission` 3 mức `ALWAYS/NEVER/ASK` (`tools/base.py:39-51`); config per tool (`base.py:54-69`); allow/deny match qua `check_allowlist_denylist` từng tool (`bash.py:258-297`, `read_file.py:87-103`).
- Luồng quyết định: plan mode allowlist > auto-approve > allow/deny list > config permission > approval callback (`agent.py:889-952`).
- Approval UI: Approve / Approve always / Deny (`app.py:235-295`); ACP permission options (`acp/utils.py:77-119`).
- Plan mode: tool allowlist (`agent.py:69-71`), bash allow/deny read-only (`bash.py:153-239`).
- Dangerous directory: tắt scan project context, in prompt nhắc model (`utils.py:105-141`; `system_prompt.py:417-423`).
- Không có: project trust, permission theo đường dẫn ngoài tool-specific denylist, sandbox process, giới hạn network, audit log riêng, hook chặn tool của bên thứ ba.

### 2.3 Vận hành

- Log: `logging` ghi `~/.qqcode/vibe.log` (`utils.py:144-154`).
- Update notifier: check GitHub release (`vibe/cli/update_notifier/`), config `enable_update_checks` (`config.py:445`).
- Stats: `AgentStats` (`types.py:45-112`) và `/status` (`app.py:810-831`); UI có `context_progress` widget.
- Không có telemetry, không có eval framework; test là pytest unit + snapshot (`tests/`, 60 file; `pytest-textual-snapshot`, `respx`).
- CI: `.github/workflows/ci.yml` (lint/type/test), build/release workflows.

---

## 3. Pi có gì

### 3.1 Session

- JSONL tree v3: entry có `id`/`parentId`/type: `session`, `message`, `model_change`, `thinking_level_change`, `compaction`, `branch_summary`, `custom`, `custom_message`, `context_edit`, `session_info`, `label`, `usage` (`docs/session-format.md:62-160`).
- `SessionManager`: `create/open/continueRecent/inMemory/forkFrom`, `list/listAll`, `appendCompaction`, `appendContextEdit`, tree navigation, `importFromJsonl`/`exportToJsonl` (`session-manager.ts:1261`, `1360`, `1755-1815`).
- Branch: `/tree`, `/fork`, `/clone`, `--fork`; branch summarization khi rời nhánh (`docs/compaction.md:206-232`).
- Export HTML themed + JSONL; `/import`; `/share` Radius artifact hoặc GitHub gist (`session-share.ts:56-210`).
- `/session` hiển thị stats/usage/cost; `usage` entry theo dõi token/cost.

### 3.2 Durable runtime (package riêng)

- Task graph replicated: `defineTask` state machine versioned, phases, abort, migrate (`durable/src/tasks.ts:4-8`; `types.ts:232-256`).
- Commit atomic có sequence; SQLite/JSONL/memory storage (`session.ts:404-444`; `storage/sqlite/migrations.ts:9-88`; `storage/jsonl/storage.ts:28-59`).
- Crash recovery: task `running → pending` khi reopen, reconcile abort (`scheduler.ts:235-261`).
- Subagent: child conversation thuộc task; ví dụ foreground/background, restart-safe (`durable/test/examples/22-subagent-foreground.ts`, `23-subagent-background.ts`; `coding-agent/src/experimental/durable/subagent.ts:24-55`).
- Plan mode example, todo example (không built-in).
- Deferred response polling (`generation.ts:429-448`); retry policy (`agent.ts:19-24`).

### 3.3 Security

- Pi **không** có permission system built-in — README nói rõ chạy bằng quyền user (`pi/README.md:88-96`).
- Project trust store `~/.pi/agent/trust.json` + `defaultProjectTrust` + `--approve/--no-approve` (`docs/security.md:59-82`).
- Containerization patterns: Gondolin micro-VM extension, plain Docker, OpenShell (`docs/containerization.md`; `README.md:90-96`).
- Extension examples: `permission-gate.ts`, `protected-paths.ts`, `dirty-repo-guard.ts`, `sandbox/`.
- Tool allow/deny: `--tools`, `--exclude-tools`, `defaultTools` settings (`docs/cli.md:119-138`).

### 3.4 Telemetry & Evals

- `packages/telemetry`: span API vendor-neutral, noop/in-memory adapter, conformance suite (`telemetry/src/index.ts:1-74`).
- Install telemetry mặc định bật, opt-out `PI_TELEMETRY`; analytics opt-in (`core/telemetry.ts:8-13`; `settings-manager.ts:155-156`).
- `packages/evals`: workspace riêng với host + docs eval config, Docker (`evals/package.json:1-20`).
- Test suite dùng faux provider, không cần API key (`docs` + `test/suite/harness.ts`; AGENTS.md:38).
- `/bug` gửi báo cáo + transcript (`core/bug-report.ts`); crash log.

---

## 4. Bảng gap chi tiết

| Tính năng | QQcode | Pi | Bằng chứng QQcode | Bằng chứng Pi | Mức độ |
|---|---|---|---|---|---|
| Ghi session tăng dần | không (overwrite JSON) | JSONL append | `interaction_logger.py:196-201` | `docs/session-format.md:10-14` | G3 |
| Entry typed (model/compaction/label) | không | có | `interaction_logger.py:183-194` | `session-format.md:62-160` | G2 |
| Fork/tree/clone | không | có | không có | `interactive-mode.ts:3253-3262`, `5517`; `session-manager.ts:1755-1815` | G3 |
| Branch summary | không | có | không có | `branch-summarization.ts:258-364` | G2 |
| Export/import | không | HTML + JSONL | không có | `interactive-mode.ts:6493-6582` | G2 |
| Ghi atomic/chống hỏng | không (write đè) | commit seq + recovery | `interaction_logger.py:199-200` | `durable/src/session/session.ts:404-444` | G3 |
| Resume sau crash giữa turn | không | durable reopen | không có | `scheduler.ts:235-257` | G3 |
| Subagent | không | durable + example | không có | `durable/test/examples/22,23` | G3 |
| Task graph / background task | không | có | không có | `durable/src/harness/task-graph.ts:18-148` | G3 |
| Project trust | không | có | không có | `docs/security.md:59-82` | G2 |
| Sandbox/container | không | tài liệu + extension | không có | `docs/containerization.md` | G2 |
| Telemetry | không | package + opt-out | không có | `telemetry/src/index.ts` | G2 |
| Evals | không | package evals | `tests/` unit only | `evals/package.json` | G2 |
| Bug report | không | `/bug` | không có | `bug-report.ts` | G1 |
| Cost/cache hiển thị | stats cơ bản | footer + `/session` + cache waste | `types.py:74-89`; `app.py:810-831` | `footer.ts:179-212`; `interactive-mode.ts:6666-6699` | G1 |
| Rate limit UI | không | retry events | `utils.py:171-216` | `docs/json.md:161-180`; `docs/settings.md:125-133` | G1 |
| Debug log/command | `vibe.log` | `pi-debug.log` + `/debug` | `utils.py:144-154` | `interactive-mode.ts:3300` | G1 |

---

## 5. Ưu tiên

1. **P0 — JSONL append session** (G3): thay `save_interaction` bằng append entry (`{"type": "message"|"metadata"|"compact"|...}`) + fsync; giữ writer snapshot cũ để migrate. Đây là nền tảng cho fork/tree (doc 02) và crash recovery (doc 03).
2. **P1 — Atomic write & recovery**: thêm `tmp + rename` hoặc log-structured; khi resume phát hiện file hỏng/đứt thì cắt tới entry hợp lệ cuối.
3. **P1 — Usage/cost entry**: ghi token/cost mỗi turn (tận dụng `AgentStats`) để `/status` và UI hiển thị context/cache chính xác; thêm cache read/write nếu backend cung cấp.
4. **P2 — Export/import**: `/export` markdown/HTML từ session JSONL; `/import` ngược. Rẻ hơn fork.
5. **P2 — Subagent tối giản**: một tool `task` spawn Agent con với context riêng + budget, đọc kết quả cuối; chưa cần task graph/durable.
6. **P3 — Telemetry/evals**: chưa cần vendor; tối thiểu thêm một eval harness chạy scenario cố định bằng FakeBackend (đã có `tests/mock`, `tests/stubs`) để đo regression prompt/tool.

> **Counterfactual**: giữ snapshot overwrite, một crash đúng lúc ghi có thể mất **toàn bộ** session (không chỉ turn cuối); và không có cách nào thử một hướng khác mà không mất hội thoại hiện tại.
