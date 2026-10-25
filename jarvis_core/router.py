"""Local router: observes every message, and decides in protect mode.

For each message it decides:
- sensitive?  (health, money, relationships, legal, credentials …) → local model in protect mode
- intent      (shopping, reminder, waste, …)                       → later a fast path
- complexity  (simple / medium / complex)                          → later a smaller model

Without protect mode it only observes (`RouterObserver.observe`, in the background). With
protect mode (`Jarvis(protect=True)`) the core waits for `RouterObserver.decide` and keeps a
sensitive message, or one the router cannot classify, away from cloud models.

Everything runs locally on the server: keyword rules plus a small multilingual embedding
model (ONNX via fastembed, no PyTorch) with nearest neighbours on example sentences. The
decisions go to data/router.sqlite; a profile can show them (`RouterLog.stats`), correct them
(`RouterLog.label_last`) and export them as CSV:

    python -m jarvis_core.router test "I have had back pain for a week"
    python -m jarvis_core.router export /data/router.sqlite router.csv [--with-private]
"""

import asyncio
import csv
import ipaddress
import json
import logging
import re
import sqlite3
import sys
import threading
import time
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import numpy as np

from . import router_seeds
from .decider import Choice, Noul, Score, SystemOneClient

log = logging.getLogger(__name__)

DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# Hits here always count as sensitive, whatever the model says. German and English.
# "stem": may sit inside compound words (Rücken-schmerzen, Haus-arzt, Konto-auszug).
# "word": only as a whole word, because short or ambiguous (PIN ≠ Pinguin, Steuer ≠ Heizungssteuerung).
SENSITIVE_RULES = {
    "health": {
        "stem": ["arzt", "ärzt", "diagnose", "krank", "schmerz", "therapi", "therapeut",
                 "medikament", "tablette", "depress", "psych", "blutzucker", "blutdruck",
                 "schwanger", "krebs", "demenz", "klinik", "krankenhaus", "operation", "symptom",
                 "doctor", "diagnos", "illness", "medication", "pregnan", "cancer", "dementia",
                 "hospital", "surgery", "blood pressure", "blood sugar"],
        "word": ["sick", "pain", "ill"],
    },
    "finance": {
        "stem": ["gehalt", "konto", "schulden", "kredit", "überweisung", "insolvenz",
                 "pfändung", "steuererklärung", "steuerbescheid", "steuerberat", "nachzahlung",
                 "salary", "debt", "bankrupt", "tax return"],
        "word": ["iban", "mahnung", "steuer", "steuern", "loan", "overdraft"],
    },
    "credentials": {
        "stem": ["passwort", "kennwort", "zugangsdaten", "password", "passcode"],
        "word": ["pin", "tan", "login"],
    },
    "relationship": {
        "stem": ["trennung", "scheidung", "affäre", "seitensprung", "divorce"],
        "word": ["trennen", "streit", "streiten", "gestritten", "affair", "breakup"],
    },
    "legal": {
        "stem": ["anwalt", "anwält", "gericht", "abmahnung", "jugendamt", "kündigung",
                 "lawyer", "lawsuit", "dismissal"],
        "word": ["polizei", "anzeige", "police", "court"],
    },
}


def _compile_rule(rule: dict) -> re.Pattern:
    parts = [rf"\w*{re.escape(s)}\w*" for s in rule["stem"]]
    parts += [rf"\b{re.escape(w)}\b" for w in rule["word"]]
    return re.compile("|".join(parts), re.IGNORECASE)


_RULES = {name: _compile_rule(rule) for name, rule in SENSITIVE_RULES.items()}

OTHER = "other"
FAST_INTENTS = {"shopping", "reminder", "waste", "time", "smalltalk", "home"}
_COMPLEX_HINT = re.compile(r"\b(erklär\w*|vergleich\w*|plane?n?|analysier\w*|schreib\w* (mir )?"
                           r"(einen|eine|ein)|zusammenfass\w*|warum|begründ\w*"
                           r"|explain\w*|compar\w*|planning|analy[sz]\w*|write me an?"
                           r"|summar\w*|why|justify)\b", re.IGNORECASE)


# ---------- Embeddings ----------

class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> np.ndarray: ...


class HashEmbedder:
    """Hashed character 3-grams. Needs no download: the fallback, and for tests."""

    name = "hash-3gram"

    def __init__(self, dim: int = 2048):
        self.dim = dim

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            t = f"  {text.lower()}  "
            for i in range(len(t) - 2):
                out[row, zlib.crc32(t[i:i + 3].encode()) % self.dim] += 1.0
        return out


