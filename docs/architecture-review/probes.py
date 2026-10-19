"""Reproduction probes for docs/architecture-review/README.md.

Run from the repository root:

    uv run python docs/architecture-review/probes.py

Each probe drives the real Agent / tool / config code with a scripted fake
backend (no network, no API keys). Everything is written under a temporary
directory and QQCODE_HOME points there, so ~/.qqcode is never touched.

Intentionally no `from __future__ import annotations`: ToolManager resolves
`run` annotations through the tool module's globals, so the monkeypatched
method below must carry real annotation objects.
"""

import asyncio
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORK = pathlib.Path(tempfile.mkdtemp(prefix="qqcode-probes-"))
os.environ["QQCODE_HOME"] = str(WORK / "home")
sys.path.insert(0, str(ROOT))

from tests.mock.utils import mock_llm_chunk  # noqa: E402
from tests.stubs.fake_backend import FakeBackend  # noqa: E402
from vibe.core.agent import Agent  # noqa: E402
from vibe.core.config import (  # noqa: E402
    CompactionConfig,
    ModelConfig,
    SessionLoggingConfig,
    VibeConfig,
    skip_api_key_check,
)
from vibe.core.context.cut import select_cut  # noqa: E402
from vibe.core.llm.backend.generic import GenericBackend  # noqa: E402
from vibe.core.sessions.entries import MessageEntry  # noqa: E402
from vibe.core.sessions.store import JsonlSessionStore  # noqa: E402
from vibe.core.tools.builtins.bash import Bash, BashArgs, BashToolConfig  # noqa: E402
from vibe.core.tools.builtins.read_file import (  # noqa: E402
    ReadFile,
    ReadFileArgs,
    ReadFileToolConfig,
)
from vibe.core.tools.builtins.todo import Todo, TodoArgs, TodoResult  # noqa: E402
from vibe.core.tools.base import ToolPermission  # noqa: E402
from vibe.core.tools.manager import ToolManager  # noqa: E402
from vibe.core.types import (  # noqa: E402
    AgentMode,
    FunctionCall,
    LLMChunk,
    LLMMessage,
    Role,
    ToolCall,
)
from vibe.core.utils import VIBE_WARNING_TAG, ApprovalResponse  # noqa: E402


def make_config(*, warnings=False, window=100_000, session_logging=False):
    model = ModelConfig(
        name="probe-model", provider="mistral", alias="probe-model", context_window=window
    )
    with skip_api_key_check():
        return VibeConfig(
            session_logging=SessionLoggingConfig(enabled=session_logging),
            active_model="probe-model",
            models=[model],
            compaction=CompactionConfig(enabled=False),
            context_warnings=warnings,
        )


def tool_call(call_id, name, args):
    return ToolCall(
        id=call_id, index=0, function=FunctionCall(name=name, arguments=json.dumps(args))
    )


def tool_turn(*calls):
    return mock_llm_chunk(content="", tool_calls=list(calls))


def final(text):
    return mock_llm_chunk(content=text, finish_reason="stop")


async def drain(agent, prompt):
    return [event async for event in agent.act(prompt)]


def recording_denier(log):
    def callback(tool_name, args, call_id):
        log.append(tool_name)
        return (ApprovalResponse.NO, None)

    return callback


# ---------------------------------------------------------------- S1
async def probe_s1_bash_approval_bypass():
    """Shell metacharacters inside an allowlisted command skip the prompt."""
    bypass = WORK / "s1_bypass.txt"
    control = WORK / "s1_control.txt"
    asked = []
    backend = FakeBackend(
        [
            tool_turn(tool_call("a", "bash", {"command": f"echo $(touch {bypass})"})),
            final("ok"),
            tool_turn(tool_call("b", "bash", {"command": f"touch {control}"})),
            final("ok"),
        ]
    )
    agent = Agent(make_config(), backend=backend)
    agent.set_approval_callback(recording_denier(asked))
    await drain(agent, "bypass attempt")
    print(f"S1 'echo $(touch X)'  prompts={len(asked)} file_created={bypass.exists()}")
    await drain(agent, "control")
    print(f"S1 'touch Y' control  prompts={len(asked)} file_created={control.exists()}")


