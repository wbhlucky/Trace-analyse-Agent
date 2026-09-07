from __future__ import annotations

import asyncio
import uuid

from trace_agent.agent.transport import (
    AgentRequest,
    AgentResponse,
    AgentSession,
    RunCapableTransport,
)
from trace_agent.contract.result import (
    AnalysisContractResult,
    ContractEvidence,
    ContractFinding,
)
from trace_agent.contract.task import TaskContract
from trace_agent.errors import RunInterrupted, async_retry
from trace_agent.runtime.commands import (
    CommandResult,
    RuntimeCommand,
    RuntimeCommandType,
)
from trace_agent.runtime.events import EventType
from trace_agent.runtime.latency import LatencyPolicy
from trace_agent.runtime.sessions import AgentSessionRecord, SessionStore
from trace_agent.runtime.state import (
    RuntimeState,
    RuntimeStateMachine,
    RuntimeStateTransitionError,
)
from trace_agent.runtime.event_bus import TraceAgentEventBus, get_event_bus
from trace_agent.verification.result_verifier import (
    DeterministicResultVerifier,
    VerificationResult,
)


class AgentRuntime:
    """Owner of the lifecycle, control plane and repair loop for one task.

    It calls the transport exactly once per inference turn and delegates
    verification feedback back to the same session instead of restarting the
    whole job.  Highest-level callers should never touch a concrete SDK.

    The runtime is the only component allowed to mutate lifecycle state:
    transports observe state, tools never set it, and CLI/Web express intent
    through :class:`RuntimeCommand` rather than guessing completion.
    """

    def __init__(
        self,
        transport: RunCapableTransport,
        *,
        verifier: DeterministicResultVerifier | None = None,
        latency_policy: LatencyPolicy | None = None,
        max_repair_rounds: int = 1,
        session_store: SessionStore | None = None,
        event_bus: TraceAgentEventBus | None = None,
        max_retry_attempts: int = 3,
    ) -> None:
        self._transport = transport
        self._verifier = verifier or DeterministicResultVerifier()
        self._latency_policy = latency_policy or LatencyPolicy()
        self._max_repair_rounds = max_repair_rounds
        self._session_store = session_store
        self._event_bus = event_bus or get_event_bus()
        self._max_retry_attempts = max_retry_attempts

        self._state = RuntimeStateMachine()
        self._session: AgentSession | None = None
        self._session_record: AgentSessionRecord | None = None
        self._commands: dict[str, RuntimeCommand] = {}
        self._cancelled = False

    @property
    def state(self) -> RuntimeState:
        return self._state.state

    @property
    def session_id(self) -> str | None:
        return self._session.id if self._session else None

    async def run(self, task: TaskContract) -> AnalysisContractResult:
        self._session = self._new_session(task)
        self._session_record = self._new_session_record(task, self._session)
        self._persist_session()
        self._publish(EventType.RUN_STARTED)

        try:
            self._transition(RuntimeState.STARTING)
            self._transition(RuntimeState.RUNNING)
            result = await self._run_with_repair(task)
            self._transition(RuntimeState.COMPLETED)
            self._publish(
                EventType.RUN_COMPLETED,
                data={"session_id": self._session.id},
            )
            self._persist_session(RuntimeState.COMPLETED)
            return result
        except asyncio.CancelledError as exc:
            self._transition(RuntimeState.CANCELLED)
            self._publish(
                EventType.RUN_INTERRUPTED,
                data={"session_id": self._session.id, "reason": "cancelled"},
            )
            self._persist_session(RuntimeState.CANCELLED)
            raise RunInterrupted(run_id=task.task_id) from exc
        except RunInterrupted:
            self._transition(RuntimeState.CANCELLED)
            self._publish(
                EventType.RUN_INTERRUPTED,
                data={"session_id": self._session.id, "reason": "cancelled"},
            )
            self._persist_session(RuntimeState.CANCELLED)
            raise
        except Exception as exc:
            self._transition(RuntimeState.FAILED)
            self._publish(
                EventType.RUN_FAILED,
                data={
                    "session_id": self._session.id,
                    "error": type(exc).__name__,
                    "message": str(exc),
                },
            )
            self._persist_session(RuntimeState.FAILED)
            raise
        finally:
            await self._cleanup_transport()

    async def _run_with_repair(
        self,
        task: TaskContract,
    ) -> AnalysisContractResult:
        result = await self._invoke_with_retry(task)
        verification = self._verifier.verify(task, result)

        for repair_round in range(1, self._max_repair_rounds + 1):
            if verification.status.value in ("pass", "fail"):
                break
            self._transition(RuntimeState.REPAIRING)
            if self._session_record is not None:
                self._session_record.repair_round = repair_round
            self._publish(
                EventType.RUN_PAUSED,
                data={"reason": "verification.repair", "round": repair_round},
            )
            repair_prompt = self._repair_prompt(task, verification)
            result = await self._invoke_with_retry(task, prompt=repair_prompt)
            verification = self._verifier.verify(task, result)
            self._transition(RuntimeState.RUNNING)

        result.metadata["verification"] = verification.model_dump()
        return result

    def handle_command(self, command: RuntimeCommand) -> CommandResult:
        """Apply one control-plane command to this runtime's lifecycle."""
        if command.run_id != self.session_id:
            return CommandResult(
                command_id=command.command_id,
                accepted=False,
                reason="run_id mismatch",
                state=self.state.value,
            )

        self._commands[command.command_id] = command
        if command.type is RuntimeCommandType.CANCEL_RUN:
            self._cancelled = True
            active_states = (
                RuntimeState.CREATED,
                RuntimeState.STARTING,
                RuntimeState.RUNNING,
                RuntimeState.WAITING_INPUT,
                RuntimeState.WAITING_APPROVAL,
                RuntimeState.REPAIRING,
            )
            if self.state in active_states:
                self._transition(RuntimeState.CANCELLING)
                self._publish(
                    EventType.RUN_INTERRUPTED,
                    data={"reason": "cancel_requested"},
                )
            return CommandResult(
                command_id=command.command_id,
                accepted=True,
                state=self.state.value,
            )
        if command.type is RuntimeCommandType.RESUME_RUN:
            if self.state is not RuntimeState.WAITING_INPUT:
                return CommandResult(
                    command_id=command.command_id,
                    accepted=False,
                    reason=(
                        f"resume requires waiting_input, got {self.state.value}"
                    ),
                    state=self.state.value,
                )
            self._transition(RuntimeState.RUNNING)
            return CommandResult(
                command_id=command.command_id,
                accepted=True,
                state=self.state.value,
            )
        if command.type in (
            RuntimeCommandType.APPROVE_TOOL,
            RuntimeCommandType.DENY_TOOL,
        ):
            # Stored for the permission hook boundary added in a later phase.
            return CommandResult(
                command_id=command.command_id,
                accepted=True,
                state=self.state.value,
            )
        return CommandResult(
            command_id=command.command_id,
            accepted=False,
            reason=f"unsupported command: {command.type.value}",
            state=self.state.value,
        )

    def _new_session(self, task: TaskContract) -> AgentSession:
        return AgentSession(id=f"{task.task_id}-{uuid.uuid4().hex[:12]}")

    def _new_session_record(
        self,
        task: TaskContract,
        session: AgentSession,
    ) -> AgentSessionRecord:
        return AgentSessionRecord(
            session_id=session.id,
            task_id=task.task_id,
            runtime_state=RuntimeState.CREATED,
            metadata={
                "trace_id": task.trace_id,
                "scenario_type": task.scenario_type.value,
            },
        )

    def _persist_session(self, state: RuntimeState | None = None) -> None:
        if self._session_store is None or self._session_record is None:
            return
        if state is not None:
            self._session_record.runtime_state = state
        self._session_store.save(self._session_record)

    def _publish(
        self,
        event_type: EventType,
        *,
        data: dict | None = None,
    ) -> None:
        if self._session is None:
            return
        from trace_agent.runtime.events import AgentEvent

        self._event_bus.publish(
            AgentEvent.new(
                run_id=self._session.id,
                type=event_type,
                data=data,
            )
        )
        if self._session_record is not None and self._session_store is not None:
            try:
                last = self._event_bus.replay(self._session.id)
                if last:
                    self._session_record.last_event_id = last[-1].event_id
                    self._session_store.append_event(
                        self._session.id,
                        last[-1],
                    )
            except Exception:
                pass

    def _transition(self, to: RuntimeState) -> None:
        self._state = self._state.transition(to)

    async def _cleanup_transport(self) -> None:
        if self._session is None:
            return
        try:
            await self._transport.close(self._session.id)
        except Exception:
            return

    async def _invoke_with_retry(
        self,
        task: TaskContract,
        *,
        prompt: str | None = None,
    ) -> AnalysisContractResult:
        assert self._session is not None

        async def invoke_once() -> AnalysisContractResult:
            self._raise_if_cancelled()
            return await self._invoke(task, self._session, prompt=prompt)

        return await async_retry(
            invoke_once,
            max_attempts=self._max_retry_attempts,
        )

    def _raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise RunInterrupted(
                run_id=self._session.id if self._session else None
            )

    async def _invoke(
        self,
        task: TaskContract,
        session: AgentSession,
        *,
        prompt: str | None = None,
    ) -> AnalysisContractResult:
        timeout = self._latency_policy.llm_turn_timeout_seconds
        self._publish(EventType.PHASE_STARTED, data={"stage": "agent.analyze"})
        try:
            response = await self._transport.run(
                AgentRequest(
                    prompt=prompt or self._prompt(task),
                    tools=[],
                    session_id=session.id,
                ),
                session=session,
                timeout=timeout,
            )
        finally:
            self._publish(
                EventType.PHASE_COMPLETED,
                data={"stage": "agent.analyze"},
            )
        return self._to_result(response)

    def _prompt(self, task: TaskContract) -> str:
        parts = [
            "Analyze the registered trace and return a structured result.",
            f"scenario={task.scenario_type.value}",
            f"symptom={task.symptom}",
        ]
        if task.target_process:
            parts.append(f"target_process={task.target_process}")
        if task.time_range:
            parts.append(f"time_range={task.time_range}")
        return "\n".join(parts)

    def _repair_prompt(
        self,
        task: TaskContract,
        verification: VerificationResult,
    ) -> str:
        del task
        details = "\n".join(
            f"{issue.code}: {issue.message}"
            for issue in verification.issues
        )
        return (
            "The previous result failed deterministic verification. "
            "Repair the same structured result without adding new evidence "
            "or changing already-valid evidence. Issues:\n"
            f"{details}"
        )

    @staticmethod
    def _to_result(response: AgentResponse) -> AnalysisContractResult:
        findings = [
            ContractFinding(
                category=call.name,
                claim="Tool call issued by the model",
            )
            for call in response.tool_calls
        ]
        evidence_count = response.metadata.get("evidence_count", 0)
        if not isinstance(evidence_count, int):
            evidence_count = 0
        evidence = [
            ContractEvidence(
                evidence_id=f"ev-{index + 1}",
                source="transport",
                description=response.text,
            )
            for index in range(evidence_count)
        ]
        return AnalysisContractResult(
            conclusion=response.text,
            findings=findings,
            evidence=evidence,
            metadata=dict(response.metadata),
        )

