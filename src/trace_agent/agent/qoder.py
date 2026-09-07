from __future__ import annotations

import os
from pathlib import Path
from time import perf_counter
from typing import Any

from trace_agent.config import LlmRuntimeConfig
from trace_agent.agent.adapters.qoder import client as qoder_client
from trace_agent.agent.adapters.qoder import session as qoder_session
from trace_agent.agent.adapters.qoder import tools as qoder_tools
from trace_agent.errors import (
    AgentFailure,
    ErrorCategory,
    async_retry,
    classify_exception,
)
from trace_agent.models import (
    AnalysisResult,
    AnalyzeRequest,
)
from trace_agent.policies import (
    EvidenceSubmissionPolicy,
    SubmissionRejection,
)
from trace_agent.runtime import RunContext
from trace_agent.skills import SkillDefinition
from trace_agent.tools import ToolDefinition, ToolRegistry
from trace_agent.agent.sdk_guard import require_sdk
from trace_agent.agent.core import (
    agent_tool_payload,
    bounded_error,
    bounded_preview,
    extract_json,
    parse_analysis_result,
    parse_with_submission,
    positive_int_env,
    repair_prompt as build_repair_prompt,
    result_diagnostic,
    select,
    system_prompt as build_system_prompt,
    user_prompt as build_user_prompt,
    write_agent_result,
)