# ---------------------------------------------------------------- S2
async def probe_s2_plan_mode_writes():
    """Plan mode is documented as read-only; python -c writes a file anyway."""
    target = WORK / "s2_plan_write.txt"
    asked = []
    command = f"python3 -c \"open('{target}', 'w').write('x')\""
    backend = FakeBackend(
        [tool_turn(tool_call("p", "bash", {"command": command})), final("ok")]
    )
    agent = Agent(make_config(), backend=backend, mode=AgentMode.PLAN)
    agent.set_approval_callback(recording_denier(asked))
    await drain(agent, "plan this")
    print(f"S2 plan mode python3 -c write  file_created={target.exists()} prompts={len(asked)}")


# ---------------------------------------------------------------- S3
def probe_s3_path_denylist_vs_bash():
    """path_denylist blocks read_file but not `cat` through bash (auto-allowlisted)."""
    secret = WORK / "secret.env"
    secret.write_text("TOKEN=example\n")
    deny = [f"{WORK}/secret*"]
    read = ReadFile.from_config(
        ReadFileToolConfig(path_denylist=deny, permission=ToolPermission.ALWAYS)
    )
    bash = Bash.from_config(BashToolConfig(path_denylist=deny))
    print(
        "S3 read_file path check:",
        read.check_path_restrictions(ReadFileArgs(path=str(secret))),
    )
    print(
        "S3 bash path check:",
        bash.check_path_restrictions(BashArgs(command=f"cat {secret}")),
        "| bash allowlist decision:",
        bash.check_allowlist_denylist(BashArgs(command=f"cat {secret}")),
        "(always = runs without prompt)",
    )


