from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from vibe.core.context.cut import CutResult, select_cut
from vibe.core.context.tokens import estimate_context_tokens
from vibe.core.hooks.events import (
    AfterCompactEvent,
    BeforeCompactEvent,
    CompactFailedEvent,
    HookDecision,
)
from vibe.core.llm.cache import CachePlan
from vibe.core.middleware import ResetReason
from vibe.core.prompts import UtilityPrompt
from vibe.core.sessions.entries import CompactionEntry, new_entry_id, now_iso
from vibe.core.types import LLMMessage, LLMUsage, Role

if TYPE_CHECKING:
    from vibe.core.agent import Agent

SUMMARIZER_SYSTEM_PROMPT = (
    "You summarize conversations. Do not continue the conversation."
)
SUMMARY_TAG = "conversation_summary"
TOOL_RESULT_MAX_CHARS = 2000

type CompactionReason = Literal["auto", "manual", "overflow"]


@dataclass
class CompactionResult:
    summary: str
    cut: CutResult
    tokens_before: int
    tokens_after_estimate: int
    usage: LLMUsage | None
    is_update: bool


def _truncate(text: str | None, limit: int = TOOL_RESULT_MAX_CHARS) -> str:
    if not text:
        return "(empty)"
    if len(text) <= limit:
        return text
    return f"{text[:limit]} ...[truncated]"


def serialize_for_summary(messages: list[LLMMessage]) -> str:
    blocks: list[str] = []
    for message in messages:
        match message.role:
            case Role.user:
                blocks.append(f"[USER]\n{message.content or '(empty)'}")
            case Role.assistant:
                body = f"[ASSISTANT]\n{message.content or '(empty)'}"
                if message.tool_calls:
                    calls = "; ".join(
                        f"{tool_call.function.name}({tool_call.function.arguments})"
                        for tool_call in message.tool_calls
                    )
                    body = f"{body}\n[TOOL CALLS] {calls}"
                blocks.append(body)
            case Role.tool:
                name = f" ({message.name})" if message.name else ""
                blocks.append(f"[TOOL RESULT{name}]\n{_truncate(message.content)}")
            case Role.system:
                blocks.append(f"[SYSTEM]\n{_truncate(message.content)}")
    return "\n\n".join(blocks)


def find_previous_summary(messages: list[LLMMessage]) -> str | None:
    for message in reversed(messages):
        content = message.content or ""
        if message.role is Role.user and f"<{SUMMARY_TAG}>" in content:
            start = content.find(f"<{SUMMARY_TAG}>") + len(f"<{SUMMARY_TAG}>")
            end = content.find(f"</{SUMMARY_TAG}>")
            return content[start:end].strip() if end != -1 else content[start:].strip()
    return None


def _build_summary_prompt(
    messages: list[LLMMessage],
    previous_summary: str | None,
    instructions: str | None,
    *,
    use_legacy: bool,
    use_update: bool,
) -> str:
    serialized = serialize_for_summary(messages)
    parts = [f"<conversation>\n{serialized}\n</conversation>"]

    match (use_legacy, use_update):
        case (True, _):
            parts.append(UtilityPrompt.COMPACT.read())
        case (False, True):
            parts.append(f"<previous_summary>\n{previous_summary}\n</previous_summary>")
            parts.append(UtilityPrompt.SUMMARY_UPDATE.read())
        case _:
            parts.append(UtilityPrompt.SUMMARY.read())

    if instructions:
        parts.append(f"Additional instructions from the user: {instructions}")
    return "\n\n".join(parts)


