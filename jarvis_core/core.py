"""The Jarvis core: independent of the channel (Telegram, Slack, Alexa, app, Home Assistant).

A channel finds the person, calls `handle` and shows the `Reply`. For a confirmation
question it collects the answer and calls `take_pending` and then `resume`.

Protect mode (`protect=True`, needs a router): every message is classified by the local
router before any model sees it. A message the router finds sensitive, or one it cannot
classify, is answered like private mode: by the local model, with the private memory and
journal, never by a cloud model. Everything else goes to Claude as usual.
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

# From this many days of silence on, the answer starts with a re-entry.
REENTRY_DAYS = 3

PRIVATE_SYSTEM = (
    "You are Jarvis, {persona}, talking to {user}. This conversation is confidential "
    "and runs on a local model. Answer briefly in {language}."
)
FALLBACK_SYSTEM = (
    "You are Jarvis, {persona}, talking to {user}. Right now you are the fallback model "
    "and have no tools: no wiki, no journal, no actions. Say so honestly when a question "
    "would need them. Answer briefly in {language}."
)
FALLBACK_NOTE = "\n\n(fallback model without tools)"
PROTECT_NOTE = "\n\n(sensitive topic: answered by the local model)"


@dataclass
class Reply:
    text: str = ""
    confirm_token: str | None = None  # set when a confirmation is needed
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
                 router=None, protect: bool = False):
        if protect and router is None:
            raise ValueError("Protect mode needs a router.")
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
        # A faster model for voice channels (Alexa expects an answer within ~8 s).
        self.voice_brain = voice_brain or brain
        # Router (router.py): only observes, unless protect mode lets it decide.
        self.router = router
        self.protect = protect

    # ---------- Helpers ----------

    def today(self) -> str:
        return datetime.now(self.tz).strftime("%Y-%m-%d")

    def record_usage(self, user_id: str, provider: str, model: str,
                     input_tokens: int, output_tokens: int) -> None:
        if input_tokens or output_tokens:
            self.state.record_usage(self.today(), user_id, provider, model,
                                    input_tokens, output_tokens)

    # All unknown voices share the guest. Without history nobody can ask what another
    # person asked before.
    def _history(self, user: User) -> list[dict]:
        return [] if user.role is Role.GUEST else self.memory.history(user.id)

    def _remember(self, user: User, user_text: str, reply: str) -> None:
        if user.role is not Role.GUEST:
            self.memory.add_exchange(user.id, user_text, reply)

    def _reentry_note(self, user: User) -> str:
        """After a longer break the answer starts with a re-entry (chief-of-staff-2)."""
        last = self.state.last_contact(user.id)
        if last is None:
            return ""
        days = int((time.time() - last) // 86400)
        if days < REENTRY_DAYS:
            return ""
        since = datetime.fromtimestamp(last, self.tz).strftime("%Y-%m-%d")
        return (f"- {user.name} has not been here for {days} days (last time on {since}). Start "
                "with a short re-entry: what has changed, what is urgent now and exactly one "
                "next step. Only then the actual question.\n")

    def _over_budget(self, provider: str) -> bool:
        return (self.daily_token_budget > 0
                and self.state.tokens_on(self.today(), provider) >= self.daily_token_budget)

    # ---------- Commands ----------

    def is_private(self, user: User) -> bool:
        return self.state.is_private(user.id)

    def toggle_private(self, user: User) -> str:
        if self.state.is_private(user.id):
            self.state.set_private(user.id, False)
            return "Private mode off. Messages go to Claude again."
        self.state.set_private(user.id, True)
        msg = "Private mode on. Messages go to the local model and not into the wiki."
        if self.local_llm is None:
            msg += "\n\nNote: no local model is set up yet, so private mode cannot answer until it is."
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
            lines = [f"No usage yet today ({day})."]
        else:
            lines = [f"Usage today ({day}):"]
            lines += [f"- {p}: {i:,} input and {o:,} output tokens" for p, i, o in rows]
        if self.daily_token_budget > 0:
            used = self.state.tokens_on(day, self.brain.provider)
            lines.append(f"Daily budget for Claude: {used:,} of {self.daily_token_budget:,} tokens")
        return "\n".join(lines)

    # ---------- Messages ----------

    async def handle(self, user: User, text: str, channel: str, voice: bool = False) -> Reply:
        """voice=True: the answer is read aloud (short, no formatting, fast model)."""
        private = self.state.is_private(user.id)
        protected = False
        if self.router is not None:
            if self.protect and not private:
                decision = await self.router.decide(user.id, channel, text, private)
                # Could not classify: treat it as sensitive (when in doubt, local).
                protected = decision is None or decision.sensitive
            else:
                self.router.observe(user.id, channel, text, private)
        # Re-entry only for adults and only by text: voice stays short and does not count as
        # a return, otherwise the hint would be used up by the first Alexa sentence.
        note = ""
        if not voice and user.role in ADULTS:
            note = self._reentry_note(user)
            self.state.touch(user.id)
        if private:
            answer, _ = await self._answer_locally(
                user, text, f"{channel} · private",
                unavailable="Private mode is not set up yet (no local model). "
                            "Switch private mode off to go back to Claude.")
            return Reply(answer)

        # A new message discards an open confirmation question.
        self.state.clear_pending(user.id)
        if protected:
            answer, ok = await self._answer_locally(
                user, text, f"{channel} · protected",
                unavailable="This looks like a sensitive topic, so it stays away from cloud "
                            "models, but no local model is set up yet.")
            return Reply(answer + PROTECT_NOTE if ok else answer)
        if self._over_budget(self.brain.provider):
            return await self._handle_fallback(
                user, text, channel, "The daily budget for Claude is used up. It resets tomorrow.")
        brain = self.voice_brain if voice else self.brain
        try:
            result = await brain.respond(self._history(user), text, user, voice=voice, note=note)
        except Exception:
            log.exception("Error with Claude")
            return await self._handle_fallback(
                user, text, channel, "Something went wrong. Please try again in a moment.")
        return self._deliver(user, text, channel, result, brain)

    def take_pending(self, user: User, token: str) -> PendingEntry | None:
        """Returns the pending action (and removes it), or None if it has expired."""
        entry = self.state.pop_pending(user.id, token)
        return PendingEntry(*entry) if entry else None

    async def resume(self, user: User, entry: PendingEntry, approved: bool) -> Reply:
        try:
            result = await self.brain.resume(entry.action, approved, user)
        except Exception:
            log.exception("Error after confirmation")
            return Reply("Something went wrong.")
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
            log.exception("Error with the fallback model")
            return Reply(error_text)
        self.record_usage(user.id, self.fallback.name, self.fallback.model,
                          reply.input_tokens, reply.output_tokens)
        self._remember(user, text, reply.text)
        self.journals.for_user(user).log_exchange(f"{channel} · {self.fallback.name}", text, reply.text)
        return Reply(reply.text + FALLBACK_NOTE)

    async def _answer_locally(self, user: User, text: str, channel: str,
                              unavailable: str) -> tuple[str, bool]:
        """Private mode and protected messages: local model, private memory and journal.
        Returns the text and whether the local model answered.

        Never falls back to a cloud model: better no answer than a leak. Guests get no
        history and leave none, like with Claude."""
        if self.local_llm is None:
            return unavailable, False
        system = PRIVATE_SYSTEM.format(persona=self.brain.persona, user=user.name,
                                       language=user.language)
        guest = user.role is Role.GUEST
        history = [] if guest else self.private_memory.history(user.id)
        try:
            reply = await self.local_llm.complete(system, history, text)
        except Exception:
            log.exception("Error with the local model")
            return "The local model cannot be reached right now. Please try again later.", False
        if not guest:
            self.private_memory.add_exchange(user.id, text, reply.text)
        self.private_journals.for_user(user).log_exchange(channel, text, reply.text)
        return reply.text, True
