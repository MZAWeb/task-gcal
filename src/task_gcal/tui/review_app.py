"""The review as a screen: sections down the side, one opened beside them.

The plain report is one screen by design, and everything else hides behind
`--section NAME` — which is only useful if you remember the names. Here the
names are the navigation. The overview is the same one-screen summary, every
section is one keypress away, and moving to last week or last month is a key
rather than a flag you have to count backwards for.

Still a document rather than a dashboard: the closing line stays pinned at the
bottom whatever you're looking at, because it's the one part of the review
that asks you to do anything. Triage is here too, as the commands the plain
`--triage` prints; picking one copies it, and nothing is ever run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
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
    ContentSwitcher,
    Footer,
    Label,
    LoadingIndicator,
    OptionList,
    Static,
)
from textual.widgets.option_list import Option

from ..review.metrics import glossary, groups, summary_sections
from ..review.metrics import trends as trends_metric
from ..review.model import Review, Section
from ..review.periods import KIND_MONTH, KIND_WEEK, _month_shift
from ..review.stagnation import Prescription


@dataclass(frozen=True)
class ReviewPage:
    """One period's review, and the triage list that goes with it."""

    review: Review
    stuck: tuple[Prescription, ...] = ()


# (kind, anchor) -> page. `anchor` is any day in the period, or None for the
# default one — the same contract as `review.periods.resolve`.
Source = Callable[[str, Optional[date]], ReviewPage]

_OVERVIEW = "overview"
_TRIAGE = "triage"
_SECTION = "sec:"

_WEEKDAYS = "MTWTFSS"


def _coverage_bar(observed: int, total: int, width: int = 10) -> Text:
    """`▰▰▰▰▰▰▱▱▱▱ 6/10`, so a denominator is seen before it is read."""
    filled = 0 if total <= 0 else round(width * min(observed, total) / total)
    bar = Text()
    bar.append("▰" * filled, style="green" if observed >= total else "yellow")
    bar.append("▱" * (width - filled), style="dim")
    return bar


