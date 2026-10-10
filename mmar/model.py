from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Callable, Protocol

from .protocol import Decision, StepSnapshot


@dataclass(frozen=True, slots=True)
class ModelReply:
    output: str | dict | Decision
    usage: dict | None = None


class ModelAdapter(Protocol):
    async def complete(self, snapshot: StepSnapshot, *, repair: bool = False) -> ModelReply: ...
    async def aclose(self) -> None: ...


class MockModel:
    """No network. Optional decision callback supports sync/async test policies."""

    def __init__(self, policy: Callable | None = None, delay: float = 0.01):
        self.policy, self.delay = policy, delay
        self.calls = 0

    async def complete(self, snapshot: StepSnapshot, *, repair: bool = False) -> ModelReply:
        self.calls += 1
        await asyncio.sleep(self.delay)
        output = self.policy(snapshot) if self.policy else Decision()
        if inspect.isawaitable(output):
            output = await output
        return output if isinstance(output, ModelReply) else ModelReply(output)

    async def aclose(self) -> None:
        pass
