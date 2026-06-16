"""ipmitui - small operator-side CLI + TUI for driving a rack of IPMI BMCs.

Reads a TOML config of machines, shells out to ``ipmitool`` for power
status + SoL + power operations, runs a Rich-rendered table in either
one-shot (``ipmitui status``) or auto-refreshing (``ipmitui tui``) mode.

Config: ``~/.config/ipmitui/machines.toml`` by default (override with
``--config`` or ``IPMITUI_CONFIG``). Shape:

    [defaults]
    user = "ADMIN"
    pass_cmd = "pass show lab/{name}-bmc"   # {name} substituted

    [[machine]]
    name = "lab-01"
    host = "10.20.30.10"

    [[machine]]
    name = "lab-02"
    host = "10.20.30.11"
    user = "OTHER"
    password = "inline-only-for-lab-secrets"

    [ui]
    glyphs = true            # Nerd Font icons in the TUI (toggle with "g")

``defaults`` are applied to every entry that does not override them.
``pass_cmd`` is shelled out and its first stdout line is the password
so credentials can stay in ``pass`` / ``gopass`` / ``op`` / ``sops``
instead of a plaintext TOML. ``--config`` may be repeated to merge
several files (a simple way to group hosts for the status / tui views).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

__version__ = "1.0.0"

DEFAULT_CONFIG = Path(
    os.environ.get("IPMITUI_CONFIG") or "~/.config/ipmitui/machines.toml"
).expanduser()
DEFAULT_INTERVAL = 5.0
DEFAULT_WORKERS = 8
IPMITOOL_TIMEOUT = 5  # per-call wall clock cap, seconds


@dataclass(frozen=True)
class Machine:
    """One BMC. ``password`` and ``pass_cmd`` are mutually exclusive;
    ``pass_cmd`` runs once per IPMI invocation so a rotating secret
    can be sourced from ``pass`` / ``op`` / ``sops`` without touching
    the config file.

    ``description`` is a free-text note the operator attaches to a
    machine (e.g. "rack3 top, NVMe shelf", "old DRAC, needs
    weekly cycle"). Surfaces in the action modal so the operator
    is reminded WHY they care about this entry before firing
    something destructive.
    """

    name: str
    host: str
    user: str
    password: str | None = None
    pass_cmd: str | None = None
    description: str | None = None

    def secret(self) -> str:
        if self.password:
            return self.password
        if self.pass_cmd:
            out = subprocess.run(
                self.pass_cmd, shell=True, capture_output=True, text=True, check=True
            ).stdout
            return out.strip().splitlines()[0]
        raise ValueError(f"machine {self.name!r}: neither ``password`` nor ``pass_cmd`` set")

    def ipmi_args(self) -> list[str]:
        return [
            "ipmitool",
            "-I",
            "lanplus",
            "-H",
            self.host,
            "-U",
            self.user,
            "-P",
            self.secret(),
        ]


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """Parsed ipmitui config. ``defaults`` carries the raw
    ``[defaults]`` block from disk so a CRUD save can round-trip
    it back without losing operator-set values (e.g. a
    ``pass_cmd`` template). ``machines`` carries the resolved,
    ready-to-use :class:`Machine` instances after defaults merge."""

    machines: list[Machine]
    defaults: dict
    # UI preference from the ``[ui]`` block (``glyphs = true|false``);
    # toggled at runtime with "g" in the TUI.
    glyphs: bool = True


def _load_one(path: Path) -> Config:
    if not path.exists():
        raise SystemExit(
            f"ipmitui: config not found: {path}\n"
            f"create it with the shape documented in ``ipmitui --help`` "
            "(or ``$IPMITUI_CONFIG`` / ``--config <path>`` to point elsewhere)."
        )
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    defaults: dict = data.get("defaults", {})
    glyphs = bool(data.get("ui", {}).get("glyphs", True))
    out: list[Machine] = []
    for raw in data.get("machine", []):
        if "name" not in raw:
            raise SystemExit(f"ipmitui: machine entry missing ``name``: {raw!r}")
        merged = {**defaults, **raw}
        # ``pass_cmd``'s ``{name}`` placeholder lets one default expand
        # into per-host calls (e.g. ``pass show lab/{name}-bmc``).
        if "pass_cmd" in merged:
            merged["pass_cmd"] = merged["pass_cmd"].format(name=merged["name"])
        out.append(
            Machine(
                name=merged["name"],
                host=merged["host"],
                user=merged.get("user", "ADMIN"),
                password=merged.get("password"),
                pass_cmd=merged.get("pass_cmd"),
                description=merged.get("description"),
            )
        )
    return Config(machines=out, defaults=defaults, glyphs=glyphs)


def load_config(paths: Path | list[Path] = DEFAULT_CONFIG) -> Config:
    """Load one or several TOML configs. Passing more than one path
    merges their machines (de-duplicated by ``name``; the earlier file
    wins on a clash) so ``--config a --config b`` is a simple way to
    group hosts in the ``status`` / ``tui`` views. ``[defaults]`` are
    merged (later files override) and the ``[ui].glyphs`` preference is
    taken from the first (primary) config."""
    path_list = [paths] if isinstance(paths, Path) else list(paths)
    machines: list[Machine] = []
    seen: set[str] = set()
    defaults: dict = {}
    glyphs = True
    for idx, p in enumerate(path_list):
        cfg = _load_one(p)
        defaults.update(cfg.defaults)
        if idx == 0:
            glyphs = cfg.glyphs
        for m in cfg.machines:
            if m.name in seen:
                continue
            seen.add(m.name)
            machines.append(m)
    return Config(machines=machines, defaults=defaults, glyphs=glyphs)


def save_config(
    path: Path,
    machines: list[Machine],
    defaults: dict | None = None,
    glyphs: bool | None = None,
) -> None:
    """Write a TOML config back to ``path``. Used by the in-TUI CRUD
    flow; pass the same ``defaults`` dict you got from
    :func:`load_config` to preserve the operator's ``[defaults]``
    block. Each :class:`Machine` is written with explicit per-row
    fields (the defaults block stays intact but is not relied upon
    at re-load time), so a future external edit can re-introduce a
    defaults pattern without conflict.

    Creates the parent directory if missing and chmods the file to
    600 because the password may be plaintext.
    """
    import tomli_w

    body: dict = {}
    if defaults:
        body["defaults"] = defaults
    if glyphs is not None:
        body["ui"] = {"glyphs": glyphs}
    body["machine"] = []
    for m in machines:
        row: dict = {"name": m.name, "host": m.host, "user": m.user}
        if m.password:
            row["password"] = m.password
        if m.pass_cmd:
            row["pass_cmd"] = m.pass_cmd
        if m.description:
            row["description"] = m.description
        body["machine"].append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(body), encoding="utf-8")
    path.chmod(0o600)


def find_machine(machines: list[Machine], name: str) -> Machine:
    for m in machines:
        if m.name == name:
            return m
    known = ", ".join(m.name for m in machines) or "(none)"
    raise SystemExit(f"ipmitui: no machine named {name!r}; known: {known}")


# ---------------------------------------------------------------------------
# IPMI shell-out
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Probe:
    """One ``ipmitool chassis power status`` result.

    ``state`` is one of:
      * ``on`` / ``off``  - chassis power reported normally.
      * ``unreachable``   - network / RMCP timeout.
      * ``auth-fail``     - RAKP / privilege rejection (creds wrong).
      * ``error``         - any other ipmitool failure; ``note`` carries
        the salient stderr line.
    ``elapsed`` is the call wall clock in seconds.
    """

    state: str
    elapsed: float
    note: str = ""


_UNREACHABLE_HINTS = ("No route to host", "Connection timed out", "Unable to establish")
_AUTH_HINTS = (
    "Authentication type",
    "RAKP",
    "Get Session",
    "Privilege Level",
    "invalid user name",
    "Unable to obtain",
)


def _classify(stderr: str) -> tuple[str, str]:
    """Map ipmitool's stderr to ``(state, note)``."""
    blob = stderr.strip()
    head = blob.splitlines()[-1] if blob else ""
    if any(h.lower() in blob.lower() for h in _UNREACHABLE_HINTS):
        return "unreachable", head
    if any(h.lower() in blob.lower() for h in _AUTH_HINTS):
        return "auth-fail", head
    return "error", head


def probe_power(m: Machine) -> Probe:
    """Run ``ipmitool chassis power status`` once and classify the
    result. Never raises; on any subprocess error returns a Probe with
    ``state != "on"|"off"`` so the caller can render the failure
    bucket without try/except plumbing per row."""
    started = time.monotonic()
    try:
        result = subprocess.run(
            m.ipmi_args() + ["chassis", "power", "status"],
            capture_output=True,
            text=True,
            timeout=IPMITOOL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return Probe("unreachable", time.monotonic() - started, "timeout")
    except FileNotFoundError:
        raise SystemExit("ipmitui: ``ipmitool`` not on PATH; install it (apt: ipmitool)") from None
    except ValueError as exc:
        return Probe("error", time.monotonic() - started, str(exc))
    elapsed = time.monotonic() - started
    if result.returncode == 0:
        out = result.stdout.strip().lower()
        if "is on" in out:
            return Probe("on", elapsed)
        if "is off" in out:
            return Probe("off", elapsed)
        return Probe("error", elapsed, result.stdout.strip().splitlines()[-1] if out else "")
    state, note = _classify(result.stderr)
    return Probe(state, elapsed, note)


def power_op(m: Machine, op: str) -> int:
    """Run ``ipmitool chassis power <op>`` and return the exit code."""
    result = subprocess.run(
        m.ipmi_args() + ["chassis", "power", op],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
    sys.stdout.write(result.stdout)
    return result.returncode


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


_STATE_STYLE = {
    "on": "bold green",
    "off": "dim",
    "unreachable": "yellow",
    "auth-fail": "bold red",
    "error": "red",
}


def _render_table(rows: list[tuple[Machine, Probe]], title: str | None = None) -> Table:
    table = Table(title=title, expand=True, show_lines=False)
    table.add_column("name", style="bold", no_wrap=True)
    table.add_column("host", no_wrap=True)
    table.add_column("power")
    table.add_column("ms", justify="right", style="dim")
    table.add_column("note", style="dim")
    for m, p in rows:
        style = _STATE_STYLE.get(p.state, "red")
        table.add_row(
            m.name,
            m.host,
            f"[{style}]{p.state}[/{style}]",
            f"{p.elapsed * 1000:.0f}",
            p.note,
        )
    return table


def _scan(machines: list[Machine], workers: int) -> list[tuple[Machine, Probe]]:
    if not machines:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(machines))) as pool:
        probes = list(pool.map(probe_power, machines))
    return list(zip(machines, probes, strict=True))


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_status(machines: list[Machine], workers: int, console: Console) -> int:
    rows = _scan(machines, workers)
    console.print(_render_table(rows))
    return 0


def cmd_tui(
    config: Config,
    config_path: Path,
    workers: int,
    interval: float,
    console: Console,
) -> int:
    """Interactive operator surface: filter, select, drop into SoL,
    fire a power op, CRUD-edit the machines list without leaving the
    keyboard. Threads ``config_path`` down so save-on-edit can
    round-trip to the right file. Imports textual lazily so
    ``ipmitui status`` / ``ipmitui check`` keep their rich-only dep
    footprint."""
    del console  # textual owns the terminal end-to-end
    from ipmitui._tui import run_tui

    return run_tui(config, config_path, workers=workers, interval=interval)


def cmd_check(machines: list[Machine], workers: int, console: Console) -> int:
    """Validate that every machine in the config is reachable and
    accepts the configured credentials. Exit non-zero if any machine
    fails so this is usable from a shell ``&&`` chain after editing
    the config."""
    rows = _scan(machines, workers)
    console.print(_render_table(rows, title="ipmitui check"))
    bad = [m.name for m, p in rows if p.state not in ("on", "off")]
    if bad:
        console.print(f"[red]{len(bad)} machine(s) failed: {', '.join(bad)}[/red]")
        return 1
    console.print(f"[green]all {len(rows)} machines OK[/green]")
    return 0


def cmd_sol(machine: Machine) -> int:
    """``exec`` into ``ipmitool sol activate`` so the SoL escape
    sequence (``~.``) returns the operator to their shell cleanly,
    rather than back into a Python wrapper."""
    args = machine.ipmi_args() + ["sol", "activate"]
    os.execvp(args[0], args)


def cmd_power(machine: Machine, op: str) -> int:
    rc = power_op(machine, op)
    if rc == 0:
        print(f"{machine.name}: power {op} OK")
    else:
        print(f"{machine.name}: power {op} failed (exit {rc})", file=sys.stderr)
    return rc


def cmd_ipmitool(machine: Machine, extra_args: list[str]) -> int:
    """Pass ``extra_args`` verbatim to ipmitool with the machine's
    -I lanplus -H -U -P prefix supplied automatically. Lets the
    operator run diagnostics like ``ipmitui ipmitool warp -- sol
    info`` or ``mc info`` / ``sdr list`` / ``raw 0x06 0x01``
    without hand-extracting the password from the config file.
    Output streams to the operator's terminal; exit code mirrors
    ipmitool's."""
    if not extra_args:
        print("ipmitui: ipmitool subcommand needs args (e.g. ``sol info``)", file=sys.stderr)
        return 2
    args = machine.ipmi_args() + extra_args
    os.execvp(args[0], args)


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ipmitui",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        type=Path,
        action="append",
        default=None,
        help=(
            "TOML config; repeat to merge several files and group hosts "
            f"(default: {DEFAULT_CONFIG})"
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"parallel ipmitool calls (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"ipmitui v{__version__}",
    )
    # ``--interval`` is parsed at the top level so the default ``tui``
    # action (no subcommand) honours it the same way as ``ipmitui tui``.
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help=f"TUI refresh seconds (default: {DEFAULT_INTERVAL:g})",
    )
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")
    sub.add_parser("tui", help="interactive auto-refreshing power-state table (default)")
    sub.add_parser("status", help="one-shot parallel power-state table; for scripting")
    sub.add_parser("check", help="validate auth across the fleet; exit non-zero on any failure")
    p_sol = sub.add_parser("sol", help="exec ipmitool sol activate for one machine")
    p_sol.add_argument("name")
    for op in ("on", "off", "cycle", "reset", "soft"):
        sp = sub.add_parser(op, help=f"ipmitool chassis power {op}")
        sp.add_argument("name")
    p_raw = sub.add_parser(
        "ipmitool",
        help="run ipmitool with the machine's auth prefix (e.g. sol info / mc info)",
    )
    p_raw.add_argument("name")
    p_raw.add_argument("args", nargs=argparse.REMAINDER, help="forwarded to ipmitool verbatim")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    configs = args.config or [DEFAULT_CONFIG]
    config = load_config(configs)
    machines = config.machines
    console = Console()

    cmd = args.cmd or "tui"
    if cmd == "tui":
        # tui needs the full Config so save_config can preserve the
        # operator's ``[defaults]`` / ``[ui]`` blocks. CRUD writes back
        # to the primary (first) config; with several --config files
        # this merges edits there, so grouping is best kept read-only.
        return cmd_tui(config, configs[0], args.workers, args.interval, console)
    if cmd == "status":
        return cmd_status(machines, args.workers, console)
    if cmd == "check":
        return cmd_check(machines, args.workers, console)
    if cmd == "sol":
        return cmd_sol(find_machine(machines, args.name))
    if cmd in ("on", "off", "cycle", "reset", "soft"):
        return cmd_power(find_machine(machines, args.name), cmd)
    if cmd == "ipmitool":
        # ``argparse.REMAINDER`` keeps a leading ``--`` if the
        # operator typed one; strip it so it does not reach ipmitool.
        extra = list(args.args)
        if extra and extra[0] == "--":
            extra = extra[1:]
        return cmd_ipmitool(find_machine(machines, args.name), extra)
    parser.print_help()
    return 1


__all__ = ["__version__", "Config", "Machine", "Probe", "main", "save_config"]
