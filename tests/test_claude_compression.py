"""SDK contract and cleanup tests for Claude compression."""

import json
import os
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path

import anyio
import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKError, CLIConnectionError, ResultMessage, ThinkingBlock
from claude_agent_sdk.types import Message
from fastmcp.exceptions import ToolError
from pydantic import AnyHttpUrl, JsonValue, SecretStr

from marabrain.compression import compress_memory
from marabrain.llm_config import LLMProvider, LLMSettings


def claude_result(output: JsonValue, subtype: str = "success", error: bool = False) -> ResultMessage:
    """Construct the SDK's terminal result without fabricating assistant text parsing."""
    return ResultMessage(subtype, 1, 1, error, 2, "ephemeral", result="private reasoning", structured_output=output)


@dataclass(slots=True)
class ClaudeScript:
    """Mutable SDK script recording options, exhaustion, and asynchronous cleanup."""

    messages: list[Message] = field(default_factory=list[Message])
    options: ClaudeAgentOptions | None = None
    prompt: str = ""
    closed: bool = False
    exhausted: bool = False
    wait: bool = False
    error: ClaudeSDKError | None = None


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch) -> ClaudeScript:
    """Script SDK messages because a fake HTTP server cannot model the agent protocol."""
    state = ClaudeScript(messages=[AssistantMessage([ThinkingBlock("secret", "signature")], "model"), claude_result({"context": "selected context"})])

    class ScriptedClient:
        """Expose the SDK connection lifecycle for failure and cleanup scenarios."""

        def __init__(self, *, options: ClaudeAgentOptions) -> None:
            """Record options before the connection opens."""
            state.options = options
            assert options.cwd is not None and Path(options.cwd).is_dir()

        async def connect(self) -> None:
            """Open the scripted SDK connection."""

        async def query(self, prompt: str) -> None:
            """Record caller data separately from system instructions."""
            state.prompt = prompt

        async def receive_response(self) -> AsyncGenerator[Message, None]:
            """Yield SDK messages until completion, error, or cancellation."""
            for message in state.messages:
                yield message
            if state.error is not None:
                raise state.error
            if state.wait:
                await anyio.sleep_forever()
            state.exhausted = True

        async def disconnect(self) -> None:
            """Exercise awaited cleanup even after cancellation."""
            await anyio.lowlevel.checkpoint()
            state.closed = True

    monkeypatch.setattr("marabrain.compression.ClaudeSDKClient", ScriptedClient)
    return state


@pytest.fixture
def claude_settings() -> LLMSettings:
    """Provide explicit Claude credentials and gateway settings."""
    return LLMSettings(provider=LLMProvider.ANTHROPIC, model="model", provider_url=AnyHttpUrl("http://localhost:1234"), api_key=SecretStr("configured-key"))


@pytest.mark.parametrize("effort", [None, "high"])
async def test_claude_sdk_contract(claude: ClaudeScript, monkeypatch: pytest.MonkeyPatch, effort: str | None) -> None:
    """Isolate agent options and return only validated structured context after exhaustion."""
    ambient = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_EFFORT_LEVEL", "CLAUDE_CONFIG_DIR")
    for name in ambient:
        monkeypatch.setenv(name, "ambient-secret")
    settings = LLMSettings.model_validate(
        {
            "provider": LLMProvider.ANTHROPIC,
            "model": "model",
            "provider_url": "http://localhost:1234",
            "api_key": "configured-key",
            "reasoning_effort": effort,
            "max_tokens": 1234,
            "max_input_tokens": 128000,
        }
    )
    assert await compress_memory(settings, "q", "full memory") == "selected context"
    options = claude.options
    assert options is not None and options.cwd is not None
    assert json.loads(claude.prompt) == {"query": "q"}
    assert isinstance(options.system_prompt, str) and options.system_prompt.endswith(json.dumps("full memory"))
    assert options.model == "model" and options.effort == effort
    assert options.thinking == ({"type": "adaptive"} if effort else None)
    assert options.tools == [] and options.mcp_servers == {} and options.strict_mcp_config and options.setting_sources == []
    assert options.disallowed_tools == [] and options.max_turns is not None and 2 <= options.max_turns <= 5
    assert options.extra_args == {"no-session-persistence": None} and options.resume is None and not options.continue_conversation
    assert options.env["ANTHROPIC_API_KEY"] == "configured-key" and options.env["ANTHROPIC_BASE_URL"] == "http://localhost:1234"
    assert options.env["CLAUDE_CONFIG_DIR"] == str(options.cwd)
    assert options.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "1234" and options.env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "128000"
    assert all(options.env[name] == "" for name in ambient if name not in ("ANTHROPIC_BASE_URL", "CLAUDE_CONFIG_DIR"))
    assert os.environ["ANTHROPIC_AUTH_TOKEN"] == "ambient-secret"
    assert options.output_format is not None and options.output_format["type"] == "json_schema"
    assert options.output_format["schema"]["required"] == ["context"]
    assert claude.closed and claude.exhausted and not Path(options.cwd).exists()


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [claude_result(None)],
        [claude_result({})],
        [claude_result({"context": " "})],
        [claude_result({"context": 3})],
        [claude_result({"context": "partial"}, "error_max_turns")],
        [claude_result({"context": "refused"}, error=True)],
        [claude_result({"context": "one"}), claude_result({"context": "two"})],
    ],
)
async def test_invalid_claude_results_are_masked(claude: ClaudeScript, claude_settings: LLMSettings, messages: list[Message]) -> None:
    """Reject missing, malformed, failed, or duplicate terminal results without text fallback."""
    claude.messages = messages
    with pytest.raises(ToolError, match="^LLM compression failed\\.$"):
        await compress_memory(claude_settings, "query", "content")
    assert claude.closed and claude.options is not None and claude.options.cwd is not None
    assert not Path(claude.options.cwd).exists()


@pytest.mark.parametrize("failure", ["timeout", "after_result_timeout", "sdk_error"])
async def test_claude_failure_cleanup(claude: ClaudeScript, claude_settings: LLMSettings, failure: str) -> None:
    """Enforce the deadline through iterator exhaustion and clean up on SDK failure or timeout."""
    claude.messages = [claude_result({"context": "premature"})] if failure == "after_result_timeout" else []
    claude.wait = failure != "sdk_error"
    claude.error = CLIConnectionError("secret SDK diagnostic") if failure == "sdk_error" else None
    with pytest.raises(ToolError, match="^LLM compression failed\\.$"):
        await compress_memory(claude_settings.model_copy(update={"timeout_seconds": 0.05}), "query", "content")
    assert claude.closed and not claude.exhausted and claude.options is not None and claude.options.cwd is not None
    assert not Path(claude.options.cwd).exists()
