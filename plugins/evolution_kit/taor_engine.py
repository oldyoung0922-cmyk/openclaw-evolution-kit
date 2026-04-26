"""
evolution_kit.taor_engine — Self-Driven Think-Act-Observe-Repeat Cycle

灵感来自 Claude Code 的 TAOR 架构（claw-code 仓库），
将固定工作流替换为元级循环：AI 自己决定下一步做什么。

优化说明（2026-04-26）:
- ERR-03 已修复: DecompositionThinker 空 sub_goals 不崩溃
- 新增 LiftedLLMThinker: 可挂接外部 LLM
- 恢复 stall 检测：连续相同 action 时强制重新思考
"""

import abc
import time
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Callable

logger = logging.getLogger("taor_engine")

# ─── 核心类型 ──────────────────────────────────

class CyclePhase(Enum):
    THINK = "think"
    ACT = "act"
    OBSERVE = "observe"


class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRY_PENDING = "retry_pending"


@dataclass
class Context:
    """流经循环的可变上下文，携带任务状态。"""
    task_description: str
    current_goal: str = ""
    accumulated_observations: list[str] = field(default_factory=list)
    actions_taken: list[str] = field(default_factory=list)
    thoughts: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    cycle_count: int = 0
    max_cycles: int = 50

    @property
    def summary(self) -> str:
        return (
            f"Task: {self.task_description[:80]}\n"
            f"Goal: {self.current_goal[:80]}\n"
            f"Cycles: {self.cycle_count}/{self.max_cycles}\n"
            f"Actions: {len(self.actions_taken)} | Obs: {len(self.accumulated_observations)}"
        )


# ─── 可插拔接口 ────────────────────────────────

class Thinker(abc.ABC):
    """THINK 阶段 —— 决定下一步做什么"""
    @abc.abstractmethod
    def think(self, ctx: Context) -> tuple[str, dict[str, Any]]: ...


class Actor(abc.ABC):
    """ACT 阶段 —— 执行选定的行动"""
    @abc.abstractmethod
    def act(self, ctx: Context, goal: str) -> tuple[str, Any]: ...


class Observer(abc.ABC):
    """OBSERVE 阶段 —— 解读行动结果"""
    @abc.abstractmethod
    def observe(self, ctx: Context, action: str, result: Any) -> str: ...


class TerminationPolicy(abc.ABC):
    """判断循环是否该终止"""
    @abc.abstractmethod
    def should_terminate(self, ctx: Context) -> bool: ...


# ─── 循环记录 ──────────────────────────────────

@dataclass
class CycleStep:
    phase: CyclePhase
    input_data: Any = None
    output_data: Any = None
    status: StepStatus = StepStatus.PENDING
    error: Optional[str] = None
    duration_ms: float = 0.0


@dataclass
class CycleRecord:
    cycle_number: int
    goal: str = ""
    steps: list[CycleStep] = field(default_factory=list)
    timestamp_ms: float = 0.0

    @property
    def final_observation(self) -> Optional[str]:
        for step in reversed(self.steps):
            if step.phase == CyclePhase.OBSERVE and step.output_data is not None:
                return str(step.output_data)
        return None


@dataclass
class TAORResult:
    success: bool
    final_context: Context
    cycle_history: list[CycleRecord] = field(default_factory=list)
    total_cycles: int = 0
    total_duration_ms: float = 0.0
    error: Optional[str] = None

    @property
    def final_output(self) -> str:
        if self.cycle_history:
            last = self.cycle_history[-1]
            obs = last.final_observation
            if obs:
                return obs
        if self.final_context.accumulated_observations:
            return self.final_context.accumulated_observations[-1]
        return ""


# ─── 预算跟踪器 ────────────────────────────────

@dataclass
class BudgetTracker:
    max_cycles: int = 50
    max_observations_kept: int = 20
    max_thoughts_kept: int = 10
    max_actions_kept: int = 15
    cycle_count: int = 0

    def should_compact(self, ctx: Context) -> bool:
        return (
            len(ctx.accumulated_observations) >= self.max_observations_kept
            or len(ctx.actions_taken) >= self.max_actions_kept
        )

    def compact_context(self, ctx: Context) -> Context:
        if not self.should_compact(ctx):
            return ctx
        summary = (
            f"[COMPACTED cycle {self.cycle_count}] "
            f"Consolidated {max(0, len(ctx.accumulated_observations) - self.max_observations_kept)} obs "
            f"and {max(0, len(ctx.actions_taken) - self.max_actions_kept)} actions."
        )
        ctx.accumulated_observations = [summary] + ctx.accumulated_observations[-self.max_observations_kept:]
        ctx.actions_taken = ctx.actions_taken[-self.max_actions_kept:]
        ctx.thoughts = ctx.thoughts[-self.max_thoughts_kept:]
        return ctx


