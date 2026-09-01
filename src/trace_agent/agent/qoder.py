from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from trace_agent.policies import (
    EvidenceSubmissionPolicy,
    SubmissionRejection,
)
from trace_agent.config import LlmRuntimeConfig, is_native_byok_provider
from trace_agent.models import (
    AgentAnalysisDraft,
    AnalysisResult,
    AnalyzeRequest,
)
from trace_agent.skills import SkillDefinition
from trace_agent.tools import ToolDefinition, ToolRegistry


class QoderAgentSdkAgent:
    """Qoder Agent SDK adapter with an in-process, read-only MCP server."""

    def __init__(
        self,
        *,
        skills: list[SkillDefinition],
        runtime_config: LlmRuntimeConfig | None = None,
    ) -> None:
        if not skills:
            raise ValueError("Qoder Agent 至少需要一个分析 Skill")
        roots = {skill.project_root for skill in skills}
        if len(roots) != 1:
            raise ValueError("一次运行中的 Skill 必须来自同一项目根目录")
        self._skills = list(skills)
        self._project_root = roots.pop()
        self._runtime_config = runtime_config

    async def analyze(
        self,
        request: AnalyzeRequest,
        tools: ToolRegistry,
    ) -> AnalysisResult:
        try:
            from qoder_agent_sdk import (
                AssistantMessage,
                HookMatcher,
                QoderAgentOptions,
                QoderSDKClient,
                ResultMessage,
                TextBlock,
                access_token_from_env,
                create_sdk_mcp_server,
                qodercli_auth,
                tool,
            )
        except ImportError as exc:
            raise RuntimeError(
                "Qoder Agent SDK 未安装，请执行：uv sync --extra qoder"
            ) from exc

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
                dict(self._runtime_config.sdk_environment)
                if self._runtime_config is not None
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
                if self._runtime_config is not None
                and self._runtime_config.use_qoder_personal_access_token
                else qodercli_auth()
            ),
            resolve_model=model_resolver,
            strict_mcp_config=True,
        )

        text_blocks: list[str] = []
        result_message: Any | None = None
        attempts: list[dict[str, Any]] = []
        async with QoderSDKClient(options=options) as client:
            await client.query(
                self._user_prompt(
                    request,
                    preloaded_evidence=tools.preloaded_results(),
                    include_output_schema=manual_submission,
                )
            )
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    text_blocks.extend(
                        block.text
                        for block in message.content
                        if isinstance(block, TextBlock)
                    )
                elif isinstance(message, ResultMessage):
                    result_message = message
                    if message.result:
                        text_blocks.append(message.result)

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
            if can_repair:
                tools.seal(
                    "初次最终 JSON 未通过 AnalysisResult 校验，"
                    "正在执行无工具修复回合"
                )
                repair_text_blocks: list[str] = []
                repair_result: Any | None = None
                await client.query(
                    self._repair_prompt(
                        parse_errors,
                        manual_submission=manual_submission,
                    )
                )
                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        repair_text_blocks.extend(
                            block.text
                            for block in message.content
                            if isinstance(block, TextBlock)
                        )
                    elif isinstance(message, ResultMessage):
                        repair_result = message
                        if message.result:
                            repair_text_blocks.append(message.result)

                repaired, repair_source, repair_errors = (
                    self._parse_with_submission(
                        submission_state=submission_state,
                        result_message=repair_result,
                        text_blocks=repair_text_blocks,
                    )
                )
                attempts.append(
                    self._result_diagnostic(
                        attempt="structured-repair",
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

        diagnostic_path = self._write_agent_result(
            request,
            status="failed",
            attempts=attempts,
            tools=tools,
        )
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
            raise RuntimeError(
                "Qoder Agent 执行失败："
                f"{detail}；session="
                f"{getattr(result_message, 'session_id', 'unknown')}；"
                f"诊断={diagnostic_path}"
            )
        detail = "；".join(parse_errors[-3:]) or "没有可解析的最终 JSON"
        raise RuntimeError(
            "Qoder Agent 最终结构化结果不符合 AnalysisResult："
            f"{detail}；诊断={diagnostic_path}"
        )

    @staticmethod
    def _resolve_cli_path() -> Path | None:
        """Honor an explicit Qoder CLI binary; otherwise use SDK bundled CLI."""
        explicit = os.environ.get("QODER_CLI_EXECUTABLE")
        if explicit:
            path = Path(explicit).expanduser()
            if not path.is_file():
                raise RuntimeError(
                    "QODER_CLI_EXECUTABLE 指向的文件不存在："
                    f"{path}"
                )
            return path.resolve()

        return None

    def _create_model_resolver(self) -> Any | None:
        """Route every Qoder LLM turn through the configured BYOK provider."""
        if self._runtime_config is None:
            return None

        custom_model = {
            "provider": self._runtime_config.provider.value,
            "model": self._runtime_config.model,
            "api_key": self._runtime_config.api_key,
        }
        if not is_native_byok_provider(self._runtime_config.provider):
            custom_model["url"] = self._runtime_config.base_url

        def resolve_model(context: dict[str, Any]) -> dict[str, Any]:
            del context
            return {"model": dict(custom_model)}

        return resolve_model

    @staticmethod
    def _positive_int_env(
        name: str,
        *,
        default: int,
        maximum: int,
    ) -> int:
        raw = os.environ.get(name)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} 必须是整数") from exc
        if not 1 <= value <= maximum:
            raise ValueError(f"{name} 必须在 1 到 {maximum} 之间")
        return value

    @staticmethod
    def _tool_result(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(payload, ensure_ascii=False),
                }
            ]
        }

    @classmethod
    def _create_sdk_tool(
        cls,
        tool_decorator: Any,
        definition: ToolDefinition,
        registry: ToolRegistry,
    ) -> Any:
        @tool_decorator(
            definition.name,
            definition.description,
            definition.input_schema,
        )
        async def invoke(arguments: dict[str, Any]) -> dict[str, Any]:
            payload = registry.invoke(definition.name, arguments)
            return cls._tool_result(
                cls._agent_tool_payload(definition.name, payload)
            )

        return invoke

    @classmethod
    def _agent_tool_payload(
        cls,
        tool_name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Keep full Evidence on disk while bounding model context growth."""

        data = payload.get("data")
        if not isinstance(data, dict):
            return payload
        if tool_name == "inspect_completion_latency_phases":
            compact_data = cls._compact_completion_phases(data)
        elif tool_name == "inspect_perf_profile":
            compact_data = cls._compact_perf_profile(data)
        else:
            return payload
        return {
            "evidence_id": payload.get("evidence_id"),
            "summary": payload.get("summary"),
            "data": compact_data,
            "full_evidence_persisted": True,
            "agent_view_complete_for_decision": True,
            "deterministic_hydration": (
                "Do not copy thread/perf transport data into the final Draft; "
                "the application layer hydrates it from this Evidence."
            ),
        }

    @classmethod
    def _compact_completion_phases(
        cls,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        phases: list[dict[str, Any]] = []
        for raw_phase in data.get("phases") or []:
            if not isinstance(raw_phase, dict):
                continue
            profiles: list[dict[str, Any]] = []
            for wrapper in raw_phase.get("thread_profiles") or []:
                if not isinstance(wrapper, dict):
                    continue
                thread = wrapper.get("thread_execution")
                if not isinstance(thread, dict):
                    continue
                compact_thread = cls._select(
                    thread,
                    "process_name",
                    "thread_name",
                    "pid",
                    "ipid",
                    "tid",
                    "itid",
                    "state_breakdown",
                    "cpu_distribution",
                    "cpu_migrations",
                    "schedule_slices",
                    "longest_running_ms",
                    "longest_runnable_ms",
                    "longest_sleep_ms",
                    "priority",
                    "diagnosis",
                    "assessment",
                    "confidence",
                )
                compact_thread["contention_intervals"] = list(
                    thread.get("contention_intervals") or []
                )[:3]
                compact_thread["wakeup_chain"] = list(
                    thread.get("wakeup_chain") or []
                )[:3]
                profiles.append(
                    {
                        **cls._select(
                            wrapper,
                            "absolute_active_ms",
                            "absolute_running_ms",
                            "selection_reasons",
                            "limitations",
                        ),
                        "thread_execution": compact_thread,
                    }
                )
            frames = raw_phase.get("frames")
            compact_frames = dict(frames) if isinstance(frames, dict) else {}
            for key in ("long_frames", "mapped_presentations"):
                compact_frames[key] = list(compact_frames.get(key) or [])[:10]
            phases.append(
                {
                    **cls._select(
                        raw_phase,
                        "name",
                        "start_ns",
                        "end_ns",
                        "duration_ms",
                        "thread_rankings",
                        "recommended_perf_thread_ids",
                    ),
                    "thread_profiles": profiles,
                    "slice_hotspots": list(
                        raw_phase.get("slice_hotspots") or []
                    )[:10],
                    "frames": compact_frames,
                }
            )
        return {
            **cls._select(
                data,
                "target_process",
                "main_thread",
                "render_threads",
                "input_ns",
                "response_ns",
                "completion_ns",
                "recommended_perf_scope",
                "selection_policy",
                "limitations",
            ),
            "phases": phases,
        }

    @classmethod
    def _compact_perf_profile(
        cls,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        for raw_event in data.get("event_profiles") or []:
            if not isinstance(raw_event, dict):
                continue
            events.append(
                {
                    **cls._select(
                        raw_event,
                        "event_type_id",
                        "event_name",
                        "sample_count",
                        "total_event_count",
                        "application_hotspot_status",
                        "bottom_up_semantics",
                    ),
                    "threads": list(raw_event.get("threads") or [])[:10],
                    "cpus": list(raw_event.get("cpus") or [])[:16],
                    "hotspots": list(raw_event.get("hotspots") or [])[:12],
                    "application_modules": list(
                        raw_event.get("application_modules") or []
                    )[:10],
                    "bottom_up_diagnostics": list(
                        raw_event.get("bottom_up_diagnostics") or []
                    )[:8],
                    "context_hotspots": list(
                        raw_event.get("context_hotspots") or []
                    )[:5],
                }
            )
        return {
            **cls._select(
                data,
                "interval_start_ns",
                "interval_end_ns",
                "collection",
                "requested_process_ids",
                "requested_thread_ids",
                "observed_process_ids",
                "observed_thread_ids",
                "sample_count",
                "total_callchain_frames",
                "symbolized_callchain_frames",
                "symbolization_rate",
                "limitations",
            ),
            "event_profiles": events,
        }

    @staticmethod
    def _select(source: dict[str, Any], *keys: str) -> dict[str, Any]:
        return {key: source[key] for key in keys if key in source}

    @classmethod
    def _create_submission_tool(
        cls,
        tool_decorator: Any,
        state: dict[str, Any],
        registry: ToolRegistry,
        request: AnalyzeRequest,
    ) -> Any:
        @tool_decorator(
            "submit_analysis_result",
            (
                "提交最终 AgentAnalysisDraft。完成取证后必须调用一次；analysis "
                "只传语义 JSON，不含 perf 和 critical_threads。工具会执行严格 "
                "Schema 校验；若返回 "
                "accepted=false，按 validation_error 修正后再次提交。"
            ),
            {"analysis": dict},
        )
        async def submit(arguments: dict[str, Any]) -> dict[str, Any]:
            policy = EvidenceSubmissionPolicy(
                request=request,
                registry=registry,
            )
            rejection = policy.validate_collected_evidence()
            if rejection is not None:
                return cls._submission_rejection(state, rejection)
            raw_analysis = arguments.get("analysis")
            try:
                draft = AgentAnalysisDraft.model_validate(raw_analysis)
                parsed = draft.to_analysis_result()
            except ValueError as exc:
                error = cls._bounded_error(exc)
                state.setdefault("errors", []).append(
                    "submit_analysis_result: " + error
                )
                return cls._tool_result(
                    {
                        "accepted": False,
                        "validation_error": error,
                        "instruction": (
                            "不得继续取证；仅修正字段并再次提交。"
                        ),
                    }
                )

            rejection = policy.validate_analysis(parsed)
            if rejection is not None:
                return cls._submission_rejection(state, rejection)

            state["result"] = parsed
            registry.seal("最终 AnalysisResult 已通过 Schema 校验")
            return cls._tool_result(
                {
                    "accepted": True,
                    "instruction": "结果已接收，不得再调用任何工具。",
                }
            )

        return submit

    @classmethod
    def _submission_rejection(
        cls,
        state: dict[str, Any],
        rejection: SubmissionRejection,
    ) -> dict[str, Any]:
        state.setdefault("errors", []).append(
            "submit_analysis_result: " + rejection.error
        )
        return cls._tool_result(
            {
                "accepted": False,
                "validation_error": rejection.error,
                "instruction": rejection.instruction,
            }
        )

    def _create_read_hook(self) -> Any:
        async def validate_read(
            hook_input: dict[str, Any],
            tool_use_id: str | None,
            context: dict[str, Any],
        ) -> dict[str, Any]:
            del tool_use_id, context
            tool_input = hook_input.get("tool_input")
            raw_path = (
                tool_input.get("file_path")
                if isinstance(tool_input, dict)
                else None
            )
            allowed = self._is_allowed_skill_file(raw_path)
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": (
                        "allow" if allowed else "deny"
                    ),
                    "permissionDecisionReason": (
                        "允许读取本次启用的 Skill 文件"
                        if allowed
                        else "Read 只能访问本次启用的 Skill 目录"
                    ),
                }
            }

        return validate_read

    def _is_allowed_skill_file(self, raw_path: Any) -> bool:
        if not isinstance(raw_path, str) or not raw_path.strip():
            return False

        requested = Path(raw_path)
        if not requested.is_absolute():
            requested = self._project_root.resolve() / requested
        try:
            resolved = requested.resolve(strict=True)
        except OSError:
            return False

        return resolved.is_file() and any(
            resolved.is_relative_to(skill.directory.resolve())
            for skill in self._skills
        )

    def _system_prompt(
        self,
        *,
        manual_submission: bool = False,
    ) -> str:
        enabled_skills = "、".join(
            skill.name for skill in self._skills
        )
        skill_calls = "、".join(
            f"Skill({skill.name})" for skill in self._skills
        )
        final_instruction = (
            "最终必须调用 submit_analysis_result 工具提交精简的 analysis Draft。"
            "Draft 禁止包含 perf、critical_threads、线程状态、CPU 分布、唤醒链"
            "等确定性大对象；应用层会从 Evidence 自动合并这些字段。"
            "若工具返回 accepted=false，只修正 validation_error 指出的字段并"
            "再次提交；accepted=true 后停止，不要在正文重复 JSON。"
            if manual_submission
            else (
                "最终只输出符合 SDK output_format 约束的精简 Draft JSON，"
                "不要输出 Markdown。"
            )
        )
        return (
            "你是只读性能 Trace 分析 Agent。"
            f"本次启用的 Skills 为：{enabled_skills}。"
            "在调用任何 mcp__trace 工具之前，必须先通过 Skill 工具"
            f"逐一加载：{skill_calls}。不得跳过或用已有知识代替。"
            "必须遵守所有已启用 Skill；若启用了 perf-sample-analysis，"
            "必须将有效 Perf 数据与同一问题窗口内的 Trace 关键路径联合分析。"
            "只能使用提供的 Trace 工具获取 Trace 事实。"
            "Skill 引用资料可通过 Read 读取，但不得读取本次启用的 Skill 目录之外的文件。"
            "SQL 只能通过 query_trace_sql 执行；不得访问 Shell、网络或其他数据源。"
            "本次分析必须收敛：普通 Trace 工具调用不超过 14 次，其中 "
            "query_trace_sql 不超过 6 次。严禁调用与当前 scenario_type "
            "不匹配的其他场景候选工具。若 Trace 有 perf-samples，"
            "inspect_perf_profile 另有独立保留预算且必须在最终提交前调用。"
            "模型不得在最终 Draft 中复制 critical_threads；若场景分析需要线程"
            "事实，应读取精确区间的确定性工具结果，最终由应用层自动回填。"
            "应用层会在 Agent 启动前固化执行 Trace 概览和场景候选发现，"
            "并通过 preloaded_evidence 提供结果；不得重复相同参数的调用。"
            "completion-latency 场景必须使用预载的首次 "
            "inspect_completion_latency_candidates，并针对最终应用的非零 "
            "target_ipid 只完成一次补充取证；若 preloaded_evidence 含 "
            "explicit_time_range，补充调用必须原样传递 interval_start_ns 和 "
            "interval_end_ns，最终 input/completion 及 problem_interval 必须严格"
            "等于显式区间，不得改用 Trace 最后输入点。帧静止、动画结束和窗口末帧只可发现"
            "候选，没有业务语义证据时必须令 completion_proven=false。"
            "若用户提供 problem_duration_ms 且没有更强显式边界，在确认单次操作"
            "假设后，必须优先使用工具返回的 last-input-plus-user-duration 作为"
            "用户定义完成区间，并在 completion_semantics 中写明该假设。"
            "确定 input/response/completion 后，若 Trace 具备调度数据，必须调用 "
            "inspect_completion_latency_phases，固定使用 max_threads_per_phase=4、"
            "max_slices_per_phase=10、max_frames_per_phase=10；它只做精确阶段取证，"
            "正常只调用一次；若最终提交因用户定义边界不一致被拒绝，必须按提示"
            "使用纠正后的最终边界再调用一次，不得用其输出反向"
            "篡改业务边界。优先采用绝对 Running 贡献，忽略 interval_wrapper_candidate "
            "作为最终根因，并用 recommended_perf_scope.thread_ids 限定 Perf。"
            "completion phase 的线程事实直接使用 phase 工具返回值进行判断；"
            "phase 已返回的线程禁止再次调用 inspect_thread_execution。"
            "frame-jank 场景在确认目标应用 ipid 和问题区间后，必须调用一次 "
            "inspect_frame_jank：首次 frame_producer_itid=0，refresh_rate_hz 使用"
            "用户值或传 0；若工具返回多个实际产帧管线且自动选择不可靠，最多"
            "再按明确候选 itid 调用一次。不得自行用 frame_slice 行数除以整窗"
            "时长重算 FPS，不得默认 60Hz，不得默认主线程就是 UI 线程。"
            "根因分析只跟随 selected-application-frame-producer 和 frame_maps "
            "关联的目标 RenderService 线程；禁止把整个 render_service 进程的"
            "负载归因给目标应用。observable_delay_stage 只表示延迟出现在哪个"
            "流水线阶段，必须结合最差帧/卡顿簇精确窗口的调度、Slice、唤醒链"
            "以及可用 Perf 才能形成根因与优化建议。有效 FPS 不具备 meaningful "
            "条件时必须明确 unavailable，不得把静止区间报告成低 FPS。"
            "Perf 结论必须下钻到应用 HAP/HSP/ABC/AOT 或应用自带 SO；appspawn、"
            "libbegetutil、加载器、MainThread::Start、EventRunner::Run 等公共根节点"
            "只能作为调用上下文，不得作为应用根因或优化对象。"
            "Bottom-up 只允许作为内部辅助证据：不得输出独立 Bottom-up 榜单，"
            "不得把 app.hap+offsets、libfoo.so+offsets 或通用运行时叶子当作结论；"
            "仅当其定位到具体应用函数或明确操作，并被 Top-down Trace 关键阶段"
            "验证时，才可合并进根因和建议。"
            "优先用 CTE、条件聚合和 UNION ALL "
            "在一次 SQL 中比较多个阶段；不得用多次查询试探同一表字段。"
            "工具预算由运行时硬限制；应在第 12 次调用前结束取证并预留最终"
            "结构化输出。若收到预算耗尽错误，不得换工具重试，必须立即基于"
            "已有 Evidence 提交最终结果。"
            "在得到可靠边界、主导阶段、关键线程状态和根因证据后立即形成最终结果，"
            "不要为了填满可选字段继续扩展取证。缺少精确分阶段数据时留空并写入 limitations。"
            + final_instruction
        )

    @staticmethod
    def _user_prompt(
        request: AnalyzeRequest,
        *,
        preloaded_evidence: list[dict[str, Any]] | None = None,
        include_output_schema: bool = False,
    ) -> str:
        context = {
            "trace_id": request.trace_id,
            "scenario_type": request.scenario_type,
            "scenario": request.scenario,
            "symptom": request.symptom,
            "device": request.device,
            "build": request.build,
            "time_range": request.time_range,
            "target_process": request.target_process,
            "operation_marker": request.operation_marker,
            "start_marker": request.start_marker,
            "end_marker": request.end_marker,
            "response_marker": request.response_marker,
            "completion_marker": request.completion_marker,
            "problem_duration_ms": request.problem_duration_ms,
            "refresh_rate_hz": request.refresh_rate_hz,
            "has_baseline": request.baseline_trace_path is not None,
        }
        output_contract = ""
        if include_output_schema:
            output_contract = (
                "\nsubmit_analysis_result.analysis 必须符合以下精简 Draft JSON Schema："
                + json.dumps(
                    AgentAnalysisDraft.model_json_schema(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        return (
            "先调用所有已启用的 Skill，再使用 Trace 工具分析以下任务。"
            "应用层已经执行了不需要判断的确定性预分析；"
            "以下 preloaded_evidence 与工具返回值具有相同证据效力，"
            "不要重复调用其中同参数的发现工具。"
            f"\n任务上下文：{json.dumps(context, ensure_ascii=False)}"
            "\npreloaded_evidence："
            + json.dumps(
                preloaded_evidence or [],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + output_contract
        )

    @staticmethod
    def _extract_json(text_blocks: list[str]) -> dict[str, Any]:
        decoder = json.JSONDecoder()
        for text in reversed(text_blocks):
            candidate = text.strip()
            if candidate.startswith("```"):
                candidate = candidate.removeprefix("```json")
                candidate = candidate.removeprefix("```")
                candidate = candidate.removesuffix("```").strip()
            try:
                value = json.loads(candidate)
                if isinstance(value, dict):
                    return value
            except json.JSONDecodeError:
                pass

            for index, char in enumerate(candidate):
                if char != "{":
                    continue
                try:
                    value, _ = decoder.raw_decode(candidate[index:])
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    return value

        raise ValueError("Qoder Agent 没有返回可解析的结构化 JSON")

    @classmethod
    def _parse_analysis_result(
        cls,
        result_message: Any | None,
        text_blocks: list[str],
    ) -> tuple[AnalysisResult | None, str | None, list[str]]:
        errors: list[str] = []
        structured = (
            getattr(result_message, "structured_output", None)
            if result_message is not None
            else None
        )
        if isinstance(structured, AnalysisResult):
            return structured, "result_message.structured_output", errors
        if isinstance(structured, AgentAnalysisDraft):
            return (
                structured.to_analysis_result(),
                "result_message.structured_output",
                errors,
            )
        if isinstance(structured, dict):
            try:
                return (
                    AgentAnalysisDraft.model_validate(
                        structured
                    ).to_analysis_result(),
                    "result_message.structured_output",
                    errors,
                )
            except ValueError as exc:
                errors.append(
                    "structured_output(dict): "
                    + cls._bounded_error(exc)
                )
        elif isinstance(structured, str):
            try:
                return (
                    AgentAnalysisDraft.model_validate_json(
                        structured
                    ).to_analysis_result(),
                    "result_message.structured_output_json",
                    errors,
                )
            except ValueError as exc:
                errors.append(
                    "structured_output(str): "
                    + cls._bounded_error(exc)
                )

        for index, text in enumerate(reversed(text_blocks)):
            try:
                candidate = cls._extract_json([text])
            except ValueError:
                continue
            try:
                return (
                    AgentAnalysisDraft.model_validate(
                        candidate
                    ).to_analysis_result(),
                    f"text_blocks[-{index + 1}]",
                    errors,
                )
            except ValueError as exc:
                errors.append(
                    f"text_blocks[-{index + 1}]: "
                    + cls._bounded_error(exc)
                )
        if not errors:
            errors.append("未发现可解析且符合 Schema 的 AgentAnalysisDraft JSON")
        return None, None, errors

    @classmethod
    def _parse_with_submission(
        cls,
        *,
        submission_state: dict[str, Any],
        result_message: Any | None,
        text_blocks: list[str],
    ) -> tuple[AnalysisResult | None, str | None, list[str]]:
        submitted = submission_state.get("result")
        if isinstance(submitted, AnalysisResult):
            return submitted, "submit_analysis_result", []

        parsed, source, errors = cls._parse_analysis_result(
            result_message,
            text_blocks,
        )
        submission_errors = submission_state.get("errors")
        if isinstance(submission_errors, list):
            errors.extend(str(item) for item in submission_errors[-5:])
        return parsed, source, errors

    @staticmethod
    def _repair_prompt(
        parse_errors: list[str],
        *,
        manual_submission: bool = False,
    ) -> str:
        details = "\n".join(parse_errors[-5:])[-6000:]
        output_instruction = (
            "请调用 submit_analysis_result 重新提交精简 analysis Draft"
            if manual_submission
            else "请仅输出一个符合已配置 Draft JSON Schema 的 JSON 对象"
        )
        return (
            "最终结构化输出没有通过 AgentAnalysisDraft 校验。现在进入最终修复"
            "回合：不得再调用任何 Skill 或 Trace 工具，不得增加新证据或"
            "臆造字段。请仅依据本会话已有 Evidence，"
            f"{output_instruction}；缺少可选分析时使用 null "
            "或空数组，并将证据不足写入 limitations。上一轮校验信息：\n"
            f"{details}"
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
        if result_message is None:
            message_data: dict[str, Any] | None = None
        else:
            structured = getattr(
                result_message,
                "structured_output",
                None,
            )
            message_data = {
                "subtype": getattr(result_message, "subtype", None),
                "is_error": getattr(result_message, "is_error", None),
                "num_turns": getattr(result_message, "num_turns", None),
                "session_id": getattr(
                    result_message,
                    "session_id",
                    None,
                ),
                "stop_reason": getattr(
                    result_message,
                    "stop_reason",
                    None,
                ),
                "terminal_reason": getattr(
                    result_message,
                    "terminal_reason",
                    None,
                ),
                "errors": getattr(result_message, "errors", None),
                "structured_output_type": type(structured).__name__,
                "structured_output_preview": cls._bounded_preview(
                    structured,
                    50_000,
                ),
                "result_preview": cls._bounded_preview(
                    getattr(result_message, "result", None),
                    20_000,
                ),
            }
        return {
            "attempt": attempt,
            "parse_source": parse_source,
            "parse_errors": parse_errors,
            "result_message": message_data,
            "text_block_count": len(text_blocks),
            "text_previews": [
                cls._bounded_preview(item, 10_000)
                for item in text_blocks[-3:]
            ],
        }

    @staticmethod
    def _write_agent_result(
        request: AnalyzeRequest,
        *,
        status: str,
        attempts: list[dict[str, Any]],
        tools: ToolRegistry,
    ) -> Path:
        path = request.output_dir.resolve() / "agent-result.json"
        payload = {
            "status": status,
            "tool_budget": tools.budget_snapshot(),
            "attempts": attempts,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _bounded_error(error: ValueError) -> str:
        return str(error)[:4000]

    @staticmethod
    def _bounded_preview(value: Any, limit: int) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(
                    value,
                    ensure_ascii=False,
                    default=str,
                )
            except (TypeError, ValueError):
                text = repr(value)
        return text[:limit]
