"""Router im Beobachtungsmodus.

Entscheidet für jede Nachricht, ohne einzugreifen:
- sensibel?  (Gesundheit, Finanzen, Beziehung, Recht, Zugangsdaten …) → später lokal
- Absicht    (einkauf, erinnerung, muell, …)                          → später Schnellpfad
- Aufwand    (einfach / mittel / komplex)                              → später kleineres Modell

Alles läuft lokal auf dem Server: Schlüsselwort-Regeln plus ein kleines mehrsprachiges
Embedding-Modell (ONNX über fastembed, kein PyTorch) mit nächsten Nachbarn auf
Beispielsätzen. Die Entscheidungen landen in data/router.sqlite und lassen sich mit
/router auswerten, mit „/router ja|nein“ korrigieren und als CSV exportieren:

    python -m jarvis.router test "Ich habe Rückenschmerzen"
    python -m jarvis.router export /data/router.sqlite router.csv [--mit-privat]
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

# Treffer hier zählen immer als sensibel – unabhängig vom Modell.
# "stamm": darf in zusammengesetzten Wörtern stehen (Rücken-schmerzen, Haus-arzt, Konto-auszug).
# "wort": nur als ganzes Wort, weil kurz oder mehrdeutig (PIN ≠ Pinguin, Steuer ≠ Heizungssteuerung).
SENSITIVE_RULES = {
    "gesundheit": {
        "stamm": ["arzt", "ärzt", "diagnose", "krank", "schmerz", "therapi", "therapeut",
                  "medikament", "tablette", "depress", "psych", "blutzucker", "blutdruck",
                  "schwanger", "krebs", "demenz", "klinik", "krankenhaus", "operation", "symptom"],
        "wort": [],
    },
    "finanzen": {
        "stamm": ["gehalt", "konto", "schulden", "kredit", "überweisung", "insolvenz",
                  "pfändung", "steuererklärung", "steuerbescheid", "steuerberat", "nachzahlung"],
        "wort": ["iban", "mahnung", "steuer", "steuern"],
    },
    "zugangsdaten": {
        "stamm": ["passwort", "kennwort", "zugangsdaten"],
        "wort": ["pin", "tan", "login"],
    },
    "beziehung": {
        "stamm": ["trennung", "scheidung", "affäre", "seitensprung"],
        "wort": ["trennen", "streit", "streiten", "gestritten"],
    },
    "recht": {
        "stamm": ["anwalt", "anwält", "gericht", "abmahnung", "jugendamt", "kündigung"],
        "wort": ["polizei", "anzeige"],
    },
}


def _compile_rule(rule: dict) -> re.Pattern:
    parts = [rf"\w*{re.escape(s)}\w*" for s in rule["stamm"]]
    parts += [rf"\b{re.escape(w)}\b" for w in rule["wort"]]
    return re.compile("|".join(parts), re.IGNORECASE)


_RULES = {name: _compile_rule(rule) for name, rule in SENSITIVE_RULES.items()}

FAST_INTENTS = {"einkauf", "erinnerung", "muell", "zeit", "smalltalk", "haus"}
_COMPLEX_HINT = re.compile(r"\b(erklär\w*|vergleich\w*|plane?n?|analysier\w*|schreib\w* (mir )?"
                           r"(einen|eine|ein)|zusammenfass\w*|warum|begründ\w*)\b", re.IGNORECASE)


# ---------- Embeddings ----------

class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> np.ndarray: ...


class HashEmbedder:
    """Zeichen-3-Gramme, gehasht. Braucht keinen Download – Rückfall und für Tests."""

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
    """Mehrsprachiges Satz-Embedding über fastembed (ONNX). Lädt das Modell beim ersten Aufruf."""

    def __init__(self, model: str = DEFAULT_MODEL, cache_dir: str | None = None):
        self.name = model
        self.cache_dir = cache_dir
        self._model = None

    def embed(self, texts: list[str]) -> np.ndarray:
        if self._model is None:
            from fastembed import TextEmbedding  # erst bei Bedarf laden
            self._model = TextEmbedding(self.name, cache_dir=self.cache_dir)
        return np.array(list(self._model.embed(texts)), dtype=np.float32)


def _normalize(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.where(norms == 0, 1, norms)


# ---------- Entscheidung ----------

@dataclass
class Decision:
    sensitive: bool
    sensitive_score: float          # 0..1 aus dem Modell
    reasons: list[str] = field(default_factory=list)  # getroffene Regeln
    intent: str = "sonstiges"
    intent_score: float = 0.0
    complexity: str = "mittel"
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
        # Vorladen beim Start und erste Nachricht laufen in verschiedenen Threads.
        self._lock = threading.Lock()

    def _prepare(self) -> None:
        with self._lock:
            if self._seeds is not None:
                return
            try:
                self._seeds = self._embed_seeds(self.embedder)
            except Exception:
                log.warning("Router: Embedding-Modell %s nicht ladbar, nutze %s",
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
        # Abstand der Ähnlichkeiten in eine Wahrscheinlichkeit übersetzen (Temperatur 0,05).
        score = float(1.0 / (1.0 + np.exp(-(s - n) / 0.05)))
        reasons = sorted(name for name, rx in _RULES.items() if rx.search(text))

        intent_scores = {name: float(np.max(m @ vec)) for name, m in self._seeds["intents"].items()}
        intent, intent_score = max(intent_scores.items(), key=lambda kv: kv[1])
        if intent_score < 0.35:
            intent = "sonstiges"

        words = len(text.split())
        if intent in FAST_INTENTS and words <= 15:
            complexity = "einfach"
        elif words > 30 or _COMPLEX_HINT.search(text):
            complexity = "komplex"
        else:
            complexity = "mittel"

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


# ---------- Protokoll ----------

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
            " label INTEGER)"  # 1 = sensibel, 0 = unkritisch (von Hand korrigiert)
        )
        # Antworten eines zweiten Entscheiders im Schattenbetrieb (z. B. Laya über ollaya).
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
        """Setzt das Label der letzten Nachricht der Person. Gibt deren Text zurück."""
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
            return f"Router: in den letzten {days} Tagen noch keine Nachrichten beobachtet."
        sens = q("SELECT COUNT(*) FROM decisions WHERE ts >= ? AND sensitive = 1")[0]
        rules = q("SELECT COUNT(*) FROM decisions WHERE ts >= ? AND reasons != '[]'")[0]
        priv, priv_hit = q("SELECT COUNT(*), COALESCE(SUM(sensitive), 0) FROM decisions"
                           " WHERE ts >= ? AND private = 1")
        lat = q("SELECT AVG(latency_ms) FROM decisions WHERE ts >= ?")[0]
        lines = [
            f"Router (Beobachtung, letzte {days} Tage): {total} Nachrichten",
            f"- als sensibel eingestuft: {sens} ({sens / total:.0%}), davon per Regel: {rules}",
        ]
        if priv:
            lines.append(f"- im Privatmodus gesendet: {priv}, davon erkannt: {priv_hit} ({priv_hit / priv:.0%})")

        tp, fp, fn, tn = (q("SELECT COUNT(*) FROM decisions WHERE ts >= ? AND label = ? AND sensitive = ?",
                            lab, pred)[0] for lab, pred in ((1, 1), (0, 1), (1, 0), (0, 0)))
        if tp + fp + fn + tn:
            precision = tp / (tp + fp) if tp + fp else 0.0
            recall = tp / (tp + fn) if tp + fn else 0.0
            lines.append(f"- korrigierte Labels: {tp + fp + fn + tn} · Präzision {precision:.0%} · "
                         f"Recall {recall:.0%} · übersehen: {fn}")

        intents = self._db.execute(
            "SELECT intent, COUNT(*) FROM decisions WHERE ts >= ? GROUP BY intent ORDER BY 2 DESC",
            (since,)).fetchall()
        lines.append("- Absichten: " + ", ".join(f"{i} {c}" for i, c in intents))
        comp = self._db.execute(
            "SELECT complexity, COUNT(*) FROM decisions WHERE ts >= ? GROUP BY complexity ORDER BY 2 DESC",
            (since,)).fetchall()
        lines.append("- Aufwand: " + ", ".join(f"{k} {c}" for k, c in comp))
        emb = self._db.execute("SELECT embedder FROM decisions ORDER BY id DESC LIMIT 1").fetchone()[0]
        lines.append(f"- Modell: {emb}, im Schnitt {lat:.0f} ms")
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
        out = [f"Schatten-Entscheider {model}{' (Cloud)' if rows[-1][1] else ''}: "
               f"{len(rows)} Anfragen, {len(rows) - len(ok)} Fehler"]
        if not ok:
            return out
        lat = sum(r[5] or 0 for r in ok) / len(ok)
        sens = [r for r in ok if r[2] is not None]
        if sens:
            agree = sum((r[2] >= 0.5) == bool(r[7]) for r in sens) / len(sens)
            out.append(f"- „sensibel?“ gleich wie lokal: {agree:.0%}")
            priv = [r for r in sens if r[8]]
            if priv:
                hit = sum(r[2] >= 0.5 for r in priv)
                out.append(f"- im Privatmodus gesendet: {len(priv)}, davon erkannt: {hit} ({hit / len(priv):.0%})")
            labeled = [r for r in sens if r[9] is not None]
            if labeled:
                tp = sum(r[9] == 1 and r[2] >= 0.5 for r in labeled)
                fp = sum(r[9] == 0 and r[2] >= 0.5 for r in labeled)
                fn = sum(r[9] == 1 and r[2] < 0.5 for r in labeled)
                precision = tp / (tp + fp) if tp + fp else 0.0
                recall = tp / (tp + fn) if tp + fn else 0.0
                out.append(f"- auf korrigierten Labels: Präzision {precision:.0%} · Recall {recall:.0%} · übersehen: {fn}")
        intent_agree = sum(r[3] == r[10] for r in ok) / len(ok)
        comp_agree = sum(r[4] == r[11] for r in ok) / len(ok)
        out.append(f"- Absicht gleich: {intent_agree:.0%} · Aufwand gleich: {comp_agree:.0%} · im Schnitt {lat:.0f} ms")
        return out

    def export_csv(self, path: Path, include_private: bool = False) -> int:
        # Pro Nachricht die jüngste Schatten-Antwort dazu (falls vorhanden).
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
                    r = (*r[:3], "", *r[4:])  # Text privater Nachrichten weglassen
                writer.writerow(r)
                count += 1
        return count


# ---------- Beobachter ----------

class RouterObserver:
    """Klassifiziert im Hintergrund und protokolliert. Greift nie in die Antwort ein."""

    def __init__(self, router: Router, log_store: RouterLog, shadow: "ShadowDecider | None" = None):
        self.router = router
        self.log = log_store
        self.shadow = shadow
        self._tasks: set[asyncio.Task] = set()

    def observe(self, user_id: str, channel: str, text: str, private: bool) -> None:
        try:
            task = asyncio.get_running_loop().create_task(
                self._observe(user_id, channel, text, private))
        except RuntimeError:
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _observe(self, user_id: str, channel: str, text: str, private: bool) -> Decision | None:
        try:
            decision = await asyncio.to_thread(self.router.classify, text)
            decision_id = self.log.add(user_id, channel, text, private, decision)
            if self.shadow is not None:
                await self.shadow.observe(self.log, decision_id, text, private, decision)
            return decision
        except Exception:
            log.warning("Router-Beobachtung fehlgeschlagen", exc_info=True)
            return None

    async def drain(self) -> None:
        """Wartet auf laufende Beobachtungen (für Tests und beim Herunterfahren)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