class FastEmbedder:
    """Multilingual sentence embedding via fastembed (ONNX). Loads the model on first use."""

    def __init__(self, model: str = DEFAULT_MODEL, cache_dir: str | None = None):
        self.name = model
        self.cache_dir = cache_dir
        self._model = None

    def embed(self, texts: list[str]) -> np.ndarray:
        if self._model is None:
            from fastembed import TextEmbedding  # load only when needed
            self._model = TextEmbedding(self.name, cache_dir=self.cache_dir)
        return np.array(list(self._model.embed(texts)), dtype=np.float32)


def _normalize(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.where(norms == 0, 1, norms)


# ---------- Decision ----------

@dataclass
class Decision:
    sensitive: bool
    sensitive_score: float          # 0..1 from the model
    reasons: list[str] = field(default_factory=list)  # rules that matched
    intent: str = OTHER
    intent_score: float = 0.0
    complexity: str = "medium"
    embedder: str = ""
    latency_ms: float = 0.0


class Router:
    def __init__(self, embedder: Embedder, threshold: float = 0.5, k: int = 3,
                 fallback: Embedder | None = None):
        self.embedder = embedder
        self.fallback = fallback or HashEmbedder()
        self.threshold = threshold
        self.k = k
        self._seeds: dict | None = None
        # Preloading at start-up and the first message run in different threads.
        self._lock = threading.Lock()

    def _prepare(self) -> None:
        with self._lock:
            if self._seeds is not None:
                return
            try:
                self._seeds = self._embed_seeds(self.embedder)
            except Exception:
                log.warning("Router: cannot load embedding model %s, using %s",
                            getattr(self.embedder, "name", "?"), self.fallback.name, exc_info=True)
                self.embedder = self.fallback
                self._seeds = self._embed_seeds(self.embedder)

    @staticmethod
    def _embed_seeds(embedder: Embedder) -> dict:
        intents = {name: _normalize(embedder.embed(ex)) for name, ex in router_seeds.INTENTS.items()}
        return {
            "sensitive": _normalize(embedder.embed(router_seeds.SENSITIVE)),
            "normal": _normalize(embedder.embed(router_seeds.NORMAL)),
            "intents": intents,
        }

    def classify(self, text: str) -> Decision:
        start = time.perf_counter()
        self._prepare()
        vec = _normalize(self.embedder.embed([text]))[0]

        def top_k(matrix: np.ndarray) -> float:
            sims = np.sort(matrix @ vec)[::-1]
            return float(np.mean(sims[: self.k]))

        s, n = top_k(self._seeds["sensitive"]), top_k(self._seeds["normal"])
        # Turn the gap between the similarities into a probability (temperature 0.05).
        score = float(1.0 / (1.0 + np.exp(-(s - n) / 0.05)))
        reasons = sorted(name for name, rx in _RULES.items() if rx.search(text))

        intent_scores = {name: float(np.max(m @ vec)) for name, m in self._seeds["intents"].items()}
        intent, intent_score = max(intent_scores.items(), key=lambda kv: kv[1])
        if intent_score < 0.35:
            intent = OTHER

        words = len(text.split())
        if intent in FAST_INTENTS and words <= 15:
            complexity = "simple"
        elif words > 30 or _COMPLEX_HINT.search(text):
            complexity = "complex"
        else:
            complexity = "medium"

        return Decision(
            sensitive=bool(reasons) or score >= self.threshold,
            sensitive_score=round(score, 3),
            reasons=reasons,
            intent=intent,
            intent_score=round(intent_score, 3),
            complexity=complexity,
            embedder=self.embedder.name,
            latency_ms=round((time.perf_counter() - start) * 1000, 1),
        )


# ---------- Log ----------

class RouterLog:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS decisions ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, user_id TEXT NOT NULL,"
            " channel TEXT NOT NULL, text TEXT NOT NULL, private INTEGER NOT NULL,"
            " sensitive INTEGER NOT NULL, sensitive_score REAL NOT NULL, reasons TEXT NOT NULL,"
            " intent TEXT NOT NULL, intent_score REAL NOT NULL, complexity TEXT NOT NULL,"
            " embedder TEXT NOT NULL, latency_ms REAL NOT NULL,"
            " label INTEGER)"  # 1 = sensitive, 0 = harmless (corrected by hand)
        )
        # Answers of a second decider running in shadow (e.g. Laya via ollaya).
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS shadow ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id INTEGER NOT NULL,"
            " ts REAL NOT NULL, model TEXT NOT NULL, cloud INTEGER NOT NULL,"
            " sensitive_p REAL, intent TEXT, intent_p REAL, complexity TEXT,"
            " latency_ms REAL, error TEXT)"
        )
        self._db.commit()

    def add_shadow(self, decision_id: int, model: str, cloud: bool, sensitive_p: float | None,
                   intent: str | None, intent_p: float | None, complexity: str | None,
                   latency_ms: float | None, error: str | None = None) -> None:
        self._db.execute(
            "INSERT INTO shadow (decision_id, ts, model, cloud, sensitive_p, intent, intent_p,"
            " complexity, latency_ms, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (decision_id, time.time(), model, int(cloud), sensitive_p, intent, intent_p,
             complexity, latency_ms, error),
        )
        self._db.commit()

    def add(self, user_id: str, channel: str, text: str, private: bool, d: Decision,
            ts: float | None = None) -> int:
        cur = self._db.execute(
            "INSERT INTO decisions (ts, user_id, channel, text, private, sensitive,"
            " sensitive_score, reasons, intent, intent_score, complexity, embedder, latency_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts or time.time(), user_id, channel, text, int(private), int(d.sensitive),
             d.sensitive_score, json.dumps(d.reasons), d.intent, d.intent_score,
             d.complexity, d.embedder, d.latency_ms),
        )
        self._db.commit()
        return cur.lastrowid

    def label_last(self, user_id: str, sensitive: bool) -> str | None:
        """Sets the label of the person's last message. Returns its text."""
        row = self._db.execute(
            "SELECT id, text FROM decisions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
        ).fetchone()
        if row is None:
            return None
        self._db.execute("UPDATE decisions SET label = ? WHERE id = ?", (int(sensitive), row[0]))
        self._db.commit()
        return row[1]

    def stats(self, days: int = 30) -> str:
        since = time.time() - days * 86400
        q = lambda sql, *p: self._db.execute(sql, (since, *p)).fetchone()  # noqa: E731
        total = q("SELECT COUNT(*) FROM decisions WHERE ts >= ?")[0]
        if not total:
            return f"Router: no messages observed in the last {days} days."
        sens = q("SELECT COUNT(*) FROM decisions WHERE ts >= ? AND sensitive = 1")[0]
        rules = q("SELECT COUNT(*) FROM decisions WHERE ts >= ? AND reasons != '[]'")[0]
        priv, priv_hit = q("SELECT COUNT(*), COALESCE(SUM(sensitive), 0) FROM decisions"
                           " WHERE ts >= ? AND private = 1")
        lat = q("SELECT AVG(latency_ms) FROM decisions WHERE ts >= ?")[0]
        lines = [
            f"Router (last {days} days): {total} messages",
            f"- classed as sensitive: {sens} ({sens / total:.0%}), of which by rule: {rules}",
        ]
        if priv:
            lines.append(f"- sent in private mode: {priv}, of which detected: {priv_hit} ({priv_hit / priv:.0%})")

        tp, fp, fn, tn = (q("SELECT COUNT(*) FROM decisions WHERE ts >= ? AND label = ? AND sensitive = ?",
                            lab, pred)[0] for lab, pred in ((1, 1), (0, 1), (1, 0), (0, 0)))
        if tp + fp + fn + tn:
            precision = tp / (tp + fp) if tp + fp else 0.0
            recall = tp / (tp + fn) if tp + fn else 0.0
            lines.append(f"- corrected labels: {tp + fp + fn + tn} · precision {precision:.0%} · "
                         f"recall {recall:.0%} · missed: {fn}")

        intents = self._db.execute(
            "SELECT intent, COUNT(*) FROM decisions WHERE ts >= ? GROUP BY intent ORDER BY 2 DESC",
            (since,)).fetchall()
        lines.append("- intents: " + ", ".join(f"{i} {c}" for i, c in intents))
        comp = self._db.execute(
            "SELECT complexity, COUNT(*) FROM decisions WHERE ts >= ? GROUP BY complexity ORDER BY 2 DESC",
            (since,)).fetchall()
        lines.append("- complexity: " + ", ".join(f"{k} {c}" for k, c in comp))
        emb = self._db.execute("SELECT embedder FROM decisions ORDER BY id DESC LIMIT 1").fetchone()[0]
        lines.append(f"- model: {emb}, {lat:.0f} ms on average")
        lines += self._shadow_stats(since)
        return "\n".join(lines)

    def _shadow_stats(self, since: float) -> list[str]:
        rows = self._db.execute(
            "SELECT s.model, s.cloud, s.sensitive_p, s.intent, s.complexity, s.latency_ms, s.error,"
            " d.sensitive, d.private, d.label, d.intent, d.complexity"
            " FROM shadow s JOIN decisions d ON d.id = s.decision_id WHERE d.ts >= ?", (since,)
        ).fetchall()
        if not rows:
            return []
        ok = [r for r in rows if r[6] is None]
        model = rows[-1][0]
        out = [f"Shadow decider {model}{' (cloud)' if rows[-1][1] else ''}: "
               f"{len(rows)} requests, {len(rows) - len(ok)} errors"]
        if not ok:
            return out
        lat = sum(r[5] or 0 for r in ok) / len(ok)
        sens = [r for r in ok if r[2] is not None]
        if sens:
            agree = sum((r[2] >= 0.5) == bool(r[7]) for r in sens) / len(sens)
            out.append(f"- \"sensitive?\" same as local: {agree:.0%}")
            priv = [r for r in sens if r[8]]
            if priv:
                hit = sum(r[2] >= 0.5 for r in priv)
                out.append(f"- sent in private mode: {len(priv)}, of which detected: {hit} ({hit / len(priv):.0%})")
            labeled = [r for r in sens if r[9] is not None]
            if labeled:
                tp = sum(r[9] == 1 and r[2] >= 0.5 for r in labeled)
                fp = sum(r[9] == 0 and r[2] >= 0.5 for r in labeled)
                fn = sum(r[9] == 1 and r[2] < 0.5 for r in labeled)
                precision = tp / (tp + fp) if tp + fp else 0.0
                recall = tp / (tp + fn) if tp + fn else 0.0
                out.append(f"- on corrected labels: precision {precision:.0%} · recall {recall:.0%} · missed: {fn}")
        intent_agree = sum(r[3] == r[10] for r in ok) / len(ok)
        comp_agree = sum(r[4] == r[11] for r in ok) / len(ok)
        out.append(f"- same intent: {intent_agree:.0%} · same complexity: {comp_agree:.0%} · {lat:.0f} ms on average")
        return out

    def export_csv(self, path: Path, include_private: bool = False) -> int:
        # Per message the newest shadow answer (if any).
        rows = self._db.execute(
            "SELECT d.ts, d.user_id, d.channel, d.text, d.private, d.sensitive, d.sensitive_score,"
            " d.reasons, d.intent, d.intent_score, d.complexity, d.embedder, d.latency_ms, d.label,"
            " s.model, s.sensitive_p, s.intent, s.intent_p, s.complexity, s.latency_ms, s.error"
            " FROM decisions d LEFT JOIN shadow s ON s.id ="
            " (SELECT MAX(id) FROM shadow WHERE decision_id = d.id) ORDER BY d.id"
        ).fetchall()
        cols = ["ts", "user_id", "channel", "text", "private", "sensitive", "sensitive_score",
                "reasons", "intent", "intent_score", "complexity", "embedder", "latency_ms", "label",
                "shadow_model", "shadow_sensitive_p", "shadow_intent", "shadow_intent_p",
                "shadow_complexity", "shadow_latency_ms", "shadow_error"]
        count = 0
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(cols)
            for r in rows:
                if r[4] and not include_private:
                    r = (*r[:3], "", *r[4:])  # leave out the text of private messages
                writer.writerow(r)
                count += 1
        return count


