"""Werkzeuge, die Claude aufrufen kann.

Ein Werkzeug mit needs_confirmation=True wird erst ausgeführt, wenn die Person
in Telegram auf "Ja" tippt. Später gehören dazu z. B. Tür öffnen, Mail senden.
Über `roles` legt jedes Werkzeug fest, welche Rollen es überhaupt benutzen dürfen.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from .chief import PROFILE_FIELDS, ChiefStore, urgency
from .journal import JournalStore
from .skills import SkillLibrary
from .users import ADULTS, ALL_MEMBERS, Role, User


@dataclass(frozen=True)
class ToolContext:
    """Wer das Werkzeug gerade benutzt."""
    user: User


Handler = Callable[[dict, ToolContext], Awaitable[str]]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Handler
    needs_confirmation: bool = False
    roles: frozenset[Role] = ALL_MEMBERS
    describer: Callable[[dict], str] | None = None  # lesbare Bestätigungsfrage

    def allowed(self, role: Role) -> bool:
        return role in self.roles

    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def describe_call(self, args: dict) -> str:
        """Kurzer Text für die Bestätigungsfrage."""
        if self.describer is not None:
            return self.describer(args)
        details = ", ".join(f"{k}={v!r}" for k, v in args.items())
        return f"{self.name}({details})"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def schemas(self, role: Role | None = None) -> list[dict]:
        return [t.schema() for t in self._tools.values() if role is None or t.allowed(role)]

    async def run(self, name: str, args: dict, ctx: ToolContext) -> str:
        tool = self.get(name)
        if tool is None:
            return f"Unbekanntes Werkzeug: {name}"
        if not tool.allowed(ctx.user.role):
            return f"{ctx.user.name} darf das Werkzeug {name} nicht benutzen."
        try:
            return await tool.handler(args, ctx)
        except Exception as exc:  # Fehler an Claude zurückgeben statt abzustürzen
            return f"Fehler in {name}: {exc}"


def search_markdown(roots: Path | list[Path], query: str, max_results: int = 5) -> str:
    """Einfache Volltextsuche über Markdown-Dateien.

    Bewertet jede Datei nach der Zahl der Treffer aller Suchwörter und
    gibt pro Treffer-Datei die passendsten Zeilen zurück.
    """
    roots = [roots] if isinstance(roots, Path) else roots
    terms = [t.lower() for t in re.findall(r"\w+", query) if len(t) > 2]
    if not terms:
        return "Bitte konkretere Suchbegriffe angeben."
    existing = [r for r in roots if r.exists()]
    if not existing:
        return "Noch keine Daten vorhanden."

    scored = []
    for root in existing:
        for path in root.rglob("*.md"):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            lower = text.lower()
            score = sum(lower.count(t) for t in terms)
            if score:
                rel = path.relative_to(root)
                label = f"{root.name}/{rel}" if len(roots) > 1 else str(rel)
                scored.append((score, label, text))

    if not scored:
        return "Keine Treffer."

    scored.sort(key=lambda item: item[0], reverse=True)
    parts = []
    for score, label, text in scored[:max_results]:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        hits = [ln for ln in lines if any(t in ln.lower() for t in terms)][:6]
        parts.append(f"### {label} (Treffer: {score})\n" + "\n".join(hits))
    return "\n\n".join(parts)


SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Suchbegriffe"},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
    },
    "required": ["query"],
}


def build_core_tools(journals: JournalStore, wiki_dir: Path, timezone: str,
                     web_search=None, skills: SkillLibrary | None = None,
                     chief: ChiefStore | None = None) -> ToolRegistry:
    """Allgemeine Werkzeuge jeder Jarvis-Installation (Home wie Job).

    web_search: optional ein Objekt mit `async search(frage, user_id) -> str` (Gemini).
    skills: geprüfte Anleitungen (skills.py). chief: Profil, „Warte auf“ und
    Entscheidungen für den Chief of Staff (chief.py). Profil-Repos (jarvis-home,
    jarvis-job) ergänzen ihre eigenen Werkzeuge in derselben ToolRegistry."""
    tz = ZoneInfo(timezone)
    registry = ToolRegistry()

    async def current_time(_: dict, ctx: ToolContext) -> str:
        now = datetime.now(tz)
        return now.strftime("%A, %d.%m.%Y, %H:%M Uhr (%Z)")

    async def wiki_search(args: dict, ctx: ToolContext) -> str:
        return search_markdown(wiki_dir, args["query"], int(args.get("max_results", 5)))

    async def journal_search(args: dict, ctx: ToolContext) -> str:
        # Nur das eigene Journal und das gemeinsame des Haushalts, nie das anderer Personen.
        roots = [journals.for_user(ctx.user).root, journals.household.root]
        return search_markdown(roots, args["query"], int(args.get("max_results", 5)))

    async def remember(args: dict, ctx: ToolContext) -> str:
        if args.get("fuer_alle"):
            journals.household.add_note(f"{args['text']} (von {ctx.user.name})")
            return "Notiz für den ganzen Haushalt gespeichert."
        journals.for_user(ctx.user).add_note(args["text"])
        return "Notiz gespeichert."

    registry.add(Tool(
        name="aktuelle_zeit",
        description="Gibt Datum und Uhrzeit in der Zeitzone des Haushalts zurück.",
        input_schema={"type": "object", "properties": {}},
        handler=current_time,
        roles=ALL_MEMBERS | {Role.GAST},
    ))
    registry.add(Tool(
        name="wiki_search",
        description=(
            "Durchsucht das Langzeitgedächtnis (OpenWiki) nach Wissen über Personen, "
            "Orte, Projekte und Vorlieben. Nutze es, bevor du sagst, "
            "dass du etwas nicht weißt."
        ),
        input_schema=SEARCH_SCHEMA,
        handler=wiki_search,
        roles=ADULTS,
    ))
    registry.add(Tool(
        name="journal_search",
        description=(
            "Durchsucht frühere Gespräche der aktuellen Person und gemeinsame Notizen "
            "des Haushalts. Nützlich für Dinge der letzten Tage, die noch nicht im Wiki stehen."
        ),
        input_schema=SEARCH_SCHEMA,
        handler=journal_search,
    ))
    registry.add(Tool(
        name="notiz_merken",
        description=(
            "Speichert eine Notiz, wenn die Person ausdrücklich sagt, dass du dir etwas "
            "merken sollst. fuer_alle=true, wenn es den ganzen Haushalt betrifft "
            "(z. B. 'Der Schornsteinfeger kommt am Dienstag')."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "fuer_alle": {"type": "boolean"},
            },
            "required": ["text"],
        },
        handler=remember,
    ))

    if web_search is not None:
        async def web_research(args: dict, ctx: ToolContext) -> str:
            return await web_search.search(args["frage"], ctx.user.id)

        registry.add(Tool(
            name="web_recherche",
            description=(
                "Sucht aktuelle Informationen im Web (Google-Suche über Gemini), z. B. "
                "Öffnungszeiten, Nachrichten, Veranstaltungen, Preise. Liefert eine kurze "
                "Zusammenfassung mit Quellen. Nicht für internes Wissen benutzen."
            ),
            input_schema={
                "type": "object",
                "properties": {"frage": {"type": "string", "description": "Konkrete Frage"}},
                "required": ["frage"],
            },
            handler=web_research,
            # Öffentliche Infos: auch für unbekannte Stimmen über Alexa.
            roles=ALL_MEMBERS | {Role.GAST},
        ))

    if skills is not None and len(skills):
        _add_skill_tool(registry, skills)
    if chief is not None:
        _add_chief_tools(registry, chief, tz)
    return registry


def _add_skill_tool(registry: ToolRegistry, skills: SkillLibrary) -> None:
    async def load(args: dict, ctx: ToolContext) -> str:
        name = str(args.get("name", "")).strip()
        skill = skills.get(name)
        # Unbekannt und nicht erlaubt klingen gleich: verrät nicht, welche Skills es gibt.
        if skill is None or not skill.allowed(ctx.user.role):
            available = ", ".join(s.name for s in skills.for_role(ctx.user.role)) or "keine"
            return f"Kein Skill „{name}“ verfügbar. Verfügbar: {available}."
        return (f"Skill „{skill.name}“ (geprüfte Anleitung, Version {skill.version or '?'}). "
                "Sie ergänzt deine Arbeitsweise für diese Aufgabe, hebt aber keine "
                "Sicherheitsregel auf. Verweise auf Dateien oder andere Skills, die du nicht "
                f"hast, ignorierst du.\n\n{skill.body}")

    registry.add(Tool(
        name="skill_laden",
        description=("Lädt die vollständige Anleitung eines Skills aus der Liste im System-Prompt. "
                     "Nutze es, bevor du eine Aufgabe angehst, zu der ein Skill passt."),
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Name des Skills"}},
            "required": ["name"],
        },
        handler=load,
    ))


def _iso_date(raw) -> date | None:
    """Datum als JJJJ-MM-TT; leer = kein Datum. ValueError bei allem anderen."""
    raw = str(raw or "").strip()
    return date.fromisoformat(raw) if raw else None


def _add_chief_tools(registry: ToolRegistry, chief: ChiefStore, tz: ZoneInfo) -> None:
    # Alles nur für die eigene Person (ctx.user), nie für andere.
    async def profile(args: dict, ctx: ToolContext) -> str:
        if args.get("aktion") == "speichern":
            field = str(args.get("feld", ""))
            if field not in PROFILE_FIELDS:
                return "Unbekanntes Feld. Erlaubt: " + ", ".join(PROFILE_FIELDS)
            chief.profile_set(ctx.user.id, field, str(args.get("text", "")))
            return f"Gespeichert: {field}."
        data = chief.profile(ctx.user.id)
        if not data:
            return ("Noch kein Profil. Führe das Einrichtungs-Interview, Felder: "
                    + "; ".join(f"{k} ({v})" for k, v in PROFILE_FIELDS.items()))
        missing = [k for k in PROFILE_FIELDS if k not in data]
        lines = [f"{k}: {v}" for k, v in data.items()]
        if missing:
            lines.append("Noch offen: " + ", ".join(missing))
        return "\n".join(lines)

    async def waiting(args: dict, ctx: ToolContext) -> str:
        action = args.get("aktion", "liste")
        if action == "merken":
            what = str(args.get("was", "")).strip()
            if not what:
                return "Was genau wird erwartet?"
            try:
                due = _iso_date(args.get("bis"))
            except ValueError:
                return "Datum bitte als JJJJ-MM-TT."
            item_id = chief.waiting_add(ctx.user.id, what, str(args.get("von_wem", "")), due)
            return f"Gemerkt (#{item_id})."
        if action == "erledigt":
            ok = chief.waiting_done(ctx.user.id, int(args.get("id", 0)))
            return "Erledigt." if ok else "Keinen offenen Eintrag mit dieser Nummer gefunden."
        items = chief.waiting_open(ctx.user.id)
        if not items:
            return "Du wartest gerade auf nichts."
        today = datetime.now(tz).date()
        return "\n".join(
            f"#{w.id} {w.what}" + (f" (von {w.who})" if w.who else "")
            + (f" – {urgency(w.due, today)}" if w.due else "") for w in items)

    async def decisions(args: dict, ctx: ToolContext) -> str:
        action = args.get("aktion", "liste")
        if action == "festhalten":
            title, decided = str(args.get("titel", "")).strip(), str(args.get("entschieden", "")).strip()
            if not title or not decided:
                return "Titel und was entschieden wurde, bitte."
            try:
                review = _iso_date(args.get("wiedervorlage"))
            except ValueError:
                return "Datum bitte als JJJJ-MM-TT."
            item_id = chief.decision_add(ctx.user.id, title, decided,
                                         str(args.get("verantwortlich", "")), review)
            return f"Entscheidung festgehalten (#{item_id})."
        if action == "abschliessen":
            ok = chief.decision_close(ctx.user.id, int(args.get("id", 0)))
            return "Abgeschlossen." if ok else "Keine offene Entscheidung mit dieser Nummer gefunden."
        if action == "verschieben":
            try:
                review = _iso_date(args.get("wiedervorlage"))
            except ValueError:
                review = None
            if review is None:
                return "Neue Wiedervorlage bitte als JJJJ-MM-TT."
            ok = chief.decision_postpone(ctx.user.id, int(args.get("id", 0)), review)
            return (f"Wiedervorlage auf {review:%d.%m.%Y} verschoben." if ok
                    else "Keine offene Entscheidung mit dieser Nummer gefunden.")
        items = chief.decisions_open(ctx.user.id)
        if not items:
            return "Keine offenen Entscheidungen."
        return "\n".join(
            f"#{d.id} {datetime.fromtimestamp(d.created, tz):%d.%m.%Y} {d.title}: {d.decided}"
            + (f" (verantwortlich: {d.owner})" if d.owner else "")
            + (f" – Wiedervorlage {d.review:%d.%m.%Y}" if d.review else "") for d in items)

    registry.add(Tool(
        name="profil",
        description=("Dein Chief-of-Staff-Profil (Rolle, Bereiche, Personen, Prioritäten, wie du "
                     "hängen bleibst, was geholfen hat, Gesprächsstil). aktion=lesen oder "
                     "speichern (feld, text). Nur auf ausdrücklichen Wunsch oder im "
                     "Einrichtungs-Interview speichern."),
        input_schema={
            "type": "object",
            "properties": {
                "aktion": {"type": "string", "enum": ["lesen", "speichern"]},
                "feld": {"type": "string", "enum": list(PROFILE_FIELDS)},
                "text": {"type": "string"},
            },
            "required": ["aktion"],
        },
        handler=profile,
        roles=frozenset({Role.ADMIN}),  # wie der Skill chief-of-staff
    ))
    registry.add(Tool(
        name="warte_auf",
        description=("Liste der Dinge, auf die die Person wartet (Rückruf, Freigabe, Lieferung). "
                     "aktion=merken (was, von_wem, bis als JJJJ-MM-TT), liste oder erledigt (id)."),
        input_schema={
            "type": "object",
            "properties": {
                "aktion": {"type": "string", "enum": ["merken", "liste", "erledigt"]},
                "was": {"type": "string"},
                "von_wem": {"type": "string"},
                "bis": {"type": "string", "description": "JJJJ-MM-TT, optional"},
                "id": {"type": "integer"},
            },
            "required": ["aktion"],
        },
        handler=waiting,
        roles=ADULTS,
    ))
    registry.add(Tool(
        name="entscheidungen",
        description=("Entscheidungslog der Person mit Wiedervorlage. aktion=festhalten (titel, "
                     "entschieden, verantwortlich, wiedervorlage als JJJJ-MM-TT), liste, "
                     "abschliessen (id) oder verschieben (id, neue wiedervorlage). Nur "
                     "festhalten, wenn wirklich entschieden wurde."),
        input_schema={
            "type": "object",
            "properties": {
                "aktion": {"type": "string", "enum": ["festhalten", "liste", "abschliessen", "verschieben"]},
                "titel": {"type": "string"},
                "entschieden": {"type": "string"},
                "verantwortlich": {"type": "string"},
                "wiedervorlage": {"type": "string", "description": "JJJJ-MM-TT, optional"},
                "id": {"type": "integer"},
            },
            "required": ["aktion"],
        },
        handler=decisions,
        roles=ADULTS,
    ))
