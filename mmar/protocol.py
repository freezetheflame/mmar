from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any


class MmarError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class InvalidDecision(MmarError):
    pass


class TransientModelError(MmarError):
    pass


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", value):
        raise MmarError("invalid_identifier")
    return value


def bounded_text(value: str, limit: int, code: str = "text_too_large") -> str:
    if not isinstance(value, str) or not value.strip():
        raise MmarError("empty_text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise MmarError("invalid_unicode") from None
    if size > limit:
        raise MmarError(code)
    return value


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True, slots=True)
class Limits:
    concurrency: int = 4
    batch_size: int = 8
    inbox: int = 128
    pending_total: int = 4096
    actions: int = 4
    targets: int = 32
    fanout: int = 64
    message_bytes: int = 8192
    identity_bytes: int = 8192
    memory_bytes: int = 8192
    directory_bytes: int = 16384
    context_bytes: int = 65536
    history_messages: int = 8
    output_bytes: int = 65536
    steps: int = 1000
    calls: int = 1500
    deliveries: int = 10000
    session_steps: int = 100
    session_calls: int = 150
    session_deliveries: int = 1000
    agent_steps: int = 200
    agent_calls: int = 300
    attempts: int = 3
    recovery_attempts: int = 1
    request_timeout: float = 60.0
    step_timeout: float = 180.0
    retry_delay: float = 0.25
    store_queue: int = 128

    def __post_init__(self):
        for key, value in asdict(self).items():
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"Finite numeric limit required: {key}")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                if key not in ("history_messages", "recovery_attempts", "retry_delay") or value != 0:
                    raise ValueError(f"Invalid limit: {key}")
            if key not in ("request_timeout", "step_timeout", "retry_delay") and type(value) is not int:
                raise ValueError(f"Integer required: {key}")
        if self.context_bytes < self.identity_bytes + self.memory_bytes + self.directory_bytes + 4096:
            raise ValueError("context_bytes must allow identity, memory, directory and protocol")


@dataclass(frozen=True, slots=True)
class Decision:
    actions: tuple[dict, ...] = ()
    memory_replace: str | None = None

    def to_dict(self) -> dict:
        return {"actions": list(self.actions), "memory_replace": self.memory_replace}

    @classmethod
    def parse(cls, raw: str | dict | Decision, limits: Limits) -> Decision:
        try:
            if isinstance(raw, cls):
                raw = raw.to_dict()
            if isinstance(raw, str):
                bounded_text(raw, limits.output_bytes)
                def pairs(items):
                    result = {}
                    for key, value in items:
                        if key in result:
                            raise ValueError("duplicate key")
                        result[key] = value
                    return result
                raw = json.loads(raw, object_pairs_hook=pairs,
                                 parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(raw, dict) or set(raw) != {"actions", "memory_replace"}:
                raise ValueError()
            actions = raw["actions"]
            if not isinstance(actions, list) or len(actions) > limits.actions:
                raise ValueError()
            clean = []
            for action in actions:
                if not isinstance(action, dict):
                    raise ValueError()
                kind = action.get("type")
                keys = {"type", "targets", "content"} if kind == "send" else {"type", "group", "content"}
                if kind not in ("send", "broadcast") or set(action) != keys:
                    raise ValueError()
                bounded_text(action["content"], limits.message_bytes)
                if kind == "send":
                    targets = action["targets"]
                    if not isinstance(targets, list) or not 1 <= len(targets) <= limits.targets:
                        raise ValueError()
                    targets = [identifier(t) for t in targets]
                    if len(set(targets)) != len(targets):
                        raise ValueError()
                    clean.append({"type": kind, "targets": targets, "content": action["content"]})
                else:
                    clean.append({"type": kind, "group": identifier(action["group"]), "content": action["content"]})
            memory = raw["memory_replace"]
            if memory is not None:
                bounded_text(memory, limits.memory_bytes)
            bounded_text(dumps(raw), limits.output_bytes)
            return cls(tuple(clean), memory)
        except (ValueError, TypeError, KeyError, RecursionError, MmarError):
            raise InvalidDecision("invalid_decision") from None


@dataclass(frozen=True, slots=True)
class StepSnapshot:
    step_id: str
    agent_id: str
    session_id: str
    identity: str
    memory: str
    directory: dict
    messages: tuple[dict, ...]
    history: tuple[dict, ...] = ()
    limits: dict = field(default_factory=dict)
