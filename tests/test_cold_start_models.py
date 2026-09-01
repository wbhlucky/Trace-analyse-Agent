from __future__ import annotations

from trace_agent.models import (
    ColdStartAnalysis,
    ColdStartStage,
    CpuExecutionShare,
    ResolvedProcess,
    SchedulingDiagnosis,
    ThreadExecutionAnalysis,
    ThreadPriorityProfile,
    ThreadStateBreakdown,
    TraceBoundary,
)


def test_cold_start_analysis_accepts_structured_scheduling_data():
    thread = ThreadExecutionAnalysis(
        process_name="com.example.app",
        thread_name="main",
        pid=100,
        ipid=10,
        tid=100,
        itid=11,
        state_breakdown=ThreadStateBreakdown(
            running_ms=120,
            runnable_ms=20,
            sleeping_ms=60,
            uninterruptible_io_ms=0,
            uninterruptible_other_ms=0,
            other_ms=0,
        ),
        cpu_distribution=[
            CpuExecutionShare(
                cpu=3,
                running_ms=80,
                share=2 / 3,
                schedule_slices=4,
            ),
            CpuExecutionShare(
                cpu=5,
                running_ms=40,
                share=1 / 3,
                schedule_slices=2,
            ),
        ],
        cpu_migrations=1,
        schedule_slices=6,
        longest_running_ms=45,
        longest_runnable_ms=12,
        longest_sleep_ms=40,
        priority=ThreadPriorityProfile(
            observed_values=[120],
            dominant_value=120,
            interpretation="保留原始优先级，平台排序语义未确认",
        ),
        diagnosis=SchedulingDiagnosis.MIXED,
        assessment="CPU 执行与等待共同贡献该阶段耗时",
        confidence=0.8,
        evidence_ids=["ev-0004"],
    )
    stage = ColdStartStage(
        name="application-init",
        start_ns=1_000_000_000,
        end_ns=1_200_000_000,
        duration_ms=200,
        critical_threads=[thread],
        assessment="初始化阶段",
        evidence_ids=["ev-0003", "ev-0004"],
    )

    result = ColdStartAnalysis(
        resolved_process=ResolvedProcess(
            name="com.example.app",
            pid=100,
            ipid=10,
            main_tid=100,
            main_itid=11,
            selection_reason="Trace 内新建并产生首帧",
            confidence=0.95,
            evidence_ids=["ev-0001"],
        ),
        cold_start_proven=True,
        classification_reason="目标进程在 Trace 内首次创建",
        start_boundary=TraceBoundary(
            name="process-start",
            timestamp_ns=1_000_000_000,
            source="process.start_ts",
            confidence=0.9,
            evidence_ids=["ev-0001"],
        ),
        end_boundary=TraceBoundary(
            name="first-frame",
            timestamp_ns=1_200_000_000,
            source="frame_slice",
            source_id="frame_slice:42",
            confidence=0.9,
            evidence_ids=["ev-0002"],
        ),
        total_duration_ms=200,
        stages=[stage],
        critical_path_summary="初始化阶段控制首帧完成",
        evidence_ids=["ev-0001", "ev-0002", "ev-0004"],
    )

    assert result.resolved_process.main_itid == 11
    assert result.stages[0].critical_threads[0].cpu_migrations == 1
    assert (
        result.stages[0].critical_threads[0].diagnosis
        is SchedulingDiagnosis.MIXED
    )
