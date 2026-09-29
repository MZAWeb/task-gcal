"""The check-in as a screen: every open episode in a list, one form beside it.

The line prompt asks about episodes in a fixed order and can't go back. Here
the list is always visible, so you can answer the ones you know first, skip
around, and correct an answer you gave a minute ago — a correction is just a
later line for the same episode, exactly as it is on the command line.

The fast path is still two keys: a number picks the answer, Enter records it.
An answer that involved work asks for minutes first, and Enter on a blank
field leaves them unknown rather than borrowing the estimate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    ContentSwitcher,
    Footer,
    Input,
    Label,
    ListItem,
    ListView,
    LoadingIndicator,
    OptionList,
    ProgressBar,
    Static,
)
from textual.widgets.option_list import Option

from ..checkin import ANSWERS, Answer, CheckinSummary, reflection_for, when
from ..reflections import Reflection
from ..review.episodes import KIND_BLOCK_PASSED, Episode


@dataclass(frozen=True)
class CheckinData:
    """What the screen needs, gathered before it is shown."""

    episodes: tuple[Episode, ...]
    since: datetime
    tz: object
    calendar_ok: bool = True


Loader = Callable[[], CheckinData]
Saver = Callable[[Reflection], None]


@dataclass
class _Draft:
    """An answer in progress. Kept per episode so moving around loses nothing."""

    answer: Optional[Answer] = None
    minutes: str = ""
    note: str = ""


@dataclass
class _State:
    drafts: dict[str, _Draft] = field(default_factory=dict)
    saved: dict[str, Answer] = field(default_factory=dict)
    skipped: set[str] = field(default_factory=set)


_BY_KEY = {answer.key: answer for answer in ANSWERS}

_GLYPH_OPEN = "○"
_GLYPH_SAVED = "✓"
_GLYPH_SKIPPED = "–"


class CheckinScreen(Screen[CheckinSummary]):
    """Answer unexplained episodes. Dismisses with what was recorded."""

    DEFAULT_CSS = """
    CheckinScreen { layout: vertical; }
    CheckinScreen #topbar {
        height: 1; padding: 0 1; background: $panel;
    }
    CheckinScreen #topbar-title { width: 1fr; text-style: bold; }
    CheckinScreen #topbar-count { width: auto; color: $text-muted; padding: 0 1; }
    CheckinScreen #progress { width: 24; }
    CheckinScreen #progress Bar { width: 24; }
    CheckinScreen #switcher { height: 1fr; }
    CheckinScreen #main { height: 1fr; }
    CheckinScreen #left {
        width: 40%; max-width: 64; min-width: 28;
        border: round $panel-lighten-2; border-title-color: $text-muted;
    }
    CheckinScreen #left:focus-within { border: round $accent; border-title-color: $accent; }
    CheckinScreen #episodes { height: 1fr; background: transparent; }
    CheckinScreen #episodes > ListItem { padding: 0 1; }
    CheckinScreen #right {
        width: 1fr; padding: 0 1;
        border: round $panel-lighten-2; border-title-color: $text-muted;
    }
    CheckinScreen #right:focus-within { border: round $accent; border-title-color: $accent; }
    CheckinScreen #episode { height: auto; margin-bottom: 1; }
    CheckinScreen .question { text-style: bold; margin-top: 1; }
    CheckinScreen #answers {
        height: auto; max-height: 7; border: none; padding: 0;
        background: transparent;
    }
    CheckinScreen #answers:focus { border: none; }
    CheckinScreen .field { height: auto; margin-top: 1; }
    CheckinScreen .field Label { color: $text-muted; }
    CheckinScreen .field Input { width: 1fr; }
    CheckinScreen #minutes-field.hidden { display: none; }
    CheckinScreen #minutes { width: 40; }
    CheckinScreen #legend { height: 1; padding: 0 1; color: $text-muted; }
    CheckinScreen #error { color: $error; height: auto; }
    CheckinScreen #buttons { height: auto; margin-top: 1; }
    CheckinScreen #buttons Button { margin-right: 2; }
    CheckinScreen .message {
        width: 100%; height: 1fr; content-align: center middle;
        text-align: center; color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("1", "choose('1')", "Answer", key_display="1-5"),
        Binding("2", "choose('2')", show=False),
        Binding("3", "choose('3')", show=False),
        Binding("4", "choose('4')", show=False),
        Binding("5", "choose('5')", show=False),
        Binding("ctrl+s", "save", "Save", key_display="⏎/^s"),
        Binding("s", "skip", "Skip"),
        Binding("n", "move(1)", "Next", show=False),
        Binding("p", "move(-1)", "Prev", show=False),
        Binding("escape", "back_to_list", "List"),
        Binding("q", "finish", "Done"),
    ]

    def __init__(
        self,
        loader: Loader,
        saver: Saver,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        super().__init__()
        self._loader = loader
        self._saver = saver
        self._clock = clock
        self._data: Optional[CheckinData] = None
        self._episodes: list[Episode] = []
        self._state = _State()
        self._current = 0

    # -- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Label("Check-in", id="topbar-title")
            yield Label("", id="topbar-count")
            yield ProgressBar(
                id="progress", show_eta=False, show_percentage=False
            )
        with ContentSwitcher(initial="loading", id="switcher"):
            yield LoadingIndicator(id="loading")
            yield Static("", id="message", classes="message")
            with Horizontal(id="main"):
                with Vertical(id="left"):
                    yield ListView(id="episodes")
                    yield Static(self._legend(), id="legend")
                with VerticalScroll(id="right"):
                    yield Static("", id="episode")
                    yield Label("What happened?", classes="question")
                    yield OptionList(id="answers")
                    with Vertical(id="minutes-field", classes="field hidden"):
                        yield Label("", id="minutes-label")
                        yield Input(
                            placeholder="minutes, or blank if you don't know",
                            restrict=r"[0-9]*",
                            id="minutes",
                            compact=True,
                        )
                    with Vertical(classes="field"):
                        yield Label("Anything worth remembering? (optional)")
                        yield Input(placeholder="a note", id="note", compact=True)
                    yield Static("", id="error")
                    with Horizontal(id="buttons"):
                        yield Button(
                            "Save ⏎", id="save", variant="primary", compact=True
                        )
                        yield Button("Skip s", id="skip", compact=True)
        yield Footer()

    def on_mount(self) -> None:
        self._load()

    @work(thread=True, exclusive=True)
    def _load(self) -> None:
        try:
            data = self._loader()
        except Exception as exc:  # shown, not swallowed
            self.app.call_from_thread(self._show_message, f"Couldn't load: {exc}")
            return
        self.app.call_from_thread(self._loaded, data)

    # -- state ------------------------------------------------------------

    def _loaded(self, data: CheckinData) -> None:
        self._data = data
        self._episodes = list(data.episodes)
        since = data.since.astimezone(data.tz)
        self.query_one("#topbar-title", Label).update(
            f"Check-in · since {since:%a %d %b}"
        )
        if not data.calendar_ok:
            self._show_message(
                "The calendar could not be read, so blocks that passed can't "
                "be seen.\nNothing has been recorded.\n\nq to leave"
            )
            return
        if not self._episodes:
            self._show_message(
                "Nothing to review.\n\nEvery block that has ended is either "
                "done or already explained.\n\nq to leave"
            )
            return

        listing = self.query_one("#episodes", ListView)
        for index, _episode in enumerate(self._episodes):
            listing.append(ListItem(Label(self._row(index)), id=f"ep-{index}"))
        self.query_one("#left").border_title = (
            f"Unexplained · {len(self._episodes)}"
        )
        self.query_one("#progress", ProgressBar).update(
            total=len(self._episodes), progress=0
        )
        self._update_progress()
        self.query_one("#switcher", ContentSwitcher).current = "main"
        listing.index = 0
        listing.focus()
        self._show(0)

    def _show_message(self, text: str) -> None:
        self.query_one("#message", Static).update(text)
        self.query_one("#switcher", ContentSwitcher).current = "message"

    @staticmethod
    def _legend() -> Text:
        legend = Text()
        legend.append(f"{_GLYPH_OPEN} ", style="yellow")
        legend.append("open  ")
        legend.append(f"{_GLYPH_SAVED} ", style="green")
        legend.append("recorded  ")
        legend.append(f"{_GLYPH_SKIPPED} ", style="dim")
        legend.append("skipped")
        return legend

    def _draft(self, episode: Episode) -> _Draft:
        return self._state.drafts.setdefault(episode.key, _Draft())

    @property
    def summary(self) -> CheckinSummary:
        recorded = len(self._state.saved)
        skipped = len(self._state.skipped - set(self._state.saved))
        return CheckinSummary(
            recorded=recorded, skipped=skipped, found=len(self._episodes)
        )

    # -- rendering --------------------------------------------------------

    def _row(self, index: int) -> Text:
        episode = self._episodes[index]
        key = episode.key
        if key in self._state.saved:
            glyph, style = _GLYPH_SAVED, "green"
        elif key in self._state.skipped:
            glyph, style = _GLYPH_SKIPPED, "dim"
        else:
            glyph, style = _GLYPH_OPEN, "yellow"
        row = Text(no_wrap=True, overflow="ellipsis")
        row.append(f"{glyph} ", style=style)
        row.append(f"{when(episode, self._data.tz)[:10]}  ", style="dim")
        row.append(
            episode.task.description,
            style="dim" if key in self._state.skipped else "",
        )
        return row

    def _refresh_row(self, index: int) -> None:
        item = self.query_one(f"#ep-{index}", ListItem)
        item.query_one(Label).update(self._row(index))

    def _episode_text(self, episode: Episode) -> Group:
        tz = self._data.tz
        task = episode.task
        head = Text()
        head.append(task.description, style="bold")
        head.append("\n")
        meta = [task.ref]
        if task.project:
            meta.append(task.project)
        if task.estimate_minutes:
            meta.append(f"estimated {task.estimate_minutes}m")
        if task.due:
            meta.append(f"due {task.due.astimezone(tz):%a %d %b}")
        head.append(" · ".join(meta), style="dim")
        head.append("\n\nEvidence", style="bold")

        # A grid rather than prose, so a long line wraps under its own text
        # instead of under the timestamp.
        evidence = Table.grid(padding=(0, 1))
        evidence.add_column(style="dim", no_wrap=True)
        evidence.add_column(no_wrap=True)
        evidence.add_column(ratio=1)
        for e in sorted(episode.evidence, key=lambda e: e.at):
            block = e.kind == KIND_BLOCK_PASSED
            evidence.add_row(
                f" {e.at.astimezone(tz):%a %d %b %H:%M}",
                Text("▮" if block else "↷", style="cyan" if block else "magenta"),
                e.detail,
            )

        tail = Text()
        tail.append("\nSuggests ", style="dim")
        tail.append(episode.suggestion, style="italic yellow")
        saved = self._state.saved.get(episode.key)
        if saved is not None:
            tail.append("\nRecorded ", style="dim")
            tail.append(f"✓ {saved.label}", style="green")
            tail.append("  (answer again to correct it)", style="dim")
        return Group(head, evidence, tail)

    def _answer_options(self, chosen: Optional[Answer]) -> list[Option]:
        options = []
        for answer in ANSWERS:
            picked = chosen is not None and chosen.key == answer.key
            prompt = Text()
            prompt.append("● " if picked else "○ ", style="bold green" if picked else "dim")
            prompt.append(f"{answer.key}  ", style="bold")
            prompt.append(answer.label, style="bold" if picked else "")
            options.append(Option(prompt, id=answer.key))
        return options

    def _show(self, index: int) -> None:
        if not self._episodes:
            return
        self._current = index
        episode = self._episodes[index]
        draft = self._draft(episode)
        self.query_one("#right").border_title = (
            f"{index + 1} of {len(self._episodes)}"
        )
        self.query_one("#episode", Static).update(self._episode_text(episode))
        self._render_answers(draft.answer)
        self._render_minutes(episode, draft)
        self.query_one("#minutes", Input).value = draft.minutes
        self.query_one("#note", Input).value = draft.note
        self.query_one("#error", Static).update("")
        self.query_one("#right", VerticalScroll).scroll_home(animate=False)

    def _render_answers(self, chosen: Optional[Answer]) -> None:
        answers = self.query_one("#answers", OptionList)
        answers.clear_options()
        answers.add_options(self._answer_options(chosen))
        if chosen is not None:
            answers.highlighted = ANSWERS.index(chosen)

    def _render_minutes(self, episode: Episode, draft: _Draft) -> None:
        field_ = self.query_one("#minutes-field")
        wants = draft.answer is not None and draft.answer.ask_minutes
        field_.set_class(not wants, "hidden")
        aside = (
            f" ({episode.planned_minutes}m set aside)"
            if episode.planned_minutes
            else ""
        )
        self.query_one("#minutes-label", Label).update(
            f"How many minutes did you spend on it{aside}?"
        )

    def _update_progress(self) -> None:
        summary = self.summary
        self.query_one("#progress", ProgressBar).update(progress=summary.recorded)
        self.query_one("#topbar-count", Label).update(
            f"{summary.recorded} recorded · "
            f"{summary.found - summary.recorded} open"
        )

    # -- events -----------------------------------------------------------

    @on(ListView.Highlighted, "#episodes")
    def _highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.index is not None:
            self._stash()
            self._show(event.list_view.index)

    @on(ListView.Selected, "#episodes")
    def _selected(self, _event: ListView.Selected) -> None:
        self.query_one("#answers", OptionList).focus()

    @on(OptionList.OptionSelected, "#answers")
    def _answer_picked(self, event: OptionList.OptionSelected) -> None:
        self.action_choose(event.option.id)

    @on(Input.Changed)
    def _typed(self, event: Input.Changed) -> None:
        if not self._episodes:
            return
        draft = self._draft(self._episodes[self._current])
        if event.input.id == "minutes":
            draft.minutes = event.value
        elif event.input.id == "note":
            draft.note = event.value

    @on(Input.Submitted, "#minutes")
    def _minutes_done(self, _event: Input.Submitted) -> None:
        self.query_one("#note", Input).focus()

    @on(Input.Submitted, "#note")
    def _note_done(self, _event: Input.Submitted) -> None:
        self.action_save()

    @on(Button.Pressed, "#save")
    def _save_pressed(self, _event: Button.Pressed) -> None:
        self.action_save()

    @on(Button.Pressed, "#skip")
    def _skip_pressed(self, _event: Button.Pressed) -> None:
        self.action_skip()

    def _stash(self) -> None:
        """Keep what's typed in the current episode's draft before moving."""
        if not self._episodes:
            return
        draft = self._draft(self._episodes[self._current])
        draft.minutes = self.query_one("#minutes", Input).value
        draft.note = self.query_one("#note", Input).value

    # -- actions ----------------------------------------------------------

    def _ready(self) -> bool:
        return bool(self._episodes) and (
            self.query_one("#switcher", ContentSwitcher).current == "main"
        )

    def action_choose(self, key: str) -> None:
        if not self._ready() or key not in _BY_KEY:
            return
        episode = self._episodes[self._current]
        draft = self._draft(episode)
        draft.answer = _BY_KEY[key]
        self._render_answers(draft.answer)
        self._render_minutes(episode, draft)
        self.query_one("#error", Static).update("")
        target = "#minutes" if draft.answer.ask_minutes else "#note"
        self.query_one(target, Input).focus()

    def action_save(self) -> None:
        if not self._ready():
            return
        self._stash()
        episode = self._episodes[self._current]
        draft = self._draft(episode)
        error = self.query_one("#error", Static)
        if draft.answer is None:
            error.update("Pick what happened first: 1-5, or s to skip.")
            self.query_one("#answers", OptionList).focus()
            return
        minutes: Optional[int] = None
        if draft.answer.ask_minutes and draft.minutes.strip():
            try:
                minutes = int(draft.minutes)
            except ValueError:
                minutes = 0
            if minutes <= 0:
                error.update("Minutes must be a whole number above zero, or blank.")
                self.query_one("#minutes", Input).focus()
                return

        reflection = reflection_for(
            episode,
            draft.answer,
            now=self._clock(),
            actual_minutes=minutes,
            note=draft.note,
        )
        try:
            self._saver(reflection)
        except OSError as exc:
            error.update(f"Couldn't save: {exc}")
            return
        self._state.saved[episode.key] = draft.answer
        self._state.skipped.discard(episode.key)
        self._refresh_row(self._current)
        self._update_progress()
        self._advance()

    def action_skip(self) -> None:
        if not self._ready():
            return
        self._stash()
        episode = self._episodes[self._current]
        if episode.key not in self._state.saved:
            self._state.skipped.add(episode.key)
            self._refresh_row(self._current)
            self._update_progress()
        self._advance()

    def action_move(self, step: int) -> None:
        if not self._ready():
            return
        target = (self._current + step) % len(self._episodes)
        self.query_one("#episodes", ListView).index = target

    def action_back_to_list(self) -> None:
        if self._ready():
            self.query_one("#episodes", ListView).focus()

    def _advance(self) -> None:
        """Move to the next episode nobody has dealt with yet."""
        total = len(self._episodes)
        handled = set(self._state.saved) | self._state.skipped
        for step in range(1, total + 1):
            index = (self._current + step) % total
            if self._episodes[index].key not in handled:
                self.query_one("#episodes", ListView).index = index
                self.query_one("#episodes", ListView).focus()
                return
        self.query_one("#episodes", ListView).focus()
        self.notify(
            "That's all of them. q to finish, or pick one to change it.",
            title="Done",
        )

    def action_finish(self) -> None:
        self.dismiss(self.summary)


class CheckinApp(App[CheckinSummary]):
    """`task-gcal checkin` on a terminal."""

    TITLE = "task-gcal check-in"

    def __init__(self, loader: Loader, saver: Saver, **screen_kwargs) -> None:
        super().__init__()
        self._screen = CheckinScreen(loader, saver, **screen_kwargs)

    def on_mount(self) -> None:
        self.push_screen(self._screen, callback=self.exit)


def run(settings, *, since: Optional[datetime] = None) -> int:
    """Open the check-in screen against the real data. Returns an exit code."""
    from datetime import timedelta

    from .. import reflections as reflections_mod
    from ..checkin import collect_window, open_episodes
    from ..review.episodes import DEFAULT_SINCE_DAYS

    now = datetime.now(timezone.utc)
    since = since or (now - timedelta(days=DEFAULT_SINCE_DAYS))
    tz = settings.resolve_timezone()
    seen: dict[str, bool] = {}

    def loader() -> CheckinData:
        facts = collect_window(settings, since=since, now=now)
        seen["calendar_ok"] = facts.calendar_ok
        episodes = (
            open_episodes(facts, since=since, now=now) if facts.calendar_ok else []
        )
        return CheckinData(
            episodes=tuple(episodes),
            since=since,
            tz=tz,
            calendar_ok=facts.calendar_ok,
        )

    summary = CheckinApp(loader, reflections_mod.append).run()
    if summary is not None and summary.found:
        left = summary.found - summary.recorded
        print(
            f"Recorded {summary.recorded}, left {left} open. "
            "Answers can be corrected by running this again."
        )
    return 0 if seen.get("calendar_ok", True) else 1