class ReviewScreen(Screen[None]):
    """Browse a period's review. Read-only."""

    DEFAULT_CSS = """
    ReviewScreen { layout: vertical; }
    ReviewScreen #topbar { height: 1; padding: 0 1; background: $panel; }
    ReviewScreen #period { width: auto; text-style: bold; }
    ReviewScreen #seen { width: 1fr; padding: 0 2; color: $text-muted; }
    ReviewScreen #nav-hint { width: auto; color: $text-muted; }
    ReviewScreen #main { height: 1fr; }
    ReviewScreen #left {
        width: 30; border: round $panel-lighten-2; border-title-color: $text-muted;
    }
    ReviewScreen #left:focus-within { border: round $accent; border-title-color: $accent; }
    ReviewScreen #nav { height: 1fr; border: none; background: transparent; }
    ReviewScreen #nav:focus { border: none; }
    ReviewScreen #legend { height: 1; padding: 0 1; color: $text-muted; }
    ReviewScreen #right {
        width: 1fr; border: round $panel-lighten-2; border-title-color: $text-muted;
    }
    ReviewScreen #right:focus-within { border: round $accent; border-title-color: $accent; }
    ReviewScreen #content { height: 1fr; }
    ReviewScreen .page { padding: 0 1; }
    ReviewScreen #triage { height: 1fr; }
    ReviewScreen #triage-intro { height: auto; padding: 0 1 1 1; color: $text-muted; }
    ReviewScreen #commands { height: 1fr; border: none; background: transparent; }
    ReviewScreen #commands:focus { border: none; }
    ReviewScreen #message {
        height: 1fr; content-align: center middle; text-align: center;
        color: $text-muted;
    }
    ReviewScreen #closing {
        height: auto; max-height: 5; padding: 0 1; margin: 0 0;
        border: round $warning; border-title-color: $warning;
    }
    ReviewScreen #closing.hidden { display: none; }
    """

    BINDINGS = [
        Binding("left,h", "shift(-1)", "Earlier", key_display="←/h"),
        Binding("right,l", "shift(1)", "Later", key_display="→/l"),
        Binding("w", "kind('week')", "Week"),
        Binding("m", "kind('month')", "Month"),
        Binding("t", "today", "Now"),
        Binding("c", "checkin", "Check-in"),
        Binding("y", "copy", "Copy", show=False),
        Binding("o", "open_html", "Browser"),
        Binding("r", "reload", "Reload"),
        Binding("q", "app.quit", "Quit"),
    ]

    def __init__(
        self,
        source: Source,
        *,
        kind: str = KIND_WEEK,
        anchor: Optional[date] = None,
        focus: Optional[str] = None,
        checkin: Optional[Callable[[], Screen]] = None,
        opener: Callable[[str], object] = webbrowser.open,
    ) -> None:
        super().__init__()
        self._source = source
        self._kind = kind
        self._anchor = anchor
        self._focus_on = focus or _OVERVIEW
        self._checkin = checkin
        self._opener = opener
        self._cache: dict[tuple[str, Optional[date]], ReviewPage] = {}
        self._page: Optional[ReviewPage] = None
        self._request = 0

    # -- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Label("Review", id="period")
            yield Label("", id="seen")
            yield Label("← earlier · later →", id="nav-hint")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield OptionList(id="nav")
                yield Static(
                    Text.assemble(
                        ("• ", "yellow"), "suggests  ", ("– ", "dim"), "not measured"
                    ),
                    id="legend",
                )
            with Vertical(id="right"):
                with ContentSwitcher(initial="loading", id="content"):
                    yield LoadingIndicator(id="loading")
                    yield Static("", id="message")
                    with VerticalScroll(id="page-scroll"):
                        yield Static("", id="page", classes="page")
                    with Vertical(id="triage"):
                        yield Static("", id="triage-intro")
                        yield OptionList(id="commands")
        yield Static("", id="closing", classes="hidden")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#left").border_title = "Sections"
        self._fetch()

    # -- loading ----------------------------------------------------------

    def _fetch(self, *, fresh: bool = False) -> None:
        key = (self._kind, self._anchor)
        if fresh:
            self._cache.clear()
        if key in self._cache:
            self._shown(self._cache[key])
            return
        self._request += 1
        self.query_one("#content", ContentSwitcher).current = "loading"
        self._load(self._request, key)

    @work(thread=True, exclusive=True)
    def _load(self, request: int, key: tuple[str, Optional[date]]) -> None:
        kind, anchor = key
        try:
            page = self._source(kind, anchor)
        except Exception as exc:  # shown, not swallowed
            self.app.call_from_thread(self._failed, request, exc)
            return
        self.app.call_from_thread(self._loaded, request, key, page)

    def _loaded(self, request: int, key, page: ReviewPage) -> None:
        self._cache[key] = page
        # Also under the day it starts on, which is how moving between periods
        # names them: arriving back at this week by `→` is the same page as
        # opening on it by default, and shouldn't cost a second fetch.
        period = page.review.period
        self._cache[(key[0], period.start.astimezone(period.tz).date())] = page
        if request == self._request:
            self._shown(page)

    def _failed(self, request: int, exc: Exception) -> None:
        if request != self._request:
            return
        self.query_one("#message", Static).update(
            f"Couldn't build the review:\n{exc}\n\nr to try again"
        )
        self.query_one("#content", ContentSwitcher).current = "message"

    # -- rendering --------------------------------------------------------

    def _shown(self, page: ReviewPage) -> None:
        self._page = page
        review = page.review
        self.query_one("#period", Label).update(self._title(review))
        self.query_one("#seen", Label).update(self._seen(review))
        self._build_nav(page)
        self._show_closing(review)
        self._open(self._focus_on)

    def _title(self, review: Review) -> Text:
        period = review.period
        tz = period.tz
        first = period.start.astimezone(tz)
        last = (period.nominal_end - timedelta(microseconds=1)).astimezone(tz)
        title = Text()
        title.append(period.label, style="bold")
        title.append(f"  {first:%a %d %b} – {last:%a %d %b}", style="dim")
        if period.in_progress:
            title.append("  in progress", style="italic yellow")
        return title

    def _seen(self, review: Review) -> Text:
        """Which days a run observed, as a strip, for weeks; a bar for months."""
        out = Text()
        if review.observed is None:
            return out
        seen, total = review.observed
        observed = set(review.observed_days)
        period = review.period
        if period.kind == KIND_WEEK:
            # The whole week, so a Tuesday review shows five days still to
            # come rather than a week that seems to stop on Tuesday.
            first = period.start.astimezone(period.tz).date()
            today = period.end.astimezone(period.tz).date()
            for n in range(7):
                day = first + timedelta(days=n)
                letter = _WEEKDAYS[day.weekday()]
                if day.isoformat() in observed:
                    out.append(letter, style="bold green")
                elif day > today or (day == today and period.in_progress
                                     and period.end.astimezone(period.tz).time()
                                     == datetime.min.time()):
                    out.append("·", style="dim")
                else:
                    out.append(letter, style="red")
                out.append(" ")
        else:
            out.append_text(_coverage_bar(seen, total))
            out.append(" ")
        out.append(f"task-gcal ran on {seen} of {total} days", style="dim")
        return out

    def _build_nav(self, page: ReviewPage) -> None:
        review = page.review
        where = groups(review.period.kind)
        nav = self.query_one("#nav", OptionList)
        nav.clear_options()
        nav.add_option(Option(Text("Overview", style="bold"), id=_OVERVIEW))
        group = None
        for section in review.sections:
            heading = where.get(section.key)
            if heading != group:
                group = heading
                nav.add_option(None)
                nav.add_option(
                    Option(Text(heading or "", style="dim italic"), disabled=True)
                )
            label = Text("  ")
            if section.measured:
                label.append(section.label)
            else:
                label.append(section.label, style="dim")
                label.append(" –", style="dim")
            if section.suggestions:
                label.append(" •", style="yellow")
            nav.add_option(Option(label, id=f"{_SECTION}{section.key}"))
        nav.add_option(None)
        triage = Text("Triage")
        triage.append(f"  {len(page.stuck)}", style="yellow" if page.stuck else "dim")
        nav.add_option(Option(triage, id=_TRIAGE))

    def _show_closing(self, review: Review) -> None:
        closing = self.query_one("#closing", Static)
        found = review.closing
        if found is None:
            closing.set_class(True, "hidden")
            return
        _key, best = found
        text = Text(best.text)
        note = review.repetition_note()
        if note:
            text.append(f"  {note}", style="italic dim")
        closing.update(text)
        closing.border_title = "Look at"
        closing.set_class(False, "hidden")

    def _open(self, target: str) -> None:
        """Show a nav entry's page and put the cursor on it."""
        if self._page is None:
            return
        nav = self.query_one("#nav", OptionList)
        try:
            index = nav.get_option_index(target)
        except Exception:  # a section that doesn't exist this period
            target, index = _OVERVIEW, nav.get_option_index(_OVERVIEW)
        self._focus_on = target
        if nav.highlighted != index:
            nav.highlighted = index
        self._show_page(target)
        if not self.focused or self.focused is self.query_one("#nav"):
            nav.focus()

    def _show_page(self, target: str) -> None:
        page = self._page
        content = self.query_one("#content", ContentSwitcher)
        right = self.query_one("#right")
        if target == _TRIAGE:
            right.border_title = "Triage"
            self._render_triage(page)
            content.current = "triage"
            return
        body = self.query_one("#page", Static)
        if target.startswith(_SECTION):
            section = page.review.section(target[len(_SECTION):])
            if section is not None:
                group = groups(page.review.period.kind).get(section.key)
                right.border_title = (
                    f"{section.label} · {group}" if group else section.label
                )
                body.update(self._section_view(page.review, section))
                content.current = "page-scroll"
                self.query_one("#page-scroll", VerticalScroll).scroll_home(
                    animate=False
                )
                return
        right.border_title = "Overview"
        body.update(self._overview(page.review))
        content.current = "page-scroll"
        self.query_one("#page-scroll", VerticalScroll).scroll_home(animate=False)

    def _overview(self, review: Review) -> Group:
        chosen = summary_sections(review.sections, kind=review.period.kind)
        grid = Table.grid(padding=(0, 2), expand=True)
        grid.add_column(style="bold", no_wrap=True, min_width=14)
        grid.add_column(ratio=1)
        for section in chosen:
            if not section.measured:
                continue
            grid.add_row(section.label, section.summary)
            grid.add_row("", "")
        parts: list = [grid]

        unmeasured = [
            s.label for s in chosen if not s.measured and not s.optional
        ]
        if unmeasured:
            parts.append(
                Text.assemble(("Not measured  ", "bold yellow"), ", ".join(unmeasured))
            )
        if review.caveats:
            parts.append(Text("\nCoverage", style="bold"))
            caveats = Table.grid(padding=(0, 1))
            caveats.add_column(style="dim", no_wrap=True)
            caveats.add_column(style="dim", ratio=1)
            for caveat in review.caveats:
                caveats.add_row(" ·", caveat)
            parts.append(caveats)
        rest = [s for s in review.sections if s not in chosen]
        if rest:
            parts.append(
                Text(
                    f"\n{len(rest)} more sections in the list on the left.",
                    style="dim italic",
                )
            )
        return Group(*parts)

    def _section_view(self, review: Review, section: Section) -> Group:
        parts: list = []
        means = glossary().get(section.key)
        if means:
            parts.append(Text(means, style="italic dim"))
            parts.append(Text(""))
        parts.append(Text(section.summary, style="bold"))

        if not section.measured:
            parts.append(
                Text(
                    "\nNot measured: the inputs weren't there. "
                    "See Coverage on the overview.",
                    style="yellow",
                )
            )

        sparks = self._sparklines(section)
        if sparks is not None:
            parts.append(Text(""))
            parts.append(sparks)

        if section.detail:
            parts.append(Text(""))
            for line in section.detail:
                parts.append(Text(line, no_wrap=False, overflow="fold"))

        if section.coverage:
            parts.append(Text(""))
            cover = Table.grid(padding=(0, 1))
            cover.add_column(no_wrap=True)
            cover.add_column(no_wrap=True, justify="right", style="dim")
            cover.add_column(style="dim")
            for coverage in section.coverage:
                cover.add_row(
                    _coverage_bar(coverage.observed, coverage.total),
                    f"{coverage.observed}/{coverage.total}",
                    coverage.label,
                )
            parts.append(cover)

        if section.suggestions:
            parts.append(Text(""))
            for suggestion in sorted(
                section.suggestions, key=lambda s: -s.weight
            ):
                parts.append(
                    Text.assemble(("→ ", "bold yellow"), (suggestion.text, "yellow"))
                )
        return Group(*parts)

    @staticmethod
    def _sparklines(section: Section) -> Optional[Table]:
        if section.key != trends_metric.KEY:
            return None
        rows = trends_metric.series_for_display(section.data)
        if not rows:
            return None
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold", no_wrap=True)
        table.add_column(style="cyan", no_wrap=True)
        table.add_column(style="dim", no_wrap=True)
        for name, values, unit in rows:
            bars = trends_metric.sparkline(list(values))
            if not bars:
                continue
            table.add_row(
                name, bars, f"{values[0]:g}{unit} → {values[-1]:g}{unit}"
            )
        return table

    def _render_triage(self, page: ReviewPage) -> None:
        intro = self.query_one("#triage-intro", Static)
        commands = self.query_one("#commands", OptionList)
        commands.clear_options()
        if not page.stuck:
            intro.update(
                "Nothing looks stuck: no open task has enough signs against it "
                "to need a decision."
            )
            return
        intro.update(
            "Commands that would settle each stuck task. Nothing here is run: "
            "Enter or y copies the highlighted one for you to paste."
        )
        # One column for every command, so the reasons line up to read down.
        width = max(
            (len(c) for p in page.stuck for c, _why in p.commands), default=0
        )
        for n, prescription in enumerate(page.stuck):
            if n:
                commands.add_option(None)
            commands.add_option(
                Option(Text(prescription.entry.summary, style="bold"), disabled=True)
            )
            for command, why in prescription.commands:
                row = Text("  ")
                row.append(command.ljust(width), style="cyan")
                row.append(f"   # {why}", style="dim")
                commands.add_option(Option(row, id=command))

    # -- events -----------------------------------------------------------

    @on(OptionList.OptionHighlighted, "#nav")
    def _nav_moved(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.id and event.option.id != self._focus_on:
            self._focus_on = event.option.id
            self._show_page(event.option.id)

    @on(OptionList.OptionSelected, "#nav")
    def _nav_chosen(self, event: OptionList.OptionSelected) -> None:
        # Enter moves into the page, so it can be scrolled or picked from.
        target = (
            "#commands" if event.option.id == _TRIAGE else "#page-scroll"
        )
        self.query_one(target).focus()

    @on(OptionList.OptionSelected, "#commands")
    def _command_chosen(self, event: OptionList.OptionSelected) -> None:
        self._copy(event.option.id)

    # -- actions ----------------------------------------------------------

    def _period(self):
        return None if self._page is None else self._page.review.period

    def action_shift(self, step: int) -> None:
        period = self._period()
        if period is None:
            return
        start = period.start.astimezone(period.tz).date()
        if period.kind == KIND_WEEK:
            anchor = start + timedelta(weeks=step)
        else:
            anchor = _month_shift(start, step)
        today = self._page.review.generated_at.astimezone(period.tz).date()
        if anchor > today:
            self.notify("That hasn't happened yet.", severity="warning")
            return
        self._anchor = anchor
        self._fetch()

    def action_kind(self, kind: str) -> None:
        if kind not in (KIND_WEEK, KIND_MONTH) or kind == self._kind:
            return
        period = self._period()
        self._kind = kind
        if period is not None:
            # Stay at the same moment: the last day being looked at, so a
            # week becomes the month it ends in and a month its last week.
            last = (period.end - timedelta(microseconds=1)).astimezone(period.tz)
            self._anchor = last.date()
        self._fetch()

    def action_today(self) -> None:
        self._anchor = None
        self._fetch()

    def action_reload(self) -> None:
        self._fetch(fresh=True)

    def action_checkin(self) -> None:
        if self._checkin is None:
            return
        # Answers change the reasons section, so what's cached is stale after.
        self.app.push_screen(
            self._checkin(), callback=lambda _summary: self._fetch(fresh=True)
        )

    def action_copy(self) -> None:
        commands = self.query_one("#commands", OptionList)
        if self.focused is not commands or commands.highlighted is None:
            return
        option = commands.get_option_at_index(commands.highlighted)
        if option.id:
            self._copy(option.id)

    def _copy(self, command: Optional[str]) -> None:
        if not command:
            return
        self.app.copy_to_clipboard(command)
        _system_copy(command)
        self.notify(command, title="Copied, not run")

    def action_open_html(self) -> None:
        if self._page is None:
            return
        from ..review.render import render

        review = self._page.review
        text = render(review, fmt="html", sections=review.sections, detailed=True)
        handle, name = tempfile.mkstemp(
            prefix=f"task-gcal-{review.period.label.replace(' ', '-').lower()}-",
            suffix=".html",
        )
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(text)
        path = Path(name)
        # Task titles are in here, so keep it to the owner even in /tmp.
        os.chmod(path, 0o600)
        self._opener(path.resolve().as_uri())
        self.notify(str(path), title="Opened in the browser")


def _system_copy(text: str) -> None:
    """Belt and braces for terminals that ignore OSC 52. Best effort only."""
    for tool in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if shutil.which(tool[0]):
            try:
                subprocess.run(tool, input=text.encode(), check=False, timeout=2)
            except (OSError, subprocess.SubprocessError):
                pass
            return


class ReviewApp(App[None]):
    """`task-gcal review` on a terminal."""

    TITLE = "task-gcal review"

    def __init__(self, source: Source, **screen_kwargs) -> None:
        super().__init__()
        self._screen = ReviewScreen(source, **screen_kwargs)

    def on_mount(self) -> None:
        self.push_screen(self._screen)


def source_for(settings, *, gcal=None) -> Source:
    """Build pages from the real Taskwarrior, calendar and journal."""
    from ..review import build
    from ..review.facts import collect, utc_now
    from ..review.periods import resolve
    from ..review.stagnation import find, prescribe

    tz = settings.resolve_timezone()

    def load(kind: str, anchor: Optional[date]) -> ReviewPage:
        now = utc_now()
        period = resolve(kind, tz, now=now, anchor=anchor)
        facts = collect(settings, period, now=now, gcal=gcal)
        stuck = find(
            list(facts.tasks),
            timelines=facts.timelines,
            blocks_by_task=facts.blocks_by_task(),
            now=facts.now,
        )
        return ReviewPage(
            review=build(facts), stuck=tuple(prescribe(e) for e in stuck)
        )

    return load


def run(settings, request) -> int:
    """Open the review screen for a `ReviewRequest`. Returns an exit code."""
    from .. import reflections as reflections_mod
    from ..review.facts import utc_now
    from ..review.periods import resolve
    from .checkin_app import CheckinScreen, checkin_loader

    anchor = request.anchor
    if anchor is None and request.offset:
        # Counting back is turned into naming, so moving from there is exact.
        tz = settings.resolve_timezone()
        period = resolve(request.kind, tz, now=utc_now(), offset=request.offset)
        anchor = period.start.astimezone(tz).date()

    focus = None
    if request.triage:
        focus = _TRIAGE
    elif request.sections:
        focus = f"{_SECTION}{request.sections[0]}"

    def checkin() -> Screen:
        return CheckinScreen(checkin_loader(settings), reflections_mod.append)

    ReviewApp(
        source_for(settings),
        kind=request.kind,
        anchor=anchor,
        focus=focus,
        checkin=checkin,
    ).run()
    return 0
