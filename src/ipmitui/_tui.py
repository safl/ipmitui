"""Textual TUI for ipmitui.

The fancy ``ipmitui tui`` command. ``ipmitui status`` stays the plain
one-shot Rich table; this module is the interactive operator surface:
filter, select, drop into SoL or fire a power op without leaving the
keyboard. Lives in its own module so the main package stays readable
and ``textual`` only loads when the TUI is requested.

The app talks to ``ipmitool`` via ``ipmitui.probe_power`` /
``ipmitui.power_op`` so the protocol classification + secret-resolve
logic is shared with the rest of the CLI.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import time
from pathlib import Path

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, OptionList, Rule, Static
from textual.widgets.option_list import Option

from ipmitui import (
    DEFAULT_INTERVAL,
    DEFAULT_WORKERS,
    Config,
    Machine,
    Probe,
    __version__,
    power_op,
    probe_power,
    save_config,
)

_STATE_STYLE = {
    "on": "bold green",
    "off": "dim",
    "unreachable": "yellow",
    "auth-fail": "bold red",
    "error": "red",
}

# Nerd Font glyphs. These need a patched ("Nerd Font") family in the
# operator's terminal; without one they render as tofu boxes, so the
# textual state/action name always stays next to the glyph and never
# depends on it. State glyphs show in the action picker; action glyphs
# in the picker; see also the column-header and top-bar glyphs.
_STATE_ICON = {
    "on": "",  # bolt - powered
    "off": "",  # power - off
    "unreachable": "",  # broken link - no RMCP
    "auth-fail": "",  # lock - creds rejected
    "error": "",  # exclamation - other failure
}

_ACTION_ICON = {
    "sol": "",  # terminal
    "on": "",  # bolt
    "off": "",  # power
    "cycle": "",  # refresh
    "reset": "",  # rotate
    "soft": "",  # moon - graceful
}

# Machine-count glyph for the top-bar count slot.
_MACHINES_ICON = ""  # U+F233 server

# Glyphs for the app title, filter label, and table column headers.
_APP_ICON = ""  # microchip
_FILTER_ICON = ""  # magnifier
_COLUMN_ICON = {
    "name": "",  # tag
    "host": "",  # globe
    "power": "",  # bolt
    "ms": "",  # clock
    "note": "",  # sticky note
}

# Refresh / sync glyph, reused in the top-bar scan-age label.
_REFRESH_ICON = ""

# Glyphs for the add / edit form field labels.
_FORM_ICON = {
    "name": "",  # tag
    "host": "",  # globe
    "user": "",  # user
    "password": "",  # lock
    "pass_cmd": "",  # terminal
    "note": "",  # sticky note
}


class _ActionList(OptionList):
    """Action picker list with vim-style j/k nav (arrows work too)."""

    BINDINGS = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]


class _MachineTable(DataTable):
    """Machine table with vim-style j/k nav (arrows work too)."""

    BINDINGS = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]


# Destructive ops get a confirm modal; ``on`` fires immediately so
# the common "wake this box" gesture stays one keystroke.
class _ActionScreen(ModalScreen[str | None]):
    """Per-machine action picker, opened on Enter or "i" (cursor
    movement in the main table is side-effect-free). A selectable
    list: arrows / j / k move and Enter acts; each row's bracketed
    letter is also a direct hotkey; Esc / q cancel.

    Returns the chosen op id (``"sol"`` / ``"on"`` / ``"off"`` /
    ``"cycle"`` / ``"reset"`` / ``"soft"``) via the standard
    ``ModalScreen.dismiss`` channel, or ``None`` if cancelled."""

    BINDINGS = [
        Binding("s", "pick('sol')", "sol"),
        Binding("o", "pick('on')", "on"),
        Binding("f", "pick('off')", "off"),
        Binding("c", "pick('cycle')", "cycle"),
        Binding("r", "pick('reset')", "reset"),
        Binding("g", "pick('soft')", "soft"),
        Binding("escape,q", "cancel", "cancel"),
    ]

    def __init__(self, machine: Machine, probe: Probe | None, glyphs: bool = True) -> None:
        super().__init__()
        self._machine = machine
        self._probe = probe
        self._glyphs = glyphs

    def compose(self) -> ComposeResult:
        def g(glyph: str) -> str:
            return f"{glyph} " if self._glyphs else ""

        state = self._probe.state if self._probe else "?"
        style = _STATE_STYLE.get(state, "")
        label = f"{g(_STATE_ICON.get(state, ''))}{state}".strip()
        styled_state = f"[{style}]{label}[/]" if style else label
        title = f"{self._machine.name}  ({self._machine.host})  power: {styled_state}"
        # Selectable list: arrows / j / k move, Enter acts; the
        # bracketed letter is also a direct hotkey (see BINDINGS).
        opts = [
            ("sol", f"{g(_ACTION_ICON['sol'])}\\[[$accent bold]s[/]]erial console (SoL)"),
            ("on", f"{g(_ACTION_ICON['on'])}power \\[[$accent bold]o[/]]n"),
            ("off", f"{g(_ACTION_ICON['off'])}power o\\[[$accent bold]f[/]]f"),
            ("cycle", f"{g(_ACTION_ICON['cycle'])}power \\[[$accent bold]c[/]]ycle"),
            ("reset", f"{g(_ACTION_ICON['reset'])}power \\[[$accent bold]r[/]]eset"),
            ("soft", f"{g(_ACTION_ICON['soft'])}\\[[$accent bold]g[/]]raceful shutdown"),
        ]
        with Vertical(classes="action-box"):
            yield Label(title, classes="action-title")
            if self._machine.description:
                yield Label(self._machine.description, classes="action-desc")
            yield _ActionList(*(Option(p, id=op) for op, p in opts), classes="action-list")
            yield Label(
                "move:   [$accent bold]j/k/arrows[/]\n"
                "select: [$accent bold]<ENTER>[/]\n"
                "cancel: [$accent bold]Esc / q[/]",
                classes="action-hint",
            )

    def on_mount(self) -> None:
        self.query_one(_ActionList).focus()

    @on(OptionList.OptionSelected)
    def _on_pick(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option_id)

    def action_pick(self, op: str) -> None:
        self.dismiss(op)

    def action_cancel(self) -> None:
        self.dismiss(None)


class IpmituiApp(App[None]):
    """Interactive table view over a set of BMCs.

    Layout (top to bottom, inside a rounded frame): a one-line top
    bar (app name, machine count, filter input, scan age), a heavy
    rule, the ``DataTable``, a heavy rule, then a custom footer hint
    bar. Refreshes by running ``_scan`` in a thread pool every
    ``interval`` seconds so the UI does not block on slow BMCs.
    """

    # Textual's built-in command palette (Ctrl+P) opens a fuzzy
    # picker over its own dev / theme commands. ipmitui has no use
    # for that surface; disable so Ctrl+P does not steal a keystroke.
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    /* Outer frame around the whole app. It rounds ipmitui's own edge
       so the operator has a clear boundary even when the surrounding
       multiplexer pane is unframed. The frame is scoped to the main
       Screen: ModalScreen overlays (action picker, CRUD form, delete
       confirm) drop it so their centred boxes keep just their own
       thick border instead of sitting behind a second full-screen
       frame. */
    Screen { border: round $accent; }
    ModalScreen { border: none; }

    /* Single top bar replacing the stock Header, all on one line:
       app name | machine counts | filter input | scan age. The filter
       Input takes the slack (1fr); everything else is auto width. */
    #topbar { dock: top; height: 1; background: $panel; }
    #topbar #app-title { width: auto; padding: 0 1; color: $accent; text-style: bold; }
    /* Dim the app name when the terminal/app loses focus, like the
       stock Header did, so an unfocused pane reads as inactive. */
    #topbar #app-title.-blurred { color: $text-muted; text-style: none; }
    #topbar .sep { width: auto; color: $text-muted; }
    /* Machine counts ("N machines · M showing"); server glyph accent,
       rest muted (see _refresh_status). */
    #topbar #counts { width: auto; padding: 0 1; color: $text-muted; }
    /* Base muted so only the "[f]" markup in the label is accented. */
    #topbar Label { width: auto; padding: 0 1; color: $text-muted; }
    /* Filter input fills the middle. ``f`` focuses it (only from the
       table); Esc clears it and returns focus to the table. */
    #topbar Input { width: 1fr; height: 1; border: none; padding: 0; background: $panel; }
    #topbar #age { width: auto; padding: 0 1; color: $text-muted; }
    /* Horizontal rules bracketing the table. Default Rule margin
       would insert blank rows; zero it so the rules hug the table. */
    Rule { height: 1; margin: 0; color: $accent; }
    DataTable { height: 1fr; }
    /* Drop the loud header bar (the ansi themes default it to a
       bright-blue background) but keep the header glyphs + names in
       full accent colour: not screaming, still vivid. The cursor
       (highlighted) row is what carries the strong background. */
    DataTable > .datatable--header { background: $panel; color: $accent; text-style: bold; }
    DataTable:ansi > .datatable--header {
        background: ansi_default; color: $accent; text-style: bold;
    }
    /* Footer hint bar: two Statics on one docked row. The left group
       (actions) takes the slack; the right group (help / glyphs /
       quit) is auto width, so it sits flush right. Bracketed shortcut
       letters, "┃"/"│" group separators, muted base colour. */
    #footer { dock: bottom; height: 1; background: $panel; color: $text-muted; }
    #footer #footer-left { width: 1fr; padding: 0 1; }
    #footer #footer-right { width: auto; padding: 0 1; }

    /* Notification toasts: the ansi theme's $panel matches the
       background, so without a border they don't read as a box. Give
       them a rounded border, accent by default and severity-coloured
       for warnings / errors. */
    /* The rounded border already frames each toast, so drop Textual's
       default margin-top: 1 (a blank line between stacked toasts) and
       the internal vertical padding. */
    Toast { background: $panel; border: round $accent; margin-top: 0; padding: 0 1; }
    Toast.-warning { border: round $warning; }
    Toast.-error { border: round $error; }

    _ActionScreen { align: center middle; }
    _HelpScreen { align: right bottom; }
    _DeleteConfirmScreen { align: center middle; }
    /* Popup boxes hug their content (auto width, capped) so each is no
       wider than it needs to be. The auto-width children below feed
       the measurement; the 1fr button row does not, it just fills. */
    .action-box {
        background: $panel;
        border: thick $accent;
        padding: 1 1;
        /* Keep clear of the app's outer frame, even when aligned to a
           corner (e.g. the bottom-right help popup). */
        margin: 1 2;
        width: auto;
        max-width: 80;
        height: auto;
    }
    .action-title { content-align: left middle; padding: 0 0 1 0; color: $accent; }
    .action-desc { content-align: left middle; padding: 0 0 1 0; color: $text-muted; }
    /* Footer line of a popup: no bottom padding so it does not add a
       blank line above the box border (the box padding already does). */
    .action-foot { content-align: left middle; color: $text-muted; }
    .action-body { width: auto; padding: 0 0 1 0; }
    /* Action picker list: auto width (hug the longest option) + auto
       height, no border so it sits cleanly inside the box. */
    .action-list { width: auto; height: auto; border: none; padding: 0; background: $panel; }
    /* Hint under the picker list, with a blank line above it. */
    .action-hint { color: $text-muted; padding: 1 0 0 0; }

    _MachineFormScreen { align: center middle; }
    .form-box {
        background: $panel;
        border: thick $accent;
        padding: 1 2;
        margin: 1 2;
        width: 60;
        height: auto;
    }
    /* One row per field: fixed-width label, single-line borderless
       input filling the rest. Keeps the modal short. */
    .form-field { height: 1; margin: 0 0 1 0; }
    .form-field Label { width: 12; color: $text-muted; }
    .form-field Input { width: 1fr; height: 1; border: none; padding: 0; }
    .form-title { content-align: center middle; padding: 0 0 1 0; color: $accent; }
    .form-keys { content-align: center middle; padding: 1 0 0 0; color: $text-muted; }
    /* Compact, single-line button (no chunky 3-line bordered default).
       Auto width so it does not force an auto-width popup box wide. */
    .form-buttons { width: auto; height: auto; }
    .form-buttons Button { height: 1; min-width: 0; border: none; padding: 0 2; }
    """

    # Action keys. Enter is handled by the stock DataTable
    # (select_cursor -> RowSelected -> _on_row_selected). These
    # descriptions are not displayed anywhere (the footer hint bar is
    # custom, not Textual's Footer); kept meaningful for debugging.
    BINDINGS = [
        Binding("f,slash", "focus_filter", "filter"),
        Binding("i", "open_actions", "ipmi-action"),
        Binding("r", "refresh", "refresh"),
        Binding("a", "add_machine", "add"),
        Binding("e", "edit_machine", "edit"),
        Binding("d", "delete_machine", "delete"),
        Binding("g", "toggle_glyphs", "glyphs"),
        # Help: "h" is advertised; "space" is a quiet alias.
        Binding("h,space", "help", "help"),
        Binding("q", "quit", "quit"),
        Binding("escape", "blur_filter", "blur", show=False),
    ]

    def __init__(
        self,
        config: Config,
        config_path: Path,
        workers: int = DEFAULT_WORKERS,
        interval: float = DEFAULT_INTERVAL,
        created_path: Path | None = None,
    ) -> None:
        super().__init__()
        # Mutable list: CRUD operations splice in place + save back
        # to ``config_path``. ``_config_defaults`` round-trips the
        # operator's ``[defaults]`` block on every save.
        self._machines: list[Machine] = list(config.machines)
        # Set when this run created the config; on_mount tells the
        # operator where it landed instead of failing on a missing file.
        self._created_path = created_path
        self._config_defaults: dict = dict(config.defaults)
        self._config_path = config_path
        # Nerd Font glyphs on/off (``[ui].glyphs``); toggled at runtime
        # with "g". When off, ``_g`` yields "" so every glyph-bearing
        # bit of chrome falls back to plain text.
        self._glyphs: bool = config.glyphs
        self._scan_workers = workers
        self._interval = interval
        # Most recent probe results, keyed by ``Machine.name`` for
        # stable lookups across re-renders + filter changes.
        self._probes: dict[str, Probe] = {}
        self._filter_text: str = ""
        # Monotonic timestamp of the last completed scan; drives the
        # top-bar "N secs. since refresh" label. None until the first
        # scan returns.
        self._last_scan: float | None = None

    def _g(self, glyph: str) -> str:
        """``glyph`` plus a trailing space when glyphs are enabled,
        else an empty string, so every label falls back to plain
        text when ``self._glyphs`` is off."""
        return f"{glyph} " if self._glyphs else ""

    # ----- layout ----------------------------------------------------------

    def compose(self) -> ComposeResult:
        # Single top bar replacing the stock Header, all on one line:
        # app name | machine counts | filter input | scan age. No
        # clock; the terminal / multiplexer already shows the time.
        with Horizontal(id="topbar"):
            yield Static(f"{self._g(_APP_ICON)}ipmitui", id="app-title")
            yield Static("│", classes="sep")
            yield Static("", id="counts")
            yield Static("│", classes="sep")
            yield Label(f"{self._g(_FILTER_ICON)}\\[[$accent bold]f[/]]ilter:", id="filter-label")
            yield Input(placeholder="name, host, or note substring", id="filter")
            yield Static("│", classes="sep")
            yield Static("", id="age")
        # Heavy rules bracket the table.
        yield Rule(line_style="heavy")
        yield _MachineTable(zebra_stripes=True)
        yield Rule(line_style="heavy")
        # Footer hint bar (two Statics): actions on the left; help /
        # glyphs / quit pushed to the right. Shortcut letters are
        # bracketed + highlighted; heavy "┃" groups, light "│" within.
        with Horizontal(id="footer"):
            yield Static(
                "\\[[$accent bold]i[/]]pmi-action ┃ "
                "\\[[$accent bold]a[/]]dd │ "
                "\\[[$accent bold]e[/]]dit │ "
                "\\[[$accent bold]d[/]]elete",
                id="footer-left",
            )
            yield Static(
                "\\[[$accent bold]g[/]]lyphs │ "
                "\\[[$accent bold]h[/]]elp ┃ "
                "\\[[$accent bold]q[/]]uit",
                id="footer-right",
            )

    def on_mount(self) -> None:
        # ansi-dark draws from the terminal's own 16-color ANSI
        # palette instead of hardcoding one, so the TUI blends with
        # whatever the operator's terminal + multiplexer (here zellij,
        # whose default theme also inherits the terminal palette) are
        # already rendering. Swap to a fixed theme (nord, dracula,
        # tokyo-night) if a consistent look across terminals matters
        # more than matching the host.
        self.theme = "ansi-dark"
        self.title = "ipmitui"
        self._refresh_status()
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        # Column headers carry a glyph each (toggle with "g"); the
        # word always stays so they read without a Nerd Font.
        table.add_columns(*self._columns())
        # Seed table rows so the first paint is immediate; ``power``
        # column shows ``...`` until the first probe completes.
        for m in self._machines:
            table.add_row(m.name, m.host, "...", "", "", key=m.name)
        # Compose-order focus would land on the filter Input (first
        # focusable widget), but the operator's first gesture is
        # almost always to move the cursor with j/k or arrow keys.
        # Hand focus to the table; ``f`` is the deliberate route
        # into the filter when they want to narrow the list.
        table.focus()
        # Kick off the initial scan + the periodic refresh. The
        # ``Timer`` handle is kept so the SoL handoff can pause it
        # for the duration of the session (suspend() does not stop
        # timers; without this the periodic scan keeps opening fresh
        # IPMI sessions on every BMC while the operator's SoL
        # session is also holding one open, which can trip the
        # per-IP session cap on some firmware).
        self.run_worker(self._scan_async(), exclusive=True, group="scan")
        self._refresh_timer = self.set_interval(
            self._interval, self._tick_refresh, name="auto-refresh"
        )
        # Tick the relative "refreshed Ns ago" label once a second so
        # it counts up between scans without waiting for the next probe.
        self.set_interval(1.0, self._refresh_status, name="age")
        # First run with no config: say where we created it (the file is
        # empty, so the table is too) rather than having failed to start.
        if self._created_path is not None:
            self.notify(
                f"created {self._created_path}\nno machines yet; press a to add one",
                title="first run",
                timeout=10,
            )

    # ----- focus styling ---------------------------------------------------

    def on_app_blur(self, event: events.AppBlur) -> None:
        # Terminal/app lost focus: dim the app name so an inactive
        # pane reads as such (the stock Header behaved this way).
        del event
        self.query_one("#app-title", Static).add_class("-blurred")

    def on_app_focus(self, event: events.AppFocus) -> None:
        del event
        self.query_one("#app-title", Static).remove_class("-blurred")

    # ----- scan ------------------------------------------------------------

    @work(thread=True)
    def _scan_blocking(self) -> dict[str, Probe]:
        """Run all probes in parallel in a worker thread so the
        Textual event loop stays responsive."""
        from concurrent.futures import ThreadPoolExecutor

        if not self._machines:
            return {}
        workers = max(1, min(self._scan_workers, len(self._machines)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return dict(
                zip(
                    [m.name for m in self._machines],
                    pool.map(probe_power, self._machines),
                    strict=True,
                )
            )

    async def _scan_async(self) -> None:
        # Two cancellation sources can interrupt the inner thread
        # worker: the periodic ``_tick_refresh`` fires a new scan
        # with ``exclusive=True, group="scan"`` and supersedes any
        # in-flight one, and ``App.suspend()`` (the SoL handoff)
        # cancels workers as the app pauses. Either path makes
        # ``Worker.wait()`` raise ``WorkerCancelled``; treat that
        # as a clean bail rather than letting it bubble up as an
        # error popup.
        from textual.worker import WorkerCancelled

        try:
            result = await self._scan_blocking().wait()
        except (WorkerCancelled, asyncio.CancelledError):
            return
        self._probes = result
        self._last_scan = time.monotonic()
        self._repaint_table()
        self._refresh_status()

    def _tick_refresh(self) -> None:
        """Periodic refresh; cancels any in-flight scan to keep the
        worker pool from piling up if a BMC is slow."""
        self.run_worker(self._scan_async(), exclusive=True, group="scan")

    def _last_refresh_text(self) -> str:
        """Scan age for the top-right slot: ``N secs. since [r]efresh
        <glyph>`` (just the ``[r]efresh`` hint before the first scan).
        Whole seconds, no "just now" special case: a word that later
        turns into a number shifts the layout width, which reads as
        jitter in the top-right corner."""
        # "[r]efresh" (+ glyph) also serves as the refresh-key hint, so
        # the refresh action lives here rather than in the footer.
        hint = "\\[[$accent bold]r[/]]efresh"
        if self._glyphs:
            hint += f" {_REFRESH_ICON}"
        if self._last_scan is None:
            return hint
        secs = max(0, int(time.monotonic() - self._last_scan))
        return f"{secs} secs. since {hint}"

    def _refresh_status(self) -> None:
        """Refresh the top bar's dynamic bits: the machine count and
        the scan age. Called after each scan, after CRUD, and once a
        second so the age counts up between scans."""
        self.query_one("#counts", Static).update(
            f"{self._g(_MACHINES_ICON)}{len(self._machines)} machines"
        )
        self.query_one("#age", Static).update(self._last_refresh_text())

    def _columns(self) -> list[str]:
        """Table column headers, each prefixed with its glyph when
        glyphs are enabled (toggle with "g")."""
        return [f"{self._g(_COLUMN_ICON[c])}{c}" for c in ("name", "host", "power", "ms", "note")]

    # ----- table -----------------------------------------------------------

    def _filtered_machines(self) -> list[Machine]:
        if not self._filter_text:
            return self._machines
        needle = self._filter_text.lower()
        # Match name, host, or note/description so an operator can find
        # a box by its rack label as well as its identity / address.
        return [
            m
            for m in self._machines
            if needle in m.name.lower()
            or needle in m.host.lower()
            or needle in (m.description or "").lower()
        ]

    def _repaint_table(self) -> None:
        table = self.query_one(DataTable)
        # Preserve cursor position across re-renders when possible:
        # remember the name of the highlighted row, rebuild, restore.
        highlighted = self._highlighted_name()
        table.clear()
        for m in self._filtered_machines():
            p = self._probes.get(m.name)
            state = p.state if p else "..."
            ms = f"{p.elapsed * 1000:.0f}" if p else ""
            # Show the operator's description in the note column; fall
            # back to the probe note (timeout / error detail) for
            # machines without one so failures still surface.
            note = m.description or (p.note if p else "")
            # No per-row state glyph: the bolt lives in the column
            # header only. Rows stay plain styled text.
            styled = f"[{_STATE_STYLE[state]}]{state}[/]" if state in _STATE_STYLE else state
            table.add_row(m.name, m.host, styled, ms, note, key=m.name)
        if highlighted:
            with contextlib.suppress(Exception):
                table.move_cursor(row=table.get_row_index(highlighted))

    def _highlighted_name(self) -> str | None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        try:
            key = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value
        except Exception:
            return None
        return str(key) if key is not None else None

    def _selected_machine(self) -> Machine | None:
        name = self._highlighted_name()
        if name is None:
            return None
        return next((m for m in self._machines if m.name == name), None)

    # ----- filter input ----------------------------------------------------

    def action_focus_filter(self) -> None:
        # Only jump into the filter from the table. While a popup
        # (add / edit / action picker) is active, or the filter Input
        # already owns focus, "f" must type normally / do nothing
        # rather than hijack focus on the screen underneath.
        if not isinstance(self.focused, DataTable):
            return
        self.query_one("#filter", Input).focus()

    def action_blur_filter(self) -> None:
        # Esc from inside the filter clears it and returns focus to
        # the table so the operator can keep navigating with j/k.
        # Outside the filter Esc is a no-op.
        inp = self.query_one("#filter", Input)
        if inp.has_focus:
            inp.value = ""
            self._filter_text = ""
            self._repaint_table()
            self.query_one(DataTable).focus()

    # Scope to "#filter": the CRUD form's own Input fields also emit
    # Input.Changed / Submitted, and an unscoped handler would treat
    # those as filter text and blank the table after an edit.
    @on(Input.Changed, "#filter")
    def _on_filter_changed(self, event: Input.Changed) -> None:
        self._filter_text = event.value.strip()
        self._repaint_table()

    @on(Input.Submitted, "#filter")
    def _on_filter_submitted(self, event: Input.Submitted) -> None:
        # Enter in the filter returns focus to the table so the
        # operator can act on the filtered set without the filter
        # eating subsequent keystrokes. The filter text stays
        # applied; "f" re-focuses to edit it, Esc to clear.
        del event
        self.query_one(DataTable).focus()

    # ----- actions ---------------------------------------------------------

    def action_refresh(self) -> None:
        self.notify("scanning")
        self.run_worker(self._scan_async(), exclusive=True, group="scan")

    def action_help(self) -> None:
        # Only from the table: while the filter is focused, "h" /
        # space type normally; while a popup is up, don't stack.
        if not isinstance(self.focused, DataTable):
            return
        self.push_screen(_HelpScreen())

    def action_toggle_glyphs(self) -> None:
        # Only from the table, so "g" typed into the filter is literal.
        if not isinstance(self.focused, DataTable):
            return
        self._glyphs = not self._glyphs
        self._apply_glyphs()
        self.notify(f"glyphs {'on' if self._glyphs else 'off'}")

    def _apply_glyphs(self) -> None:
        """Re-render every glyph-bearing bit of chrome after a toggle:
        app title, filter label, table columns, counts + age."""
        self.query_one("#app-title", Static).update(f"{self._g(_APP_ICON)}ipmitui")
        self.query_one("#filter-label", Label).update(
            f"{self._g(_FILTER_ICON)}\\[[$accent bold]f[/]]ilter:"
        )
        table = self.query_one(DataTable)
        table.clear(columns=True)
        table.add_columns(*self._columns())
        self._repaint_table()
        self._refresh_status()

    @on(DataTable.RowSelected)
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        # Fired when the operator commits a row with Enter (or a
        # click) while the table has focus: the stock ``DataTable``
        # ``select_cursor`` action posts ``RowSelected``, which is how
        # the picker opens (the "i" key is the other route).
        del event
        self.action_open_actions()

    def action_open_actions(self) -> None:
        """Open the per-machine action picker. Bound to row-commit
        (Enter / click) rather than any specific power op so cursor
        movement in the table is completely free of side effects:
        the operator has to commit twice (Enter to open the picker,
        hotkey to choose) before anything fires. Triggered by Enter
        (row commit) or the "i" key."""
        if not isinstance(self.focused, DataTable):
            # Only from the table: Enter/"i" typed into the filter or
            # while a popup is open must not open (or stack) the picker.
            return
        m = self._selected_machine()
        if m is None:
            self.notify("no machine selected", severity="warning")
            return
        probe = self._probes.get(m.name)
        self.push_screen(_ActionScreen(m, probe, self._glyphs), self._dispatch_action_for(m))

    def _dispatch_action_for(self, m: Machine):
        """Returns a callback the ``_ActionScreen.dismiss`` value gets
        passed to. Captures the machine identity at modal-push time
        so a later table re-render (the periodic scan can repaint
        between push and dismiss) does not redirect the action to a
        different row."""

        def _dispatch(op: str | None) -> None:
            if op is None:
                return
            if op == "sol":
                self._do_sol(m)
            else:
                self._fire_power(m, op)

        return _dispatch

    def _do_sol(self, m: Machine) -> None:
        """``App.suspend()`` tears down the alt-screen + mouse
        tracking + raw-mode input, hands the terminal to ipmitool
        for the duration of the SoL session, then re-mounts when
        ipmitool exits. The operator stays inside ipmitui across
        SoL sessions instead of having to relaunch the binary.

        Pause the periodic refresh while ipmitool owns the
        terminal: ``App.suspend`` does NOT stop timers, so without
        this the auto-refresh keeps opening fresh IPMI sessions on
        every BMC each tick (Supermicro / similar firmware caps
        concurrent sessions per source IP and starts refusing).
        Refresh resumes when ipmitool exits.
        """
        args = m.ipmi_args() + ["sol", "activate"]
        self._refresh_timer.pause()
        with self.suspend():
            # Banner stays in scrollback for the duration of the
            # session so an operator who forgot the vocabulary can
            # scroll up to find it. ``~?`` also prints the same set
            # live, which is the on-demand reference.
            print(
                "\n".join(
                    [
                        "",
                        f"  SoL: {m.name} ({m.host})",
                        "  ~.   disconnect             ~?   help",
                        "  ~B   send break             ~~   literal tilde",
                        "  ~^Z  suspend                (double the tilde over SSH: ~~.)",
                        "",
                    ]
                ),
                flush=True,
            )
            subprocess.run(args, check=False)
        self._refresh_timer.resume()
        self.notify(f"SoL to {m.name} ended; re-scanning")
        self.run_worker(self._scan_async(), exclusive=True, group="scan")

    @work(thread=True, exclusive=False)
    def _fire_power(self, m: Machine, op: str) -> None:
        rc = power_op(m, op)
        if rc == 0:
            self.call_from_thread(self.notify, f"{m.name}: power {op} OK")
        else:
            self.call_from_thread(
                self.notify, f"{m.name}: power {op} FAILED (exit {rc})", severity="error"
            )
        # Re-scan so the table reflects the new state right after the op.
        self.call_from_thread(self.run_worker, self._scan_async(), exclusive=True, group="scan")

    # ----- CRUD ------------------------------------------------------------

    def action_add_machine(self) -> None:
        if self.query_one("#filter", Input).has_focus:
            return
        self.push_screen(_MachineFormScreen(None, self._glyphs), self._on_add_saved)

    def action_edit_machine(self) -> None:
        if self.query_one("#filter", Input).has_focus:
            return
        m = self._selected_machine()
        if m is None:
            self.notify("no machine selected", severity="warning")
            return
        self.push_screen(_MachineFormScreen(m, self._glyphs), self._on_edit_saved(m))

    def action_delete_machine(self) -> None:
        if self.query_one("#filter", Input).has_focus:
            return
        m = self._selected_machine()
        if m is None:
            self.notify("no machine selected", severity="warning")
            return
        self.push_screen(_DeleteConfirmScreen(m), self._on_delete_confirmed(m))

    def _on_add_saved(self, new: Machine | None) -> None:
        if new is None:
            return
        if any(m.name == new.name for m in self._machines):
            self.notify(f"name {new.name!r} already exists", severity="error")
            return
        self._machines.append(new)
        self._persist_and_repaint(f"added {new.name}")

    def _on_edit_saved(self, original: Machine):
        # Captures ``original`` at modal-push so the post-dismiss
        # callback edits the correct row even if the user pressed
        # ``e`` again after moving the cursor while typing.
        def _apply(updated: Machine | None) -> None:
            if updated is None:
                return
            if updated.name != original.name and any(
                m.name == updated.name for m in self._machines
            ):
                self.notify(f"name {updated.name!r} already exists", severity="error")
                return
            idx = self._machines.index(original)
            self._machines[idx] = updated
            self._persist_and_repaint(f"updated {updated.name}")

        return _apply

    def _on_delete_confirmed(self, m: Machine):
        def _apply(confirmed: bool | None) -> None:
            if not confirmed:
                return
            self._machines.remove(m)
            self._persist_and_repaint(f"removed {m.name}")

        return _apply

    def _persist_and_repaint(self, summary: str) -> None:
        """Save the machine list back to disk + rebuild the table +
        kick a rescan. Used by every CRUD path."""
        try:
            save_config(
                self._config_path, self._machines, self._config_defaults, glyphs=self._glyphs
            )
        except OSError as exc:
            self.notify(f"save failed: {exc}", severity="error")
            return
        # Drop the now-stale probes for machines no longer in the
        # list so they do not haunt the next repaint as ghost rows.
        self._probes = {
            name: p
            for name, p in self._probes.items()
            if any(m.name == name for m in self._machines)
        }
        # ``_repaint_table`` clears + rebuilds from the (filtered)
        # machine list, so removed rows drop out and new rows appear.
        self._repaint_table()
        self._refresh_status()
        self.notify(summary)
        self.run_worker(self._scan_async(), exclusive=True, group="scan")


class _MachineFormScreen(ModalScreen[Machine | None]):
    """Add or edit a machine. ``existing`` pre-fills the form for an
    edit and is None for an add. The Save button, ``Alt+S`` or Enter
    saves and returns the new :class:`Machine`; ``Esc`` returns
    ``None``.

    The form is intentionally minimal: name, host, user, plus EITHER
    an inline password OR a pass_cmd. Validation is light (name +
    host must be non-empty); the operator can paste a
    pass-store-style command for non-plaintext secrets."""

    # Alt+S rather than Ctrl+S: Ctrl+S is widely intercepted (terminal
    # flow-control / "save" in host apps). priority so it fires while
    # an Input field has focus. The Save button gives a mouse path;
    # Esc cancels, so no Cancel button is needed.
    BINDINGS = [
        Binding("alt+s", "save", "save", priority=True),
        # Esc cancels. No "q" here: the form has text fields where "q"
        # must type, not cancel.
        Binding("escape", "cancel", "cancel"),
    ]

    def __init__(self, existing: Machine | None, glyphs: bool = True) -> None:
        super().__init__()
        self._existing = existing
        self._glyphs = glyphs

    def compose(self) -> ComposeResult:
        def g(field: str) -> str:
            return f"{_FORM_ICON[field]} " if self._glyphs else ""

        ex = self._existing
        title = f"Edit {ex.name}" if ex else "Add machine"
        with Vertical(classes="form-box"):
            yield Label(title, classes="form-title")
            # One row per field: label on the left, single-line input
            # on the right (keeps the modal short).
            with Horizontal(classes="form-field"):
                yield Label(f"{g('name')}name")
                yield Input(value=ex.name if ex else "", id="f-name", placeholder="warp-bmc")
            with Horizontal(classes="form-field"):
                yield Label(f"{g('host')}host")
                yield Input(
                    value=ex.host if ex else "",
                    id="f-host",
                    placeholder="warp-bmc or 10.20.30.121",
                )
            with Horizontal(classes="form-field"):
                yield Label(f"{g('user')}user")
                yield Input(value=ex.user if ex else "ADMIN", id="f-user", placeholder="ADMIN")
            with Horizontal(classes="form-field"):
                yield Label(f"{g('password')}password")
                yield Input(
                    value=ex.password or "" if ex else "",
                    id="f-pass",
                    password=True,
                    placeholder="inline secret",
                )
            with Horizontal(classes="form-field"):
                yield Label(f"{g('pass_cmd')}pass_cmd")
                yield Input(
                    value=ex.pass_cmd or "" if ex else "",
                    id="f-passcmd",
                    placeholder="shell cmd; first stdout line is the password",
                )
            with Horizontal(classes="form-field"):
                yield Label(f"{g('note')}note")
                yield Input(
                    value=ex.description or "" if ex else "",
                    id="f-desc",
                    placeholder="e.g. rack3 top, NVMe shelf",
                )
            # Only a Save button (Enter / Alt+S also save); Esc cancels,
            # so no Cancel button is needed.
            with Horizontal(classes="form-buttons"):
                yield Button("Save (Alt+S)", id="save", variant="primary")
            yield Label("cancel: [$accent bold]Esc[/]", classes="form-keys")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        del event
        self.action_save()

    def action_save(self) -> None:
        name = self.query_one("#f-name", Input).value.strip()
        host = self.query_one("#f-host", Input).value.strip()
        user = self.query_one("#f-user", Input).value.strip() or "ADMIN"
        password = self.query_one("#f-pass", Input).value.strip() or None
        pass_cmd = self.query_one("#f-passcmd", Input).value.strip() or None
        description = self.query_one("#f-desc", Input).value.strip() or None
        if not name or not host:
            # Stay open; the operator can see the empty fields and
            # fill them in. A future tweak could flash a red border.
            return
        self.dismiss(
            Machine(
                name=name,
                host=host,
                user=user,
                password=password,
                pass_cmd=pass_cmd,
                description=description,
            )
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


class _DeleteConfirmScreen(ModalScreen[bool]):
    """Yes/no for delete. Splitting from ``_ActionScreen`` keeps the
    primary action picker focused on power-state ops while delete
    has its own dedicated confirm so the operator cannot wipe a row
    with a single keystroke."""

    BINDINGS = [
        Binding("y,enter", "confirm", "confirm"),
        Binding("n,escape,q", "cancel", "cancel"),
    ]

    def __init__(self, machine: Machine) -> None:
        super().__init__()
        self._machine = machine

    def compose(self) -> ComposeResult:
        with Vertical(classes="action-box"):
            yield Label(
                f"Remove {self._machine.name} ({self._machine.host}) from the config?",
                classes="action-title",
            )
            yield Label(
                "\\[[$accent bold]y[/]]es  /  \\[[$accent bold]n[/]]o",
                classes="action-body",
            )

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class _HelpScreen(ModalScreen[None]):
    """Key-binding reference. Opened with "h" or Space; dismissed with
    Esc / q / h / Space. Keys are shown in a bold-accent column so they
    read with or without colour."""

    BINDINGS = [
        Binding("escape,q,h,space", "close", "close"),
    ]

    # (keys, description); the column layout itself signals "shortcut".
    _ROWS = [
        ("f, /", "filter (matches name / host / note)"),
        ("i, Enter", "ipmi-action: power / SoL picker"),
        ("a", "add a machine"),
        ("e", "edit the selected machine"),
        ("d", "delete the selected machine"),
        ("r", "refresh now"),
        ("g", "toggle Nerd Font glyphs"),
        ("h, Space", "this help"),
        ("q", "quit"),
        ("Esc", "clear filter / close a popup"),
    ]

    def compose(self) -> ComposeResult:
        body = "\n".join(
            f"[$accent bold]{k}[/]{' ' * max(2, 12 - len(k))}{d}" for k, d in self._ROWS
        )
        with Vertical(classes="action-box"):
            # Header: name + version + author at the top.
            yield Label(
                f"ipmitui v{__version__} by Simon A. F. Lund (safl.dk)",
                classes="action-title",
            )
            yield Static(body, classes="action-body")
            yield Label("close: [$accent bold]Esc / q[/]", classes="action-foot")

    def action_close(self) -> None:
        self.dismiss()


def run_tui(
    config: Config,
    config_path: Path,
    workers: int = DEFAULT_WORKERS,
    interval: float = DEFAULT_INTERVAL,
    created_path: Path | None = None,
) -> int:
    """Entry point used by ``ipmitui tui``. SoL handoff happens
    inline via ``App.suspend()``, so this function just runs the
    app to completion.

    ``mouse=False``: this is a keyboard-driven surface, and leaving
    Textual's mouse tracking on hijacks the terminal's own
    click-drag text selection / copy, which operators find annoying.
    With it off the driver writes no mouse escape sequences (the
    SoL suspend/resume mouse toggles become no-ops too)."""
    IpmituiApp(
        config, config_path, workers=workers, interval=interval, created_path=created_path
    ).run(mouse=False)
    return 0