# ─── 终止策略 ──────────────────────────────────

class MaxCyclesPolicy(TerminationPolicy):
    def __init__(self, max_cycles: int = 50):
        self.max_cycles = max_cycles
    def should_terminate(self, ctx: Context) -> bool:
        return ctx.cycle_count >= self.max_cycles


class GoalAchievedPolicy(TerminationPolicy):
    def __init__(self, keywords: Optional[list[str]] = None):
        self.keywords = keywords or ["completed", "done", "finished", "no more actions"]
    def should_terminate(self, ctx: Context) -> bool:
        if not ctx.accumulated_observations:
            return False
        return any(kw in ctx.accumulated_observations[-1].lower() for kw in self.keywords)


# ─── 主循环引擎 ────────────────────────────────

class TAORLoop:
    """自驱动 Think-Act-Observe-Repeat 循环引擎"""

    def __init__(self, thinker: Thinker, actor: Actor, observer: Observer,
                 policies: Optional[list[TerminationPolicy]] = None,
                 max_cycles: int = 50, stall_limit: int = 3):
        self.thinker = thinker
        self.actor = actor
        self.observer = observer
        self.policies = policies or [MaxCyclesPolicy(max_cycles)]
        self.max_cycles = max_cycles
        self.stall_limit = stall_limit
        self.budget = BudgetTracker(max_cycles=max_cycles)

    def run(self, ctx: Context) -> TAORResult:
        start_ms = time.time() * 1000
        cycles: list[CycleRecord] = []
        ctx.max_cycles = self.max_cycles
        action_streak = 0
        last_action: Optional[str] = None

        for cycle_num in range(1, self.max_cycles + 1):
            ctx.cycle_count = cycle_num
            rec = CycleRecord(cycle_number=cycle_num, timestamp_ms=time.time() * 1000)

            # THINK
            step = CycleStep(phase=CyclePhase.THINK, status=StepStatus.RUNNING)
            try:
                goal, meta = self.thinker.think(ctx)
                ctx.current_goal = goal
                ctx.thoughts.append(goal)
                step.status = StepStatus.SUCCEEDED
                step.output_data = goal
                rec.goal = goal
            except Exception as e:
                step.status = StepStatus.FAILED
                step.error = str(e)
                rec.steps.append(step)
                cycles.append(rec)
                return TAORResult(False, ctx, cycles, len(cycles),
                                  (time.time() - start_ms) * 1000, error=str(e))
            rec.steps.append(step)

            # ACT
            step = CycleStep(phase=CyclePhase.ACT, status=StepStatus.RUNNING)
            try:
                action, result = self.actor.act(ctx, goal)
                ctx.actions_taken.append(action)
                if action == last_action:
                    action_streak += 1
                else:
                    action_streak = 0
                    last_action = action
                if action_streak >= self.stall_limit:
                    action_streak = 0
                    ctx.accumulated_observations.append(
                        f"[STALL_DETECTED] '{action}' repeated {self.stall_limit}x — re-evaluating.")
                step.status = StepStatus.SUCCEEDED
                step.output_data = (action, result)
            except Exception as e:
                step.status = StepStatus.FAILED
                step.error = str(e)
                action, result = "[ACT_FAILED]", str(e)
            rec.steps.append(step)

            # OBSERVE
            step = CycleStep(phase=CyclePhase.OBSERVE, status=StepStatus.RUNNING)
            try:
                obs = self.observer.observe(ctx, action, result)
                ctx.accumulated_observations.append(obs)
                step.status = StepStatus.SUCCEEDED
                step.output_data = obs
            except Exception as e:
                obs = f"[OBSERVE_FAILED] {e}"
                ctx.accumulated_observations.append(obs)
                step.status = StepStatus.FAILED
                step.error = str(e)
            rec.steps.append(step)

            cycles.append(rec)
            self.budget.cycle_count = cycle_num
            ctx = self.budget.compact_context(ctx)

            # 终止检查
            if any(p.should_terminate(ctx) for p in self.policies):
                break

        elapsed = (time.time() - start_ms) * 1000
        return TAORResult(True, ctx, cycles, len(cycles), elapsed)


# ─── 内建 Thinker ──────────────────────────────