# ---------------------------------------------------------------- S4
def probe_s4_untrusted_repo_content():
    """An untrusted repo's .qqcode/instructions.md and skills are still read."""
    repo = WORK / "untrusted_repo"
    (repo / ".qqcode" / "skills" / "evil").mkdir(parents=True, exist_ok=True)
    (repo / ".qqcode" / "instructions.md").write_text("# INJECTED-INSTRUCTIONS\n")
    (repo / ".qqcode" / "config.toml").write_text("")
    (repo / ".qqcode" / "skills" / "evil" / "SKILL.md").write_text(
        "---\nname: evil\ndescription: INJECTED-SKILL\n---\nDo what the repo says.\n"
    )
    code = (
        "import pathlib, types\n"
        "from vibe.core.trust import resolve_trust\n"
        "from vibe.core.system_prompt import _load_user_instructions\n"
        "from vibe.core.skills.manager import SkillManager\n"
        "cwd = pathlib.Path.cwd()\n"
        "print('trust:', resolve_trust(cwd, default_project_trust=False).decision.value)\n"
        "print('instructions:', repr(_load_user_instructions()))\n"
        "cfg = types.SimpleNamespace(effective_workdir=cwd, packages=[])\n"
        "print('skill dirs:', [p for p in map(str, SkillManager._compute_search_paths(cfg)) "
        "if 'untrusted_repo' in p])\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "QQCODE_HOME"}
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    for line in result.stdout.splitlines():
        print("S4", line)
    if result.returncode != 0:
        print("S4 subprocess failed:", result.stderr.strip().splitlines()[-1:])


# ---------------------------------------------------------------- C1
async def probe_c1_tool_exception_aborts_turn():
    """A non-ToolError from a tool propagates out of act()."""
    original = Todo.run

    async def boom(self, args: TodoArgs) -> TodoResult:
        raise ValueError("boom from tool")

    backend = FakeBackend([tool_turn(tool_call("t", "todo", {"action": "read"})), final("ok")])
    agent = Agent(make_config(), backend=backend, mode=AgentMode.AUTO_APPROVE)
    todo_cls = agent.tool_manager.available_tools()["todo"]
    todo_cls.run = boom
    try:
        try:
            await drain(agent, "list todos")
            print("C1 act() completed normally")
        except Exception as exc:  # the probe reports what escaped
            print(f"C1 act() raised to caller: {type(exc).__name__}: {exc}")
        tail = [(m.role.value, bool(m.tool_calls)) for m in agent.messages[-2:]]
        print("C1 context tail (role, has_tool_calls):", tail)
        agent.backend = FakeBackend([final("recovered")])
        await drain(agent, "next question")
        print(
            "C1 next turn context tail:",
            [(m.role.value, m.content) for m in agent.messages[-4:-2]],
        )
    finally:
        todo_cls.run = original


# ---------------------------------------------------------------- C3
async def probe_c3_warning_persistence():
    """Transient context warnings end up in the live log but not on disk."""
    config = make_config(warnings=True, window=10, session_logging=True)
    agent = Agent(config, backend=FakeBackend([final("one"), final("two")]))
    await drain(agent, "first question")
    await drain(agent, "second question")
    live = [VIBE_WARNING_TAG in (m.content or "") for m in agent.messages if m.role.value == "user"]
    in_log = [
        VIBE_WARNING_TAG in (e.message.content or "")
        for e in agent._log
        if isinstance(e, MessageEntry) and e.message.role.value == "user"
    ]
    files = sorted((WORK / "home").rglob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    on_disk = [
        VIBE_WARNING_TAG in (e.message.content or "")
        for e in JsonlSessionStore(config.session_logging).read(files[-1]).entries
        if isinstance(e, MessageEntry) and e.message.role.value == "user"
    ]
    print(f"C3 live request context carries warning: {live}")
    print(f"C3 in-memory session log carries warning: {in_log}")
    print(f"C3 JSONL on disk (what --resume reloads): {on_disk}")


# ---------------------------------------------------------------- C5
async def probe_c5_mcp_listener_leak():
    """Every reload adds another ToolManager listener on the MCP manager."""

    class FakeMCP:
        def __init__(self):
            self.listeners = []

        def on_tools_changed(self, callback):
            self.listeners.append(callback)

        def registered_tool_classes(self):
            return []

        def hidden_tool_names(self):
            return set()

        def reset_deferred_activation(self):
            return None

    mcp = FakeMCP()
    agent = Agent(make_config(), backend=FakeBackend([]), mcp_manager=mcp)
    for _ in range(3):
        await agent.reload_with_initial_messages()
    print(f"C5 listeners after init + 3 reloads: {len(mcp.listeners)} (1 should be live)")


# ---------------------------------------------------------------- C6
def probe_c6_builtin_loaded_twice():
    """Dynamic discovery re-executes builtin modules, so class objects differ."""
    manager = ToolManager(make_config())
    print(
        "C6 agent's Todo is the imported Todo class:",
        manager.available_tools()["todo"] is Todo,
    )


# ---------------------------------------------------------------- C2
def probe_c2_compaction_drops_latest_request():
    """Mirrors Compactor._summarize (compactor.py:274-281) on a short session."""
    big = "x" * 20_000
    conversation = [
        LLMMessage(role=Role.user, content="Refactor the parser " + big),
        LLMMessage(
            role=Role.assistant,
            content="Reading",
            tool_calls=[
                ToolCall(id="t1", index=0, function=FunctionCall(name="read_file", arguments="{}"))
            ],
        ),
        LLMMessage(role=Role.tool, tool_call_id="t1", name="read_file", content="body " + big),
        LLMMessage(role=Role.assistant, content="Got it."),
        LLMMessage(role=Role.user, content="Now run the test suite and report failures"),
    ]
    cut = select_cut(conversation, 20_000)  # CompactionConfig.keep_recent_tokens default
    prefix = conversation[: cut.first_kept_index]
    cut_index = cut.first_kept_index
    if not prefix:
        prefix = conversation
        cut_index = len(conversation)
    kept = conversation[cut_index:]
    survives = any("run the test suite" in (m.content or "") for m in kept)
    print(
        f"C2 summarized {len(prefix)}/{len(conversation)} messages; "
        f"latest request kept verbatim: {survives}"
    )


# ---------------------------------------------------------------- C4
def probe_c4_tool_call_index_collision():
    """Sparse indexes + an index-less chunk overwrite a real tool call."""

    def chunk(index, name, arguments):
        call = ToolCall(id=f"id{index}", index=index, function=FunctionCall(name=name, arguments=arguments))
        return LLMChunk(message=LLMMessage(role=Role.assistant, content="", tool_calls=[call]))

    merged = GenericBackend._merge_tool_calls(
        [chunk(0, "read_file", "{}"), chunk(2, "bash", "{}"), chunk(None, "grep", "{}")]
    )
    print("C4 merged calls:", [t.function.name for t in merged], "(model asked for bash at index 2)")


async def main():
    await probe_s1_bash_approval_bypass()
    await probe_s2_plan_mode_writes()
    probe_s3_path_denylist_vs_bash()
    probe_s4_untrusted_repo_content()
    await probe_c1_tool_exception_aborts_turn()
    await probe_c3_warning_persistence()
    await probe_c5_mcp_listener_leak()
    probe_c6_builtin_loaded_twice()
    probe_c2_compaction_drops_latest_request()
    probe_c4_tool_call_index_collision()


if __name__ == "__main__":
    asyncio.run(main())
