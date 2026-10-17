"""Der Jarvis-Kern: unabhängig vom Zugang (Telegram, später Alexa, App, Home Assistant).

Ein Zugang ermittelt die Person, ruft `handle` auf und zeigt die `Reply` an.
Bei einer Bestätigungsfrage holt er die Antwort ein und ruft `take_pending`
und danach `resume` auf.
"""

import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .brain import Brain, BrainResult, PendingAction
from .journal import JournalStore
from .llm import ChatProvider
from .memory import ShortTermMemory
from .state import StateStore
from .users import ADULTS, Role, User

log = logging.getLogger(__name__)

# Ab so vielen Tagen Pause beginnt die Antwort mit einem Wiedereinstieg.
REENTRY_DAYS = 3

PRIVATE_SYSTEM = (
    "Du bist Jarvis, {persona}, und sprichst mit {user}. Dieses "
    "Gespräch ist vertraulich und läuft auf einem lokalen Modell. Antworte knapp auf {language}."
)
FALLBACK_SYSTEM = (
    "Du bist Jarvis, {persona}, und sprichst mit {user}. Du bist gerade "
    "das Ausweichmodell und hast keine Werkzeuge: kein Wiki, kein Journal, keine Haussteuerung. "
    "Sag das ehrlich, wenn eine Frage sie bräuchte. Antworte knapp auf {language}."
)
FALLBACK_NOTE = "\n\n(Ausweichmodell ohne Werkzeuge)"


@dataclass
class Reply:
    text: str = ""
    confirm_token: str | None = None  # gesetzt, wenn eine Bestätigung nötig ist
    question: str | None = None


@dataclass
class PendingEntry:
    channel: str
    action: PendingAction