class Compactor:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent
        self._previous_summary: str | None = None

    def reset(self) -> None:
        self._previous_summary = None

    def _estimate_context(self) -> int:
        agent = self._agent
        tools = agent.format_handler.get_available_tools(
            agent.tool_manager, agent.config
        )
        return estimate_context_tokens(
            agent.messages,
            tools,
            agent._last_usage,
            agent.stats.last_usage_message_index,
        )

    @staticmethod
    def _skipped_result(tokens: int) -> CompactionResult:
        return CompactionResult(
            summary="",
            cut=CutResult(first_kept_index=0, is_split_turn=False),
            tokens_before=tokens,
            tokens_after_estimate=tokens,
            usage=None,
            is_update=False,
        )

    @staticmethod
    def _with_last_user_message(summary: str, conversation: list[LLMMessage]) -> str:
        last_user_message = next(
            (
                message.content
                for message in reversed(conversation)
                if message.role is Role.user and message.content
            ),
            None,
        )
        if not last_user_message:
            return summary
        return f"{summary}\n\nLast request from user was: {last_user_message}"

    def _estimate_messages(self, messages: list[LLMMessage]) -> int:
        agent = self._agent
        tools = agent.format_handler.get_available_tools(
            agent.tool_manager, agent.config
        )
        return estimate_context_tokens(messages, tools, None, -1)

    async def _install_summary(
        self,
        summary: str,
        kept: list[LLMMessage],
        cut_index: int,
        *,
        cut: CutResult,
        first_kept_id: str | None,
        tokens_before: int,
        usage: LLMUsage | None,
        is_update: bool,
    ) -> CompactionResult:
        agent = self._agent
        summary_message = LLMMessage(
            role=Role.user, content=f"<{SUMMARY_TAG}>\n{summary}\n</{SUMMARY_TAG}>"
        )
        tokens_after = self._estimate_messages([
            agent._system_message,
            summary_message,
            *kept,
        ])
        agent._append_entry(
            CompactionEntry(
                id=new_entry_id(),
                parent_id=agent._last_entry_id(),
                timestamp=now_iso(),
                first_kept_id=first_kept_id,
                summary=summary,
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                is_update=is_update,
            )
        )
        agent._last_usage = None
        agent.stats.last_usage_message_index = -1
        agent._refresh_context_stats()
        agent.middleware_pipeline.reset(reset_reason=ResetReason.COMPACT)

        return CompactionResult(
            summary=summary,
            cut=CutResult(first_kept_index=cut_index, is_split_turn=cut.is_split_turn),
            tokens_before=tokens_before,
            tokens_after_estimate=tokens_after,
            usage=usage,
            is_update=is_update,
        )

    @staticmethod
    def _summary_override(decision: HookDecision) -> str | None:
        if decision.action != "modify" or not decision.summary_override:
            return None
        return decision.summary_override.strip() or None

    async def compact(
        self,
        *,
        instructions: str | None = None,
        force: bool = False,
        reason: CompactionReason = "auto",
    ) -> CompactionResult:
        agent = self._agent
        config = agent.config
        tokens_before = self._estimate_context()

        if not config.compaction.enabled and not force:
            return self._skipped_result(tokens_before)

        decision = await agent.hooks.dispatch(
            BeforeCompactEvent(
                messages=list(agent.messages),
                tokens_before=tokens_before,
                reason=reason,
            )
        )
        if decision.action == "deny":
            return self._skipped_result(tokens_before)

        try:
            result = await self._summarize(
                instructions=instructions,
                tokens_before=tokens_before,
                summary_override=self._summary_override(decision),
            )
        except Exception as exc:
            await agent.hooks.dispatch(CompactFailedEvent(error=str(exc)))
            raise

        await agent.hooks.dispatch(
            AfterCompactEvent(
                tokens_before=result.tokens_before,
                tokens_after=result.tokens_after_estimate,
                is_update=result.is_update,
            )
        )
        return result

    async def _summarize(
        self,
        *,
        instructions: str | None,
        tokens_before: int,
        summary_override: str | None,
    ) -> CompactionResult:
        agent = self._agent
        config = agent.config

        agent._clean_message_history()
        await agent._recorder.flush()

        conversation = agent.messages[1:]
        if not conversation:
            return self._skipped_result(tokens_before)

        cut = select_cut(conversation, config.compaction.keep_recent_tokens)
        prefix = conversation[: cut.first_kept_index]
        cut_index = cut.first_kept_index
        if not prefix:
            prefix = conversation
            cut_index = len(conversation)
        first_kept_id = (
            agent._entry_id_at(cut_index + 1) if cut_index < len(conversation) else None
        )

        previous_summary = self._previous_summary or find_previous_summary(conversation)
        use_update = (
            previous_summary is not None and config.compaction.prompt != "legacy"
        )

        summary, usage = await self._produce_summary(
            prefix,
            previous_summary,
            instructions,
            cut_index,
            len(conversation),
            use_update=use_update,
            summary_override=summary_override,
        )

        result = await self._install_summary(
            summary,
            conversation[cut_index:],
            cut_index,
            cut=cut,
            first_kept_id=first_kept_id,
            tokens_before=tokens_before,
            usage=usage,
            is_update=use_update,
        )
        self._previous_summary = summary
        await agent._recorder.flush()
        return result

    async def _produce_summary(
        self,
        prefix: list[LLMMessage],
        previous_summary: str | None,
        instructions: str | None,
        cut_index: int,
        conversation_length: int,
        *,
        use_update: bool,
        summary_override: str | None,
    ) -> tuple[str, LLMUsage | None]:
        if summary_override is not None:
            return summary_override, None

        agent = self._agent
        prompt_text = _build_summary_prompt(
            prefix,
            previous_summary,
            instructions,
            use_legacy=agent.config.compaction.prompt == "legacy",
            use_update=use_update,
        )
        request_messages = [
            LLMMessage(role=Role.system, content=SUMMARIZER_SYSTEM_PROMPT),
            LLMMessage(role=Role.user, content=prompt_text),
        ]

        agent.stats.steps += 1
        summary_result = await agent._chat(
            messages=request_messages,
            include_tools=False,
            cache_plan=CachePlan(enabled=False),
        )
        summary = (summary_result.message.content or "").strip()
        if cut_index == conversation_length:
            summary = self._with_last_user_message(summary, prefix)
        return summary, summary_result.usage