# ---------- Schatten-Entscheider (System-One-Schnittstelle) ----------

INTENT_CRITERIA = {
    "einkauf": "Shopping list: add, show or check off items",
    "erinnerung": "Reminders: create, list or delete a reminder",
    "muell": "Waste collection: pickup dates and which bin goes out",
    "zeit": "Current time or date",
    "websuche": "Current information from the web: opening hours, news, transport, weather, prices",
    "haus": "Control the house: lights, heating, doors, alarm",
    "smalltalk": "Greetings, thanks, jokes, chit-chat",
    "wissen": "General knowledge, explanations, homework help, advice",
    "sonstiges": "Anything else",
}
COMPLEXITY_LEVELS = ["einfach", "mittel", "komplex"]
# Anweisungen auf Englisch: darauf sind die Entscheidungsmodelle trainiert; der Text darf deutsch sein.
QUESTIONS = {
    "sensibel": Noul(
        "Is this message about a sensitive private topic, such as physical or mental health, "
        "money, debts or salary, relationships or family conflicts, legal trouble, "
        "or passwords and other credentials?"),
    "absicht": Choice("What does the person want?", INTENT_CRITERIA),
    "aufwand": Score(
        "How much effort does a good answer need?",
        ["simple: one quick fact or action",
         "medium: a short answer with some thought",
         "complex: an explanation, plan, comparison or several steps"]),
}