class Jarvis:
    def __init__(self, brain: Brain, state: StateStore, memory: ShortTermMemory,
                 private_memory: ShortTermMemory, journals: JournalStore,
                 private_journals: JournalStore, timezone: str,
                 local_llm: ChatProvider | None = None,
                 fallback: ChatProvider | None = None,
                 daily_token_budget: int = 0,
                 voice_brain: Brain | None = None,
                 router=None):
        self.brain = brain
        self.state = state
        self.memory = memory
        self.private_memory = private_memory
        self.journals = journals
        self.private_journals = private_journals
        self.tz = ZoneInfo(timezone)
        self.local_llm = local_llm
        self.fallback = fallback
        self.daily_token_budget = daily_token_budget
        # Schnelleres Modell für Sprachzugänge (Alexa erwartet eine Antwort in ~8 s).
        self.voice_brain = voice_brain or brain
        # Router im Beobachtungsmodus (router.py): entscheidet mit, greift aber nicht ein.
        self.router = router

    # ---------- Hilfen ----------

    def today(self) -> str:
        return datetime.now(self.tz).strftime("%Y-%m-%d")

    def record_usage(self, user_id: str, provider: str, model: str,
                     input_tokens: int, output_tokens: int) -> None:
        if input_tokens or output_tokens:
            self.state.record_usage(self.today(), user_id, provider, model,
                                    input_tokens, output_tokens)

    # Alle unbekannten Stimmen teilen sich den Gast. Ohne Verlauf kann niemand
    # erfragen, was eine andere Person vorher gefragt hat.
    def _history(self, user: User) -> list[dict]:
        return [] if user.role is Role.GAST else self.memory.history(user.id)

    def _remember(self, user: User, user_text: str, reply: str) -> None:
        if user.role is not Role.GAST:
            self.memory.add_exchange(user.id, user_text, reply)

    def _reentry_note(self, user: User) -> str:
        """Nach längerer Pause beginnt die Antwort mit einem Wiedereinstieg (chief-of-staff-2)."""
        last = self.state.last_contact(user.id)
        if last is None:
            return ""
        days = int((time.time() - last) // 86400)
        if days < REENTRY_DAYS:
            return ""
        since = datetime.fromtimestamp(last, self.tz).strftime("%d.%m.%Y")
        return (f"- {user.name} war {days} Tage nicht hier (zuletzt am {since}). Beginne mit einem "
                "kurzen Wiedereinstieg: was sich geändert hat, was jetzt dringend ist und genau "
                "ein nächster Schritt. Erst danach die eigentliche Frage.\n")

    def _over_budget(self, provider: str) -> bool:
        return (self.daily_token_budget > 0
                and self.state.tokens_on(self.today(), provider) >= self.daily_token_budget)

    # ---------- Befehle ----------

    def is_private(self, user: User) -> bool:
        return self.state.is_private(user.id)

    def toggle_private(self, user: User) -> str:
        if self.state.is_private(user.id):
            self.state.set_private(user.id, False)
            return "Privatmodus aus. Ab jetzt wieder Claude."
        self.state.set_private(user.id, True)
        msg = "Privatmodus an. Nachrichten gehen an das lokale Modell und nicht ins Wiki."
        if self.local_llm is None:
            msg += "\n\nHinweis: LOCAL_LLM_URL ist noch nicht gesetzt, der Privatmodus antwortet erst danach."
        return msg

    def cancel_pending(self, user: User) -> None:
        self.state.clear_pending(user.id)

    def reset(self, user: User) -> None:
        self.memory.clear(user.id)
        self.private_memory.clear(user.id)
        self.state.clear_pending(user.id)

    def usage_report(self) -> str:
        day = self.today()
        rows = self.state.usage_on(day)
        if not rows:
            lines = [f"Heute ({day}) noch kein Verbrauch."]
        else:
            lines = [f"Verbrauch heute ({day}):"]
            lines += [f"- {p}: {i:,} Eingabe- und {o:,} Ausgabe-Tokens".replace(",", ".")
                      for p, i, o in rows]
        if self.daily_token_budget > 0:
            used = self.state.tokens_on(day, self.brain.provider)
            lines.append(f"Tagesbudget Claude: {used:,} von {self.daily_token_budget:,} Tokens"
                         .replace(",", "."))
        return "\n".join(lines)

    # ---------- Nachrichten ----------

    async def handle(self, user: User, text: str, channel: str, voice: bool = False) -> Reply:
        """voice=True: Antwort wird vorgelesen (kurz, ohne Formatierung, schnelles Modell)."""
        private = self.state.is_private(user.id)
        if self.router is not None:
            self.router.observe(user.id, channel, text, private)
        # Wiedereinstieg nur für Erwachsene und nur per Text: Sprache bleibt kurz und zählt
        # nicht als Rückkehr, sonst ginge der Hinweis beim ersten Alexa-Satz verloren.
        note = ""
        if not voice and user.role in ADULTS:
            note = self._reentry_note(user)
            self.state.touch(user.id)
        if private:
            return await self._handle_private(user, text, channel)

        # Eine neue Nachricht verwirft eine offene Bestätigungsfrage.
        self.state.clear_pending(user.id)
        if self._over_budget(self.brain.provider):
            return await self._handle_fallback(
                user, text, channel,
                "Das Tagesbudget für Claude ist aufgebraucht. Morgen geht es weiter.")
        brain = self.voice_brain if voice else self.brain
        try:
            result = await brain.respond(self._history(user), text, user, voice=voice, note=note)
        except Exception:
            log.exception("Fehler bei Claude")
            return await self._handle_fallback(
                user, text, channel, "Da ist etwas schiefgegangen. Versuch es gleich noch einmal.")
        return self._deliver(user, text, channel, result, brain)

    def take_pending(self, user: User, token: str) -> PendingEntry | None:
        """Gibt die offene Aktion zurück (und entfernt sie), oder None, wenn abgelaufen."""
        entry = self.state.pop_pending(user.id, token)
        return PendingEntry(*entry) if entry else None

    async def resume(self, user: User, entry: PendingEntry, approved: bool) -> Reply:
        try:
            result = await self.brain.resume(entry.action, approved, user)
        except Exception:
            log.exception("Fehler nach Bestätigung")
            return Reply("Da ist etwas schiefgegangen.")
        return self._deliver(user, entry.action.user_text, entry.channel, result, self.brain)

    def _deliver(self, user: User, user_text: str, channel: str, result: BrainResult,
                 brain: Brain) -> Reply:
        self.record_usage(user.id, brain.provider, brain.model,
                          result.usage.input_tokens, result.usage.output_tokens)
        if result.pending is not None:
            token = secrets.token_hex(4)
            self.state.put_pending(user.id, token, channel, result.pending)
            return Reply(confirm_token=token, question=result.pending.question)

        self._remember(user, user_text, result.text)
        self.journals.for_user(user).log_exchange(channel, user_text, result.text)
        return Reply(result.text)

    async def _handle_fallback(self, user: User, text: str, channel: str, error_text: str) -> Reply:
        if self.fallback is None or self._over_budget(self.fallback.name):
            return Reply(error_text)
        system = FALLBACK_SYSTEM.format(persona=self.brain.persona, user=user.name,
                                        language=user.language)
        try:
            reply = await self.fallback.complete(system, self._history(user), text)
        except Exception:
            log.exception("Fehler beim Ausweichmodell")
            return Reply(error_text)
        self.record_usage(user.id, self.fallback.name, self.fallback.model,
                          reply.input_tokens, reply.output_tokens)
        self._remember(user, text, reply.text)
        self.journals.for_user(user).log_exchange(f"{channel} · {self.fallback.name}", text, reply.text)
        return Reply(reply.text + FALLBACK_NOTE)

    async def _handle_private(self, user: User, text: str, channel: str) -> Reply:
        # Kein Ausweichen auf Cloud-Modelle: lieber keine Antwort als ein Datenleck.
        if self.local_llm is None:
            return Reply("Der Privatmodus ist noch nicht eingerichtet (LOCAL_LLM_URL fehlt). "
                         "Mit /privat schaltest du zurück zu Claude.")
        system = PRIVATE_SYSTEM.format(persona=self.brain.persona, user=user.name,
                                       language=user.language)
        try:
            reply = await self.local_llm.complete(system, self.private_memory.history(user.id), text)
        except Exception:
            log.exception("Fehler beim lokalen Modell")
            return Reply("Das lokale Modell ist gerade nicht erreichbar. "
                         "Ist der DGX Spark an und der Tunnel aktiv?")
        self.private_memory.add_exchange(user.id, text, reply.text)
        self.private_journals.for_user(user).log_exchange(f"{channel} · privat", text, reply.text)
        return Reply(reply.text)
