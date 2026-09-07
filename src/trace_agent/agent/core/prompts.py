"""Provider-independent prompt construction."""

from __future__ import annotations

import json
from typing import Any

from trace_agent.models import AgentAnalysisDraft, AnalyzeRequest
from trace_agent.skills import SkillDefinition


def system_prompt(
    skills: list[SkillDefinition],
    *,
    memory_context: str | None = None,
    manual_submission: bool = False,
) -> str:
    enabled_skills = "、".join(
        skill.name for skill in skills
    )
    skill_calls = "、".join(
        f"Skill({skill.name})" for skill in skills
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



def user_prompt(
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



def repair_prompt(
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


