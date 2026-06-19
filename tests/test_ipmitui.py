"""Unit tests for ipmitui. Network / ipmitool calls are stubbed via
``unittest.mock`` so the suite is hermetic and fast."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import ipmitui  # noqa: E402


def _write_cfg(body: str) -> Path:
    tmp = Path(tempfile.mkdtemp())
    cfg = tmp / "machines.toml"
    cfg.write_text(body)
    return cfg


class TestConfig(unittest.TestCase):
    def test_load_with_defaults_merged_per_machine(self):
        cfg = _write_cfg(
            """
[defaults]
user = "ADMIN"
pass_cmd = "echo secret-{name}"

[[machine]]
name = "lab-01"
host = "10.0.0.10"

[[machine]]
name = "lab-02"
host = "10.0.0.11"
user = "OTHER"
"""
        )
        cfg_obj = ipmitui.load_config(cfg)
        ms = cfg_obj.machines
        self.assertEqual([m.name for m in ms], ["lab-01", "lab-02"])
        self.assertEqual(ms[0].user, "ADMIN")
        self.assertEqual(ms[0].pass_cmd, "echo secret-lab-01")
        self.assertEqual(ms[1].user, "OTHER")  # overrides default
        self.assertEqual(ms[1].pass_cmd, "echo secret-lab-02")
        # The raw defaults block is preserved on the Config so a CRUD
        # save can round-trip it back without touching the operator's
        # pass_cmd template.
        self.assertEqual(cfg_obj.defaults["user"], "ADMIN")
        self.assertEqual(cfg_obj.defaults["pass_cmd"], "echo secret-{name}")

    def test_secret_runs_pass_cmd_when_set(self):
        m = ipmitui.Machine(name="x", host="h", user="u", pass_cmd="printf 'p\\n'")
        self.assertEqual(m.secret(), "p")

    def test_secret_prefers_inline_password(self):
        m = ipmitui.Machine(
            name="x", host="h", user="u", password="p", pass_cmd="echo SHOULD_NOT_RUN"
        )
        self.assertEqual(m.secret(), "p")

    def test_load_missing_file_is_empty_not_error(self):
        # A missing config is no longer fatal: it loads as an empty
        # fleet so a first run can still come up.
        cfg = ipmitui.load_config(Path("/nonexistent/bmc/ipmitui.toml"))
        self.assertEqual(cfg.machines, [])
        self.assertEqual(cfg.defaults, {})

    def test_ensure_config_creates_starter_then_is_idempotent(self):
        path = Path(tempfile.mkdtemp()) / "sub" / "ipmitui.toml"
        self.assertFalse(path.exists())
        self.assertTrue(ipmitui.ensure_config(path))  # created
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        # Round-trips as a valid, machine-less config.
        cfg = ipmitui.load_config(path)
        self.assertEqual(cfg.machines, [])
        self.assertFalse(ipmitui.ensure_config(path))  # already there

    def test_default_config_path_prefers_legacy_when_only_it_exists(self):
        with (
            mock.patch.object(ipmitui.os, "environ", {}),
            mock.patch.object(ipmitui, "DEFAULT_CONFIG") as new,
            mock.patch.object(ipmitui, "LEGACY_CONFIG") as legacy,
        ):
            new.exists.return_value = False
            legacy.exists.return_value = True
            self.assertIs(ipmitui.default_config_path(), legacy)
            new.exists.return_value = True
            self.assertIs(ipmitui.default_config_path(), new)

    def test_load_merges_multiple_configs_dedup_primary_wins(self):
        a = _write_cfg('[[machine]]\nname = "m1"\nhost = "h1"\n')
        b = _write_cfg(
            '[[machine]]\nname = "m2"\nhost = "h2"\n\n[[machine]]\nname = "m1"\nhost = "dup"\n'
        )
        cfg = ipmitui.load_config([a, b])
        self.assertEqual([m.name for m in cfg.machines], ["m1", "m2"])
        # The earlier (primary) file wins on a name clash.
        self.assertEqual(next(m for m in cfg.machines if m.name == "m1").host, "h1")

    def test_glyphs_ui_option(self):
        on = ipmitui.load_config(
            _write_cfg('[ui]\nglyphs = true\n[[machine]]\nname="a"\nhost="h"\n')
        )
        off = ipmitui.load_config(
            _write_cfg('[ui]\nglyphs = false\n[[machine]]\nname="a"\nhost="h"\n')
        )
        default = ipmitui.load_config(_write_cfg('[[machine]]\nname="a"\nhost="h"\n'))
        self.assertTrue(on.glyphs)
        self.assertFalse(off.glyphs)
        self.assertTrue(default.glyphs)

    def test_save_config_round_trips_glyphs(self):
        tmp = Path(tempfile.mkdtemp()) / "machines.toml"
        ms = [ipmitui.Machine(name="a", host="h", user="u")]
        ipmitui.save_config(tmp, ms, defaults=None, glyphs=False)
        self.assertFalse(ipmitui.load_config(tmp).glyphs)


class TestClassify(unittest.TestCase):
    def test_unreachable_hint(self):
        st, _ = ipmitui._classify("...\nUnable to establish IPMI v2 / RMCP+ session")
        self.assertEqual(st, "unreachable")

    def test_auth_hint(self):
        st, _ = ipmitui._classify("RAKP 2 HMAC is invalid")
        self.assertEqual(st, "auth-fail")

    def test_error_fallback(self):
        st, _ = ipmitui._classify("some random failure")
        self.assertEqual(st, "error")


class TestProbe(unittest.TestCase):
    def test_probe_parses_chassis_is_on(self):
        m = ipmitui.Machine(name="x", host="h", user="u", password="p")
        with mock.patch("ipmitui.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="Chassis Power is on\n", stderr="")
            p = ipmitui.probe_power(m)
        self.assertEqual(p.state, "on")

    def test_probe_parses_chassis_is_off(self):
        m = ipmitui.Machine(name="x", host="h", user="u", password="p")
        with mock.patch("ipmitui.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="Chassis Power is off\n", stderr="")
            p = ipmitui.probe_power(m)
        self.assertEqual(p.state, "off")

    def test_probe_maps_timeout_to_unreachable(self):
        import subprocess

        m = ipmitui.Machine(name="x", host="h", user="u", password="p")
        with mock.patch("ipmitui.subprocess.run") as run:
            run.side_effect = subprocess.TimeoutExpired(cmd="ipmitool", timeout=5)
            p = ipmitui.probe_power(m)
        self.assertEqual(p.state, "unreachable")
        self.assertEqual(p.note, "timeout")

    def test_probe_classifies_auth_failure(self):
        m = ipmitui.Machine(name="x", host="h", user="u", password="p")
        with mock.patch("ipmitui.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="RAKP 2 HMAC invalid")
            p = ipmitui.probe_power(m)
        self.assertEqual(p.state, "auth-fail")


class TestFindMachine(unittest.TestCase):
    def test_returns_match(self):
        ms = [ipmitui.Machine(name="a", host="h", user="u", password="p")]
        self.assertEqual(ipmitui.find_machine(ms, "a").name, "a")

    def test_missing_exits_with_known_list(self):
        ms = [ipmitui.Machine(name="a", host="h", user="u", password="p")]
        with self.assertRaises(SystemExit) as cm:
            ipmitui.find_machine(ms, "z")
        self.assertIn("a", str(cm.exception))


class TestTuiApp(unittest.TestCase):
    """``IpmituiApp`` cannot be ``run()`` in a unit test (Textual would
    take over the terminal), but its constructor + helpers are unit-
    testable in isolation. The most important guard here is the
    name-collision regression: ``App.workers`` is a property backed
    by ``self._workers`` (Textual's WorkerManager), so any attribute
    on a subclass that lands as ``self._workers`` clobbers the entire
    worker subsystem and makes the unmount path crash with
    ``'int' object has no attribute 'cancel_node'`` once the app
    exits. Catch that early."""

    def _import_tui(self):
        try:
            from ipmitui import _tui
        except ImportError as exc:
            self.skipTest(f"textual not installed: {exc}")
        return _tui

    def _build_app(self, machines):
        tui = self._import_tui()
        cfg = ipmitui.Config(machines=machines, defaults={})
        return tui.IpmituiApp(cfg, Path("/tmp/never-written.toml"), workers=4, interval=2.0)

    def test_workers_property_still_returns_textual_worker_manager(self):
        """The ``workers`` attribute on the App must remain Textual's
        ``WorkerManager``; if a subclass attribute named ``_workers``
        is introduced, Textual's property returns it instead and the
        unmount path crashes."""
        from textual.worker_manager import WorkerManager

        app = self._build_app([ipmitui.Machine(name="a", host="h", user="u", password="p")])
        self.assertIsInstance(app.workers, WorkerManager)

    def test_scan_workers_value_threaded_through_constructor(self):
        """The constructor's ``workers`` argument must land on a
        differently-named attribute so it does not collide with
        Textual's worker manager. Asserts the chosen storage."""
        app = self._build_app([ipmitui.Machine(name="a", host="h", user="u", password="p")])
        self.assertEqual(app._scan_workers, 4)

    def test_last_refresh_text_formats(self):
        """The top-right age is "N secs. since [r]efresh <glyph>" (the
        "r" + glyph double as the refresh-key hint); no "just now" word
        (which would jitter the width). Before the first scan it is
        just the hint."""
        import time

        tui = self._import_tui()
        hint = f"\\[[$accent bold]r[/]]efresh {tui._REFRESH_ICON}"
        app = self._build_app([ipmitui.Machine(name="a", host="h", user="u", password="p")])
        self.assertEqual(app._last_refresh_text(), hint)
        app._last_scan = time.monotonic()
        self.assertEqual(app._last_refresh_text(), f"0 secs. since {hint}")
        app._last_scan = time.monotonic() - 5
        self.assertEqual(app._last_refresh_text(), f"5 secs. since {hint}")

    def test_filtered_machines_matches_name_host_or_note(self):
        tui = self._import_tui()
        ms = [
            ipmitui.Machine(name="warp-bmc", host="warp-bmc", user="u", password="p"),
            ipmitui.Machine(name="wave-bmc", host="wave-bmc", user="u", password="p"),
            ipmitui.Machine(
                name="other", host="10.20.30.5", user="u", password="p", description="rack3 top"
            ),
        ]
        cfg = ipmitui.Config(machines=ms, defaults={})
        app = tui.IpmituiApp(cfg, Path("/tmp/never-written.toml"), workers=4, interval=2.0)
        app._filter_text = "wave"
        self.assertEqual([m.name for m in app._filtered_machines()], ["wave-bmc"])
        app._filter_text = "30.5"
        self.assertEqual([m.name for m in app._filtered_machines()], ["other"])
        # Match on the note / description too.
        app._filter_text = "rack3"
        self.assertEqual([m.name for m in app._filtered_machines()], ["other"])
        app._filter_text = ""
        self.assertEqual([m.name for m in app._filtered_machines()], [m.name for m in ms])


class TestSaveConfigRoundTrip(unittest.TestCase):
    """``save_config`` writes a TOML the loader accepts. CRUD edits
    that travel through the in-TUI form must survive a save + reload
    cycle without losing fields or scrambling the defaults block."""

    def test_round_trip_preserves_defaults_and_per_machine_fields(self):
        tmp = Path(tempfile.mkdtemp())
        cfg_path = tmp / "machines.toml"
        ms = [
            ipmitui.Machine(
                name="alpha",
                host="10.0.0.10",
                user="ADMIN",
                password="aa",
                description="rack3 top, NVMe shelf",
            ),
            ipmitui.Machine(name="beta", host="10.0.0.11", user="OTHER", pass_cmd="echo bb"),
        ]
        ipmitui.save_config(cfg_path, ms, defaults={"user": "ADMIN", "extra": "kept"})
        loaded = ipmitui.load_config(cfg_path)
        # Defaults round-trip.
        self.assertEqual(loaded.defaults, {"user": "ADMIN", "extra": "kept"})
        # Per-machine fields round-trip.
        names = [m.name for m in loaded.machines]
        self.assertEqual(names, ["alpha", "beta"])
        alpha, beta = loaded.machines
        self.assertEqual((alpha.host, alpha.user, alpha.password), ("10.0.0.10", "ADMIN", "aa"))
        self.assertIsNone(alpha.pass_cmd)
        self.assertEqual(alpha.description, "rack3 top, NVMe shelf")
        self.assertEqual((beta.host, beta.user, beta.pass_cmd), ("10.0.0.11", "OTHER", "echo bb"))
        self.assertIsNone(beta.password)
        self.assertIsNone(beta.description)
        # File permissions are 600 (password may be plaintext).
        mode = cfg_path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_omits_password_and_pass_cmd_when_unset(self):
        tmp = Path(tempfile.mkdtemp())
        cfg_path = tmp / "machines.toml"
        ms = [ipmitui.Machine(name="x", host="h", user="u")]
        ipmitui.save_config(cfg_path, ms, defaults=None)
        body = cfg_path.read_text()
        self.assertIn('name = "x"', body)
        self.assertNotIn("password", body)
        self.assertNotIn("pass_cmd", body)


class TestTuiInteraction(unittest.IsolatedAsyncioTestCase):
    """Drives the live app through Textual's Pilot to lock in the
    operator key flow that regressed once ``DataTable`` grew its own
    ``enter -> select_cursor`` binding (Textual 8.x): ``/`` focuses
    the filter, typing narrows the list, Enter in the filter returns
    focus to the table, Enter on a row opens the action picker, and
    the arrow keys move the cursor."""

    async def _run(self):
        try:
            from ipmitui import _tui
        except ImportError as exc:  # textual missing in the env
            self.skipTest(f"textual not installed: {exc}")
        return _tui

    async def test_filter_focus_enter_and_arrow_navigation(self):
        _tui = await self._run()
        from textual.widgets import DataTable, Input

        ms = [
            ipmitui.Machine(name="alpha", host="10.0.0.10", user="u", password="p"),
            ipmitui.Machine(name="beta", host="10.0.0.11", user="u", password="p"),
        ]
        cfg = ipmitui.Config(machines=ms, defaults={})
        # interval huge so the periodic scan never ticks mid-test.
        app = _tui.IpmituiApp(cfg, Path("/tmp/never-written.toml"), workers=2, interval=1e6)

        with mock.patch.object(_tui, "probe_power", return_value=ipmitui.Probe("on", 0.01)):
            async with app.run_test() as pilot:
                await pilot.pause()
                table = app.query_one(DataTable)
                inp = app.query_one(Input)

                # Table owns focus on launch; arrows navigate it.
                self.assertTrue(table.has_focus)
                self.assertEqual(app._highlighted_name(), "alpha")
                await pilot.press("down")
                self.assertEqual(app._highlighted_name(), "beta")
                await pilot.press("up")
                self.assertEqual(app._highlighted_name(), "alpha")

                # "f" jumps focus into the filter; typing narrows.
                await pilot.press("f")
                self.assertTrue(inp.has_focus)
                await pilot.press("b", "e", "t", "a")
                self.assertEqual([m.name for m in app._filtered_machines()], ["beta"])

                # Enter in the filter returns focus to the table without
                # opening the picker.
                await pilot.press("enter")
                await pilot.pause()
                self.assertTrue(table.has_focus)
                self.assertNotIsInstance(app.screen, _tui._ActionScreen)

                # Enter on the (single, filtered) row opens the picker
                # for the right machine.
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, _tui._ActionScreen)
                self.assertEqual(app.screen._machine.name, "beta")

    async def test_filter_key_ignored_while_modal_active(self):
        _tui = await self._run()
        from textual.widgets import Input

        ms = [ipmitui.Machine(name="a", host="h", user="u", password="p")]
        cfg = ipmitui.Config(machines=ms, defaults={})
        app = _tui.IpmituiApp(cfg, Path("/tmp/never-written.toml"), workers=1, interval=1e6)
        with mock.patch.object(_tui, "probe_power", return_value=ipmitui.Probe("on", 0.01)):
            async with app.run_test() as pilot:
                await pilot.pause()
                # Open the add-machine popup, then press "f".
                await pilot.press("a")
                await pilot.pause()
                self.assertIsInstance(app.screen, _tui._MachineFormScreen)
                await pilot.press("f")
                await pilot.pause()
                # The popup stays up and "f" typed into the form rather
                # than hijacking focus to the background filter input.
                self.assertIsInstance(app.screen, _tui._MachineFormScreen)
                self.assertFalse(app.query_one("#topbar Input", Input).has_focus)

    async def test_slash_also_focuses_filter(self):
        _tui = await self._run()
        from textual.widgets import Input

        ms = [ipmitui.Machine(name="a", host="h", user="u", password="p")]
        cfg = ipmitui.Config(machines=ms, defaults={})
        app = _tui.IpmituiApp(cfg, Path("/tmp/never-written.toml"), workers=1, interval=1e6)
        with mock.patch.object(_tui, "probe_power", return_value=ipmitui.Probe("on", 0.01)):
            async with app.run_test() as pilot:
                await pilot.pause()
                # "/" is an alias for "f" from the table.
                await pilot.press("slash")
                self.assertTrue(app.query_one("#filter", Input).has_focus)

    async def test_i_key_opens_action_picker(self):
        _tui = await self._run()

        ms = [ipmitui.Machine(name="a", host="h", user="u", password="p")]
        cfg = ipmitui.Config(machines=ms, defaults={})
        app = _tui.IpmituiApp(cfg, Path("/tmp/never-written.toml"), workers=1, interval=1e6)
        with mock.patch.object(_tui, "probe_power", return_value=ipmitui.Probe("on", 0.01)):
            async with app.run_test() as pilot:
                await pilot.pause()
                # "i" is a trigger for the picker, alongside Enter.
                await pilot.press("i")
                await pilot.pause()
                self.assertIsInstance(app.screen, _tui._ActionScreen)

    async def test_app_title_dims_on_blur(self):
        _tui = await self._run()
        from textual import events

        ms = [ipmitui.Machine(name="a", host="h", user="u", password="p")]
        cfg = ipmitui.Config(machines=ms, defaults={})
        app = _tui.IpmituiApp(cfg, Path("/tmp/never-written.toml"), workers=1, interval=1e6)
        with mock.patch.object(_tui, "probe_power", return_value=ipmitui.Probe("on", 0.01)):
            async with app.run_test() as pilot:
                await pilot.pause()
                title = app.query_one("#app-title")
                self.assertFalse(title.has_class("-blurred"))
                app.post_message(events.AppBlur())
                await pilot.pause()
                self.assertTrue(title.has_class("-blurred"))
                app.post_message(events.AppFocus())
                await pilot.pause()
                self.assertFalse(title.has_class("-blurred"))

    async def test_counts_and_frame_scoping(self):
        _tui = await self._run()
        from textual.widgets import Input, Static

        ms = [
            ipmitui.Machine(name="alpha", host="10.0.0.10", user="u", password="p"),
            ipmitui.Machine(name="beta", host="10.0.0.11", user="u", password="p"),
            ipmitui.Machine(name="gamma", host="10.0.0.12", user="u", password="p"),
        ]
        cfg = ipmitui.Config(machines=ms, defaults={})
        app = _tui.IpmituiApp(cfg, Path("/tmp/never-written.toml"), workers=2, interval=1e6)

        def counts() -> str:
            return str(app.query_one("#counts", Static).render())

        with mock.patch.object(_tui, "probe_power", return_value=ipmitui.Probe("on", 0.01)):
            async with app.run_test() as pilot:
                await pilot.pause()

                # Counts and the filter input both live in the top bar.
                app.query_one("#topbar #counts", Static)
                app.query_one("#topbar Input", Input)

                # Counts show the total; no "showing" subset, even when
                # a filter is active.
                self.assertIn("3 machines", counts())
                self.assertNotIn("showing", counts())

                # Main screen carries the rounded outer frame.
                self.assertEqual(app.screen.styles.border.top[0], "round")

                # Filtering narrows the rows but the count stays the total.
                await pilot.press("f")
                await pilot.press("b", "e", "t", "a")
                await pilot.pause()
                self.assertEqual([m.name for m in app._filtered_machines()], ["beta"])
                self.assertIn("3 machines", counts())
                self.assertNotIn("showing", counts())

                # Enter submits the filter (focus back to table), then
                # Enter opens the picker; the modal drops the outer
                # frame so it does not sit behind a second border.
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, _tui._ActionScreen)
                self.assertEqual(app.screen.styles.border.top[0], "")

    async def test_note_column_shows_description(self):
        _tui = await self._run()
        from textual.widgets import DataTable

        ms = [
            ipmitui.Machine(name="a", host="h", user="u", password="p", description="rack3 top"),
            ipmitui.Machine(name="b", host="h2", user="u", password="p"),
        ]
        cfg = ipmitui.Config(machines=ms, defaults={})
        app = _tui.IpmituiApp(cfg, Path("/tmp/never-written.toml"), workers=2, interval=1e6)
        with mock.patch.object(_tui, "probe_power", return_value=ipmitui.Probe("on", 0.01, "")):
            async with app.run_test() as pilot:
                await pilot.pause()
                table = app.query_one(DataTable)
                self.assertEqual(table.get_row_at(0)[4], "rack3 top")
                self.assertEqual(table.get_row_at(1)[4], "")

    async def test_form_ok_button_saves(self):
        _tui = await self._run()
        from textual.widgets import Button, Input

        ms = [ipmitui.Machine(name="a", host="h", user="u", password="p")]
        cfg = ipmitui.Config(machines=ms, defaults={})
        app = _tui.IpmituiApp(cfg, Path("/tmp/never-written.toml"), workers=1, interval=1e6)
        with (
            mock.patch.object(_tui, "probe_power", return_value=ipmitui.Probe("on", 0.01)),
            mock.patch.object(_tui, "save_config"),
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("a")
                await pilot.pause()
                # The form offers a single Save button (Esc cancels).
                self.assertEqual({b.id for b in app.screen.query(Button)}, {"save"})
                app.screen.query_one("#f-name", Input).value = "newbox"
                app.screen.query_one("#f-host", Input).value = "10.0.0.9"
                app.screen.query_one("#save", Button).press()
                await pilot.pause()
                self.assertIn("newbox", [m.name for m in app._machines])

    async def test_edit_does_not_blank_table(self):
        """Regression: the CRUD form's own Input.Changed events must
        not be treated as filter text (which blanked the table after an
        edit). The filter handler is scoped to ``#filter``."""
        _tui = await self._run()
        from textual.widgets import Button, DataTable, Input

        ms = [
            ipmitui.Machine(name="alpha", host="10.0.0.10", user="u", password="p"),
            ipmitui.Machine(name="beta", host="10.0.0.11", user="u", password="p"),
        ]
        cfg = ipmitui.Config(machines=ms, defaults={})
        app = _tui.IpmituiApp(cfg, Path("/tmp/never-written.toml"), workers=2, interval=1e6)
        with (
            mock.patch.object(_tui, "probe_power", return_value=ipmitui.Probe("on", 0.01)),
            mock.patch.object(_tui, "save_config"),
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("e")
                await pilot.pause()
                self.assertIsInstance(app.screen, _tui._MachineFormScreen)
                # Typing in a form field must not leak into the filter.
                app.screen.query_one("#f-desc", Input).value = "rack3 top"
                await pilot.pause()
                self.assertEqual(app._filter_text, "")
                app.screen.query_one("#save", Button).press()
                await pilot.pause()
                # Table still shows every machine after the edit.
                self.assertEqual(app.query_one(DataTable).row_count, 2)

    async def test_help_popup_opens_via_h_and_space(self):
        _tui = await self._run()

        ms = [ipmitui.Machine(name="a", host="h", user="u", password="p")]
        cfg = ipmitui.Config(machines=ms, defaults={})
        app = _tui.IpmituiApp(cfg, Path("/tmp/never-written.toml"), workers=1, interval=1e6)
        with mock.patch.object(_tui, "probe_power", return_value=ipmitui.Probe("on", 0.01)):
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("h")
                await pilot.pause()
                self.assertIsInstance(app.screen, _tui._HelpScreen)
                await pilot.press("escape")
                await pilot.pause()
                # Space is the quiet alias.
                await pilot.press("space")
                await pilot.pause()
                self.assertIsInstance(app.screen, _tui._HelpScreen)

    async def test_glyph_toggle(self):
        _tui = await self._run()
        from textual.widgets import DataTable, Static

        ms = [ipmitui.Machine(name="a", host="h", user="u", password="p")]
        cfg = ipmitui.Config(machines=ms, defaults={}, glyphs=True)
        app = _tui.IpmituiApp(cfg, Path("/tmp/never-written.toml"), workers=1, interval=1e6)

        def title() -> str:
            return str(app.query_one("#app-title", Static).render())

        def first_col() -> str:
            return str(next(iter(app.query_one(DataTable).columns.values())).label)

        with mock.patch.object(_tui, "probe_power", return_value=ipmitui.Probe("on", 0.01)):
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertTrue(app._glyphs)
                self.assertNotEqual(title(), "ipmitui")  # glyph prefix present
                self.assertNotEqual(first_col(), "name")
                await pilot.press("g")
                await pilot.pause()
                self.assertFalse(app._glyphs)
                self.assertEqual(title(), "ipmitui")  # plain, no glyph
                self.assertEqual(first_col(), "name")
                await pilot.press("g")
                await pilot.pause()
                self.assertTrue(app._glyphs)


if __name__ == "__main__":
    unittest.main()