# Tailscale vergibt Adressen aus 100.64.0.0/10.
_TAILSCALE = ipaddress.ip_network("100.64.0.0/10")


def is_local_url(url: str) -> bool:
    """Lokal = localhost, private Netze, Tailscale oder Docker-Dienstname ohne Punkt.
    Alles andere gilt als Cloud – im Zweifel also die strengere Behandlung."""
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
    """Fragt einen zweiten Entscheider und protokolliert seine Antworten neben der eigenen.

    Läuft er in der Cloud (z. B. TypeSafe Jev), bekommt er weder Privatmodus-Nachrichten
    noch solche, die lokal als sensibel gelten, und wird nie nach „sensibel?“ gefragt.
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
            del questions["sensibel"]
        try:
            result = await self.client.decide(text, questions)
        except Exception as exc:
            log.warning("Schatten-Entscheider fehlgeschlagen: %s", exc)
            log_store.add_shadow(decision_id, self.client.model, self.cloud, None, None, None,
                                 None, None, error=f"{type(exc).__name__}: {exc}"[:300])
            return
        a = result.answers
        sens = a.get("sensibel")
        intent = a.get("absicht")
        effort = a.get("aufwand")
        complexity = None
        if effort is not None and effort.probabilities:
            level = str(max(effort.probabilities.items(), key=lambda kv: kv[1])[0])
            if level.isdigit():  # Server ohne Stufennamen: Index 0..2
                complexity = COMPLEXITY_LEVELS[min(2, int(level))]
            else:
                prefixes = {"simple": "einfach", "medium": "mittel", "complex": "komplex"}
                complexity = next((v for k, v in prefixes.items() if level.lower().startswith(k)), None)
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


# ---------- Kommandozeile ----------

def _main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "test":
        router = Router(FastEmbedder())
        print(json.dumps(asdict(router.classify(" ".join(argv[1:]))), ensure_ascii=False, indent=2))
        return 0
    if len(argv) >= 3 and argv[0] == "export":
        n = RouterLog(Path(argv[1])).export_csv(Path(argv[2]), include_private="--mit-privat" in argv)
        print(f"{n} Zeilen nach {argv[2]} geschrieben")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
