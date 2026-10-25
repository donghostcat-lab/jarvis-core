"""Client for decision models with the System One interface (POST /v1/systemone).

TypeSafe Jev (cloud) and local servers such as ollaya (Laya, Decima, NLI, Kev …),
Kev itself or laya-server speak the same interface. Which model answers is decided
by DECIDER_URL + DECIDER_MODEL alone.

Request:  {"state": "...", "model": "...", "questions": {name: {type, instructions, criteria}}}
Response: {"answers": {name: {"type": "noul", "noul": 0.93}
                              | {"type": "choice", "choice": "x", "probabilities": {...}}
                              | {"type": "score", "score": 1.4, "probabilities": {...}}}}
"""

import time
from dataclasses import dataclass, field

import httpx


@dataclass(frozen=True)
class Noul:
    """Yes/no question; the answer is the probability of "yes"."""
    instructions: str

    def payload(self) -> dict:
        return {"type": "noul", "instructions": self.instructions}


@dataclass(frozen=True)
class Choice:
    instructions: str
    criteria: dict[str, str]  # option → description

    def payload(self) -> dict:
        return {"type": "choice", "instructions": self.instructions, "criteria": self.criteria}


@dataclass(frozen=True)
class Score:
    instructions: str
    criteria: list[str]  # levels from low to high

    def payload(self) -> dict:
        return {"type": "score", "instructions": self.instructions, "criteria": self.criteria}


@dataclass
class Answer:
    kind: str
    value: float | str | None = None       # noul: p(yes); choice: option; score: expected value
    confidence: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)


@dataclass
class DecisionResult:
    answers: dict[str, Answer]
    model: str
    latency_ms: float


class SystemOneClient:
    def __init__(self, base_url: str, model: str, api_key: str | None = None,
                 timeout: float = 30.0, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self._transport = transport  # for tests

    async def decide(self, state: str, questions: dict) -> DecisionResult:
        body = {"state": state, "model": self.model,
                "questions": {name: q.payload() for name, q in questions.items()}}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        start = time.perf_counter()
        async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
            resp = await client.post(f"{self.base_url}/v1/systemone", json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        latency = (time.perf_counter() - start) * 1000
        answers = {name: _parse(raw) for name, raw in (data.get("answers") or {}).items()}
        return DecisionResult(answers, data.get("model", self.model), round(latency, 1))


def _parse(raw: dict) -> Answer:
    kind = raw.get("type", "")
    probs = {str(k): float(v) for k, v in (raw.get("probabilities") or {}).items()}
    if kind == "noul":
        return Answer("noul", float(raw.get("noul", 0.0)))
    if kind == "choice":
        choice = raw.get("choice")
        conf = raw.get("confidence")
        return Answer("choice", choice, float(conf) if conf is not None else None, probs)
    if kind == "score":
        score = raw.get("score")
        legend = raw.get("legend") or {}
        # Return probabilities keyed by level name instead of index.
        named = {legend.get(k, k): v for k, v in probs.items()}
        conf = raw.get("confidence")
        return Answer("score", float(score) if score is not None else None,
                      float(conf) if conf is not None else None, named)
    return Answer(kind or "unknown")
