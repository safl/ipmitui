# ipmitui

[![CI](https://github.com/safl/ipmitui/actions/workflows/ci-cd.yml/badge.svg?branch=main)](https://github.com/safl/ipmitui/actions/workflows/ci-cd.yml)
[![license](https://img.shields.io/badge/license-BSD--3--Clause-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![release](https://img.shields.io/badge/release-v1.0.0-blue.svg)](https://github.com/safl/ipmitui/releases)

Small operator-side CLI + TUI for driving a rack of IPMI BMCs. One TOML
config of machines, one shell-out per call to `ipmitool`, parallel scans for
the live power-state table. Power, Serial-over-LAN, sanity checks, and an
interactive table with add / edit / delete.

Sibling to [`bty`](https://github.com/safl/bty): `bty` owns image catalogs,
PXE plans and deploys; `ipmitui` owns power state and console access. They
stay separate so each stays small and operator-readable.

## Install

```sh
pipx install .                          # from a checkout
pipx install git+https://github.com/safl/ipmitui
```

Requires `ipmitool` on `$PATH` (`apt install ipmitool` on Debian / Ubuntu).

## Config

Default path: `~/.config/ipmitui.toml` (override with `--config` or
`$IPMITUI_CONFIG`). If it does not exist yet it is created on first run (the
TUI says where) rather than erroring out, so you can start empty and add
machines with `a`. The legacy `~/.config/ipmitui/machines.toml` is still
read when it is the file that exists. The file is written `0600` because
passwords may be plaintext.

```toml
[defaults]
user = "ADMIN"
pass_cmd = "pass show lab/{name}-bmc"   # {name} is the machine's name

[[machine]]
name = "lab-01"
host = "10.20.30.10"
description = "headend BMC, warp rack"

[[machine]]
name = "lab-02"
host = "10.20.30.11"
user = "OTHER"               # overrides the default user
password = "inline-secret"   # plaintext; for lab-only / quick start

[ui]
glyphs = true                # Nerd Font icons in the TUI (toggle live with "g")
```

Per machine, give either `password` (inline) or `pass_cmd` (a shell command
whose first stdout line is the password); `pass_cmd` keeps the file
plaintext-free with `pass` / `gopass` / `op` / `sops`. `--config` may be
repeated to merge several files, a simple way to group hosts:

```sh
ipmitui --config warp.toml --config wave.toml      # both racks in one view
```

## Commands

Running `ipmitui` with no subcommand opens the interactive TUI.

```sh
ipmitui                       # interactive TUI (default)
ipmitui tui                   # same, explicit
ipmitui status                # one-shot parallel scan, print table, exit
ipmitui check                 # exit non-zero if any machine is unreachable / auth-fails

ipmitui sol   lab-01          # exec into `ipmitool sol activate`
ipmitui on    lab-01          # power on
ipmitui off   lab-01          # power off (hard)
ipmitui soft  lab-01          # graceful shutdown (ACPI)
ipmitui cycle lab-01          # power cycle
ipmitui reset lab-01          # power reset
ipmitui ipmitool lab-01 mc info   # raw passthrough with the auth prefix filled in
```

Flags: `--config PATH` (repeatable), `--workers N` (parallel scan width,
default 8), `--interval S` (TUI refresh seconds, default 5), `--version`.

## TUI keys

```
f or /     focus the filter (matches name / host / note)
i or Enter open the action picker for the selected machine
           (picker: j/k or arrows to move, Enter to act, Esc/q to cancel)
a / e / d  add / edit / delete a machine
r          refresh now
g          toggle Nerd Font glyphs
h or Space help
q          quit
j / k      move the cursor (alongside the arrow keys)
Esc        clear the filter / close a popup
```

The action picker offers serial console (SoL) and the power ops. Edits are
saved back to the primary config file.

## Development

```sh
make hooks      # install the pre-commit gate (ruff + hygiene)
make check      # ruff lint + format-check + tests
make test       # tests only
```

CI (`.github/workflows/ci-cd.yml`) runs lint + tests on every PR and on
`main`; a green `main` auto-tags `v<version>` and the tag publishes a GitHub
release.
