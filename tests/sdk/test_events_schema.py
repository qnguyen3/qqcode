from __future__ import annotations

from vibe.sdk import (
    EVENT_SCHEMA_VERSION,
    SDK_EVENT_TYPES,
    ErrorStreamEvent,
    MessageDeltaEvent,
    PlanApprovalRequiredEvent,
    SessionCompactedEvent,
    SessionStartedEvent,
    ThinkingUpdatedEvent,
    ToolApprovalRequiredEvent,
    ToolCallStreamEvent,
    ToolResultStreamEvent,
    TurnCompletedEvent,
    TurnStartedEvent,
    TurnUsage,
    __api_version__,
    parse_sdk_event,
    sdk_event_json_schema,
)


def test_api_and_schema_versions_are_stable() -> None:
    assert __api_version__ == 1
    assert EVENT_SCHEMA_VERSION == 1


def test_all_event_types_roundtrip_through_discriminated_union() -> None:
    samples = [
        SessionStartedEvent(session_id="s1"),
        TurnStartedEvent(turn=1),
        MessageDeltaEvent(item_id="msg_1", delta="hi", content="hi"),
        ThinkingUpdatedEvent(item_id="msg_1", delta="hm", content="hm"),
        ToolCallStreamEvent(
            tool_call_id="tc_1", tool_name="grep", args={"pattern": "foo"}
        ),
        ToolResultStreamEvent(
            tool_call_id="tc_1", tool_name="grep", status="ok", text="{}"
        ),
        TurnCompletedEvent(usage=TurnUsage(input=10, output=5), cost_usd=0.001),
        SessionCompactedEvent(tokens_before=200, tokens_after=50),
        ToolApprovalRequiredEvent(
            tool_call_id="tc_2", tool_name="bash", args={"command": "ls"}
        ),
        PlanApprovalRequiredEvent(plan="step 1"),
        ErrorStreamEvent(message="boom"),
    ]

    seen_types = {sample.type for sample in samples}
    assert seen_types == set(SDK_EVENT_TYPES)

    for sample in samples:
        wire = sample.to_wire_dict()
        assert wire["schema"] == 1
        assert wire["type"] == sample.type
        parsed = parse_sdk_event(wire)
        assert parsed == sample
        assert parsed.schema_version == 1


def test_json_schema_contains_discriminator_and_all_variants() -> None:
    schema = sdk_event_json_schema()
    assert schema == sdk_event_json_schema()
    assert "discriminator" in schema
    assert schema["discriminator"]["propertyName"] == "type"
    mapping = schema["discriminator"].get("mapping", {})
    assert set(mapping.keys()) == set(SDK_EVENT_TYPES)