# ---------- Observer ----------

class RouterObserver:
    """Classifies and logs. `observe` runs in the background and never changes the answer;
    `decide` waits for the decision (protect mode)."""

    def __init__(self, router: Router, log_store: RouterLog, shadow: "ShadowDecider | None" = None):
        self.router = router
        self.log = log_store
        self.shadow = shadow
        self._tasks: set[asyncio.Task] = set()

    def observe(self, user_id: str, channel: str, text: str, private: bool) -> None:
        self._spawn(self._observe(user_id, channel, text, private))

    async def decide(self, user_id: str, channel: str, text: str, private: bool) -> Decision | None:
        """Classifies and logs before the answer; None if the router failed (protect mode
        then treats the message as sensitive). The shadow decider runs in the background."""
        found = await self._classify(user_id, channel, text, private)
        if found is None:
            return None
        if self.shadow is not None:
            self._spawn(self._shadow(found[1], text, private, found[0]))
        return found[0]

    def _spawn(self, coro) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _classify(self, user_id: str, channel: str, text: str,
                        private: bool) -> tuple[Decision, int] | None:
        try:
            decision = await asyncio.to_thread(self.router.classify, text)
            return decision, self.log.add(user_id, channel, text, private, decision)
        except Exception:
            log.warning("Router classification failed", exc_info=True)
            return None

    async def _shadow(self, decision_id: int, text: str, private: bool, decision: Decision) -> None:
        try:
            await self.shadow.observe(self.log, decision_id, text, private, decision)
        except Exception:
            log.warning("Shadow decider failed", exc_info=True)

    async def _observe(self, user_id: str, channel: str, text: str, private: bool) -> None:
        found = await self._classify(user_id, channel, text, private)
        if found is not None and self.shadow is not None:
            await self._shadow(found[1], text, private, found[0])

    async def drain(self) -> None:
        """Waits for running observations (for tests and at shutdown)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


# ---------- Shadow decider (System One interface) ----------

INTENT_CRITERIA = {
    "shopping": "Shopping list: add, show or check off items",
    "reminder": "Reminders: create, list or delete a reminder",
    "waste": "Waste collection: pickup dates and which bin goes out",
    "time": "Current time or date",
    "web_search": "Current information from the web: opening hours, news, transport, weather, prices",
    "home": "Control the house: lights, heating, doors, alarm",
    "smalltalk": "Greetings, thanks, jokes, chit-chat",
    "knowledge": "General knowledge, explanations, homework help, advice",
    OTHER: "Anything else",
}
COMPLEXITY_LEVELS = ["simple", "medium", "complex"]
# Instructions in English: the decision models are trained on it; the message may be in any language.
QUESTIONS = {
    "sensitive": Noul(
        "Is this message about a sensitive private topic, such as physical or mental health, "
        "money, debts or salary, relationships or family conflicts, legal trouble, "
        "or passwords and other credentials?"),
    "intent": Choice("What does the person want?", INTENT_CRITERIA),
    "effort": Score(
        "How much effort does a good answer need?",
        ["simple: one quick fact or action",
         "medium: a short answer with some thought",
         "complex: an explanation, plan, comparison or several steps"]),
}

# Tailscale hands out addresses from 100.64.0.0/10.
_TAILSCALE = ipaddress.ip_network("100.64.0.0/10")


def is_local_url(url: str) -> bool:
    """Local = localhost, private networks, Tailscale or a Docker service name without a dot.
    Everything else counts as cloud, so in doubt the stricter handling applies."""
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return False
    if host == "localhost" or "." not in host:
        return True
    if host.endswith(".ts.net"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip in _TAILSCALE


class ShadowDecider:
    """Asks a second decider and logs its answers next to the router's own.

    If it runs in the cloud (e.g. TypeSafe Jev), it gets neither private-mode messages nor
    those the router finds sensitive, and it is never asked "sensitive?".
    """

    def __init__(self, client: SystemOneClient, cloud: bool | None = None):
        self.client = client
        self.cloud = (not is_local_url(client.base_url)) if cloud is None else cloud

    async def observe(self, log_store: RouterLog, decision_id: int, text: str,
                      private: bool, local: Decision) -> None:
        questions = dict(QUESTIONS)
        if self.cloud:
            if private or local.sensitive:
                return
            del questions["sensitive"]
        try:
            result = await self.client.decide(text, questions)
        except Exception as exc:
            log.warning("Shadow decider failed: %s", exc)
            log_store.add_shadow(decision_id, self.client.model, self.cloud, None, None, None,
                                 None, None, error=f"{type(exc).__name__}: {exc}"[:300])
            return
        a = result.answers
        sens = a.get("sensitive")
        intent = a.get("intent")
        effort = a.get("effort")
        complexity = None
        if effort is not None and effort.probabilities:
            level = str(max(effort.probabilities.items(), key=lambda kv: kv[1])[0])
            if level.isdigit():  # server without level names: index 0..2
                complexity = COMPLEXITY_LEVELS[min(2, int(level))]
            else:
                complexity = next((c for c in COMPLEXITY_LEVELS if level.lower().startswith(c)), None)
        elif effort is not None and effort.value is not None:
            complexity = COMPLEXITY_LEVELS[max(0, min(2, round(float(effort.value))))]
        log_store.add_shadow(
            decision_id, result.model, self.cloud,
            float(sens.value) if sens is not None and sens.value is not None else None,
            str(intent.value) if intent is not None and intent.value is not None else None,
            (intent.probabilities.get(str(intent.value)) or intent.confidence)
            if intent is not None else None,
            complexity, result.latency_ms,
        )


# ---------- Command line ----------

def _main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "test":
        router = Router(FastEmbedder())
        print(json.dumps(asdict(router.classify(" ".join(argv[1:]))), ensure_ascii=False, indent=2))
        return 0
    if len(argv) >= 3 and argv[0] == "export":
        n = RouterLog(Path(argv[1])).export_csv(Path(argv[2]), include_private="--with-private" in argv)
        print(f"{n} rows written to {argv[2]}")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