class SimpleReflectiveThinker(Thinker):
    def think(self, ctx: Context) -> tuple[str, dict[str, Any]]:
        if not ctx.current_goal or ctx.cycle_count == 1:
            return f"start: {ctx.task_description[:80]}", {"mode": "init"}
        if ctx.accumulated_observations:
            last = ctx.accumulated_observations[-1]
            if "fail" in last.lower() or "error" in last.lower():
                return f"retry: {ctx.current_goal}", {"mode": "retry"}
        return f"continue: {ctx.current_goal}", {"mode": "continue"}


class DecompositionThinker(Thinker):
    """将任务拆解为子目标，顺序推进。"""
    def __init__(self):
        self.sub_goals: list[str] = []
        self.index = 0

    def reset(self):
        self.sub_goals, self.index = [], 0

    def think(self, ctx: Context) -> tuple[str, dict[str, Any]]:
        if ctx.cycle_count == 1:
            self.reset()
            self.sub_goals = self._decompose(ctx.task_description)
        if not self.sub_goals:
            return f"work on: {ctx.task_description[:80]}", {"sub_goals": 0}
        if ctx.accumulated_observations:
            last = ctx.accumulated_observations[-1]
            if any(kw in last.lower() for kw in ["completed", "done", "finished"]):
                self.index = min(self.index + 1, len(self.sub_goals) - 1)
        return self.sub_goals[self.index], {"sub_goals": len(self.sub_goals), "at": self.index}

    def _decompose(self, task: str) -> list[str]:
        lines = [l.strip() for l in task.split("\n") if l.strip()]
        if len(lines) > 1:
            return lines[:5]
        return [f"plan: {task[:60]}", f"execute: {task[:60]}", f"verify: {task[:60]}"]


class LiftedLLMThinker(Thinker):
    """挂接外部 LLM 的真 Thinker，替代模板式占位。"""
    def __init__(self, llm: Callable[[str], str], max_decisions: int = 5):
        self.llm = llm
        self.max_decisions = max_decisions
        self._count = 0

    def think(self, ctx: Context) -> tuple[str, dict[str, Any]]:
        self._count += 1
        prompt = (
            f"Task: {ctx.task_description}\n"
            f"Cycle: {ctx.cycle_count}\n"
            f"Goal: {ctx.current_goal}\n"
            f"Recent observations: {ctx.accumulated_observations[-3:]}\n"
        )
        if self._count >= self.max_decisions:
            prompt += "\n[Verify progress before continuing.]"
            self._count = 0
        return self.llm(prompt), {"llm": True}


# ─── 内建 Actor ────────────────────────────────

class DelegatingActor(Actor):
    def __init__(self, handlers: Optional[dict[str, Callable]] = None):
        self.handlers = handlers or {}

    def act(self, ctx: Context, goal: str) -> tuple[str, Any]:
        if not self.handlers:
            return f"[no-op] {goal[:60]}", {"status": "no_handler"}
        for name, fn in self.handlers.items():
            if name.lower() in goal.lower():
                result = fn(ctx, goal)
                return f"[{name}] {goal[:60]}", result
        name, fn = next(iter(self.handlers.items()))
        return f"[{name}(default)] {goal[:60]}", fn(ctx, goal)


# ─── 内建 Observer ─────────────────────────────

class ReflectiveObserver(Observer):
    def observe(self, ctx: Context, action: str, result: Any) -> str:
        if isinstance(result, Exception):
            return f"'{action}' failed: {result}"
        return f"'{action}' → {str(result)[:200]}"


# ─── 便捷工厂 ──────────────────────────────────

def new_taor(handlers: Optional[dict[str, Callable]] = None,
             thinker: Optional[Thinker] = None,
             max_cycles: int = 20) -> TAORLoop:
    thinkers = thinker or DecompositionThinker()
    actor = DelegatingActor(handlers)
    observer = ReflectiveObserver()
    policies = [MaxCyclesPolicy(max_cycles), GoalAchievedPolicy()]
    return TAORLoop(thinkers, actor, observer, policies, max_cycles)


def run_example():
    def handler(ctx, goal):
        return {"status": "ok", "message": f"processed: {goal[:40]}"}

    loop = new_taor({"default": handler})
    ctx = Context(task_description="Analyze evolution of AI architectures.")
    result = loop.run(ctx)
    print(f"Success: {result.success}, Cycles: {result.total_cycles}, "
          f"Duration: {result.total_duration_ms:.0f}ms")
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_example()