class QoderAgentSdkAgent:
    """Qoder Agent SDK adapter with an in-process, read-only MCP server."""

    def __init__(
        self,
        *,
        skills: list[SkillDefinition],
        runtime_config: LlmRuntimeConfig | None = None,
        memory_context: str | None = None,
    ) -> None:
        if not skills:
            raise ValueError("Qoder Agent 至少需要一个分析 Skill")
        roots = {skill.project_root for skill in skills}
        if len(roots) != 1:
            raise ValueError("一次运行中的 Skill 必须来自同一项目根目录")
        self._skills = list(skills)
        self._project_root = roots.pop()
        self._runtime_config = runtime_config
        self._memory_context = memory_context

    async def analyze(
        self,
        request: AnalyzeRequest,
        tools: ToolRegistry,
        *,
        checkpoint: Any | None = None,
        run_context: RunContext | None = None,
    ) -> AnalysisResult:
        require_sdk("qoder_agent_sdk", extra_label="qoder", provider="qoder")
        from qoder_agent_sdk import (
            HookMatcher,
            QoderAgentOptions,
            QoderSDKClient,
            access_token_from_env,
            create_sdk_mcp_server,
            qodercli_auth,
            tool,
        )

        manual_submission = True
        submission_state: dict[str, Any] = {
            "result": None,
            "errors": [],
        }
        sdk_tools = [
            self._create_sdk_tool(tool, definition, tools)
            for definition in tools.available()
        ]
        if manual_submission:
            sdk_tools.append(
                self._create_submission_tool(
                    tool,
                    submission_state,
                    tools,
                    request,
                )
            )
        allowed_tools = [
            "Read",
            "Skill",
            *(
                f"mcp__trace__{definition.name}"
                for definition in tools.available()
            ),
        ]
        if manual_submission:
            allowed_tools.append("mcp__trace__submit_analysis_result")

        server = create_sdk_mcp_server(
            name="trace",
            version="0.1.0",
            tools=sdk_tools,
        )
        model_resolver = self._create_model_resolver()
        options = QoderAgentOptions(
            model=None if model_resolver is not None else request.model,
            system_prompt=self._system_prompt(
                manual_submission=manual_submission,
            ),
            cwd=self._project_root,
            cli_path=self._resolve_cli_path(),
            env=(
                {
                    "QODER_PERSONAL_ACCESS_TOKEN": qoder_client.personal_access_token()
                }
                if qoder_client.personal_access_token()
                else {}
            ),
            setting_sources=["project"],
            skills=[skill.name for skill in self._skills],
            mcp_servers={"trace": server},
            tools=allowed_tools,
            allowed_tools=allowed_tools,
            disallowed_tools=[
                "Bash",
                "Edit",
                "Glob",
                "Grep",
                "NotebookEdit",
                "Task",
                "WebFetch",
                "WebSearch",
                "Write",
            ],
            hooks={
                "PreToolUse": [
                    HookMatcher(
                        matcher="Read",
                        hooks=[self._create_read_hook()],
                    )
                ]
            },
            max_turns=self._positive_int_env(
                "TRACE_AGENT_MAX_TURNS",
                default=40,
                maximum=100,
            ),
            permission_mode="dontAsk",
            auth=(
                access_token_from_env()
                if qoder_client.personal_access_token()
                else qodercli_auth()
            ),
            resolve_model=model_resolver,
            strict_mcp_config=True,
        )

        text_blocks: list[str] = []
        result_message: Any | None = None
        attempts: list[dict[str, Any]] = []
        retry_attempts = self._positive_int_env(
            "TRACE_AGENT_QODER_RETRY_ATTEMPTS",
            default=3,
            maximum=5,
        )

        async def run_turn(prompt: str) -> tuple[list[str], Any | None]:
            if run_context is not None:
                run_context.raise_if_cancelled()
            await client.query(prompt)
            return await qoder_session.collect_text_blocks(
                client.receive_response(),
                run_context=run_context,
            )

        def record_transport_retry(exc: Exception, attempt: int) -> None:
            attempts.append(
                {
                    "attempt": f"transport-retry-{attempt}",
                    "category": classify_exception(exc).value,
                    "exception_type": type(exc).__name__,
                    "error": self._bounded_preview(str(exc), 4000) or "",
                }
            )

        def fail(
            exc: Exception,
            *,
            session_id: str | None = None,
        ) -> AgentFailure:
            attempts.append(
                {
                    "attempt": "transport-exhausted",
                    "category": classify_exception(exc).value,
                    "exception_type": type(exc).__name__,
                    "error": self._bounded_preview(str(exc), 4000) or "",
                }
            )
            diagnostic_path = self._write_agent_result(
                request,
                status="failed",
                attempts=attempts,
                tools=tools,
            )
            if isinstance(exc, AgentFailure):
                if not exc.diagnostic_paths:
                    exc.diagnostic_paths.append(diagnostic_path)
                if exc.session_id is None:
                    exc.session_id = session_id
                return exc
            return AgentFailure(
                str(exc) or type(exc).__name__,
                category=classify_exception(exc),
                session_id=session_id,
                diagnostic_paths=[diagnostic_path],
            )

        initial_prompt = self._user_prompt(
            request,
            preloaded_evidence=tools.preloaded_results(),
            include_output_schema=manual_submission,
        )
        async with QoderSDKClient(options=options) as client:
            try:
                text_blocks, result_message = await async_retry(
                    lambda: run_turn(initial_prompt),
                    max_attempts=retry_attempts,
                    on_retry=record_transport_retry,
                )
            except Exception as exc:
                failure = fail(
                    exc,
                    session_id=getattr(result_message, "session_id", None),
                )
                raise failure from exc

            parsed, parse_source, parse_errors = self._parse_with_submission(
                submission_state=submission_state,
                result_message=result_message,
                text_blocks=text_blocks,
            )
            attempts.append(
                self._result_diagnostic(
                    attempt="initial",
                    result_message=result_message,
                    text_blocks=text_blocks,
                    parse_source=parse_source,
                    parse_errors=parse_errors,
                )
            )
            if parsed is not None:
                self._write_agent_result(
                    request,
                    status="completed",
                    attempts=attempts,
                    tools=tools,
                )
                return parsed

            can_repair = (
                result_message is None
                or not bool(getattr(result_message, "is_error", False))
            )
            if can_repair and parse_errors:
                tools.seal(
                    "???? JSON ??? AnalysisResult ???"
                    "???????????"
                )
                max_repair_attempts = self._positive_int_env(
                    "TRACE_AGENT_MAX_AGENT_REPAIR_ATTEMPTS",
                    default=3,
                    maximum=5,
                )
                max_repair_tokens = self._positive_int_env(
                    "TRACE_AGENT_MAX_REPAIR_TOKENS",
                    default=40_000,
                    maximum=1_000_000,
                )
                raw_wall_time = os.environ.get(
                    "TRACE_AGENT_MAX_REPAIR_SECONDS",
                    "120",
                )
                try:
                    max_repair_seconds = max(
                        0.0,
                        float(raw_wall_time),
                    )
                except ValueError:
                    max_repair_seconds = 120.0

                repair_started = perf_counter()
                repair_token_count = 0
                repair_attempt = 0

                while parse_errors and repair_attempt < max_repair_attempts:
                    repair_attempt += 1
                    budget = tools.budget_snapshot()
                    if budget.get("remaining_invocations", 0) <= 0:
                        parse_errors.append(
                            "????????????????"
                        )
                        break
                    if (
                        max_repair_seconds > 0
                        and perf_counter() - repair_started
                        >= max_repair_seconds
                    ):
                        parse_errors.append(
                            "?????? wall-time ??"
                        )
                        break

                    repair_prompt = self._repair_prompt(
                        parse_errors,
                        manual_submission=manual_submission,
                    )
                    repair_text_blocks: list[str] = []
                    repair_result: Any | None = None
                    try:
                        repair_text_blocks, repair_result = await async_retry(
                            lambda: run_turn(repair_prompt),
                            max_attempts=retry_attempts,
                            on_retry=record_transport_retry,
                        )
                    except Exception as exc:
                        failure = fail(
                            exc,
                            session_id=getattr(
                                repair_result,
                                "session_id",
                                None,
                            ),
                        )
                        raise failure from exc

                    repair_token_count += sum(
                        len(text) for text in repair_text_blocks
                    )
                    repaired, repair_source, repair_errors = (
                        self._parse_with_submission(
                            submission_state=submission_state,
                            result_message=repair_result,
                            text_blocks=repair_text_blocks,
                        )
                    )
                    attempts.append(
                        self._result_diagnostic(
                            attempt=(
                                f"structured-repair-{repair_attempt}"
                            ),
                            result_message=repair_result,
                            text_blocks=repair_text_blocks,
                            parse_source=repair_source,
                            parse_errors=repair_errors,
                        )
                    )
                    result_message = repair_result
                    parse_errors = repair_errors

                    if repaired is not None:
                        self._write_agent_result(
                            request,
                            status="repaired",
                            attempts=attempts,
                            tools=tools,
                        )
                        return repaired

                    if checkpoint is not None:
                        try:
                            checkpoint(
                                {
                                    "attempt": repair_attempt,
                                    "parse_errors": list(parse_errors),
                                    "session_id": getattr(
                                        repair_result,
                                        "session_id",
                                        None,
                                    ),
                                }
                            )
                        except Exception:
                            pass

                    if repair_token_count >= max_repair_tokens:
                        parse_errors.append(
                            "?????? token ??"
                        )
                        break


        diagnostic_path = self._write_agent_result(
            request,
            status="failed",
            attempts=attempts,
            tools=tools,
        )
        session_id = getattr(result_message, "session_id", None)
        if (
            result_message is not None
            and bool(getattr(result_message, "is_error", False))
        ):
            details = getattr(result_message, "errors", None) or []
            result_text = getattr(result_message, "result", None)
            if not details and result_text:
                details = [result_text]
            detail = "; ".join(str(item) for item in details)
            detail = detail or str(
                getattr(result_message, "subtype", "unknown")
            )
            raise AgentFailure(
                f"Qoder Agent 执行失败：{detail}",
                category=ErrorCategory.USER_RECOVERABLE,
                session_id=session_id,
                diagnostic_paths=[diagnostic_path],
            )
        detail = "；".join(parse_errors[-3:]) or "没有可解析的最终 JSON"
        raise AgentFailure(
            (
                "Qoder Agent 最终结构化结果不符合 AnalysisResult："
                f"{detail}"
            ),
            category=ErrorCategory.USER_RECOVERABLE,
            session_id=session_id,
            diagnostic_paths=[diagnostic_path],
        )

    @staticmethod
    def _resolve_cli_path() -> Path | None:
        return qoder_client.resolve_cli_path()

    def _create_model_resolver(self) -> Any | None:
        return qoder_client.build_model_resolver(self._runtime_config)

    @staticmethod
    def _positive_int_env(
        name: str,
        *,
        default: int,
        maximum: int,
    ) -> int:
        return positive_int_env(name, default=default, maximum=maximum)


    @staticmethod
    def _tool_result(payload: dict[str, Any]) -> dict[str, Any]:
        return qoder_tools.tool_result(payload)

    @classmethod
    def _create_sdk_tool(
        cls,
        tool_decorator: Any,
        definition: ToolDefinition,
        registry: ToolRegistry,
    ) -> Any:
        del cls
        return qoder_tools.create_sdk_tool(
            tool_decorator,
            definition,
            registry,
        )

    @classmethod
    def _agent_tool_payload(
        cls,
        tool_name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return agent_tool_payload(tool_name, payload)


    @classmethod
    def _compact_completion_phases(
        cls,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        from trace_agent.agent.core.payload import compact_completion_phases
        return compact_completion_phases(data)


    @classmethod
    def _compact_perf_profile(
        cls,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        from trace_agent.agent.core.payload import compact_perf_profile
        return compact_perf_profile(data)


    @staticmethod
    def _select(source: dict[str, Any], *keys: str) -> dict[str, Any]:
        return select(source, *keys)


    @classmethod
    def _create_submission_tool(
        cls,
        tool_decorator: Any,
        state: dict[str, Any],
        registry: ToolRegistry,
        request: AnalyzeRequest,
    ) -> Any:
        del cls
        return qoder_tools.create_submission_tool(
            tool_decorator,
            state,
            registry,
            request,
        )

    @classmethod
    def _submission_rejection(
        cls,
        state: dict[str, Any],
        rejection: SubmissionRejection,
    ) -> dict[str, Any]:
        del cls
        return qoder_tools.submission_rejection(state, rejection)

    def _create_read_hook(self) -> Any:
        return qoder_tools.create_read_hook(
            self._project_root,
            self._skills,
        )

    def _is_allowed_skill_file(self, raw_path: Any) -> bool:
        return qoder_tools.is_allowed_skill_file(
            self._project_root,
            self._skills,
            raw_path,
        )

    def _system_prompt(
        self,
        *,
        manual_submission: bool = False,
    ) -> str:
        return build_system_prompt(
            self._skills,
            memory_context=self._memory_context,
            manual_submission=manual_submission,
        )


    @staticmethod
    def _user_prompt(
        request: AnalyzeRequest,
        *,
        preloaded_evidence: list[dict[str, Any]] | None = None,
        include_output_schema: bool = False,
    ) -> str:
        return build_user_prompt(
            request,
            preloaded_evidence=preloaded_evidence,
            include_output_schema=include_output_schema,
        )


    @staticmethod
    def _extract_json(text_blocks: list[str]) -> dict[str, Any]:
        return extract_json(text_blocks)


    @classmethod
    def _parse_analysis_result(
        cls,
        result_message: Any | None,
        text_blocks: list[str],
    ) -> tuple[AnalysisResult | None, str | None, list[str]]:
        return parse_analysis_result(result_message, text_blocks)


    @classmethod
    def _parse_with_submission(
        cls,
        *,
        submission_state: dict[str, Any],
        result_message: Any | None,
        text_blocks: list[str],
    ) -> tuple[AnalysisResult | None, str | None, list[str]]:
        return parse_with_submission(
            submission_state=submission_state,
            result_message=result_message,
            text_blocks=text_blocks,
        )


    @staticmethod
    def _repair_prompt(
        parse_errors: list[str],
        *,
        manual_submission: bool = False,
    ) -> str:
        return build_repair_prompt(
            parse_errors,
            manual_submission=manual_submission,
        )


    @classmethod
    def _result_diagnostic(
        cls,
        *,
        attempt: str,
        result_message: Any | None,
        text_blocks: list[str],
        parse_source: str | None,
        parse_errors: list[str],
    ) -> dict[str, Any]:
        return result_diagnostic(
            attempt=attempt,
            result_message=result_message,
            text_blocks=text_blocks,
            parse_source=parse_source,
            parse_errors=parse_errors,
        )


    @staticmethod
    def _write_agent_result(
        request: AnalyzeRequest,
        *,
        status: str,
        attempts: list[dict[str, Any]],
        tools: ToolRegistry,
    ) -> Path:
        return write_agent_result(
            request,
            status=status,
            attempts=attempts,
            tools=tools,
        )


    @staticmethod
    def _bounded_error(error: ValueError) -> str:
        return bounded_error(error)


    @staticmethod
    def _bounded_preview(value: Any, limit: int) -> str | None:
        return bounded_preview(value, limit)

