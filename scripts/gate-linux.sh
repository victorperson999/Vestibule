#!/bin/bash
# Vestibule's Linux gate (M2 step 1, S1-D2/S1-D3). Run inside WSL Ubuntu (or any Linux):
#
#   bash scripts/gate-linux.sh --setup   # (re)build both venvs from scratch, then run the gate
#   bash scripts/gate-linux.sh           # run the gate
#
# From Windows Git Bash:  MSYS_NO_PATHCONV=1 wsl.exe -d Ubuntu -- bash /mnt/c/<repo>/scripts/gate-linux.sh
# From PowerShell:        wsl -d Ubuntu -- bash /mnt/c/<repo>/scripts/gate-linux.sh
#
# Time: the whole gate (not --setup) must finish within GATE_BUDGET_S (540 s), below the gate
# agent's 10-minute tool limit; each pytest run gets the time that is left.
#
# Venvs live outside the repo: ~/.venvs/vestibule (the system /usr/bin/python3) and
# ~/.venvs/vestibule311 (a uv-installed CPython 3.11; uv sits in ~/.venvs/tools). They are
# bound to ONE checkout (editable install); check_env fails if this checkout isn't it.
# No cache is written into the repo: ruff runs with --no-cache, mypy and bytecode caches go
# to ~/.cache, and pytest's cache plugin is off. Every pytest run is time-bounded.
#
# Skips: any skipped test fails the gate (a missing Docker daemon or image must never turn
# into a quiet green), except reasons listed in ALLOWED_SKIPS, which depend on the host
# rather than on a broken setup.
set -u
REPO=$(cd "$(dirname "$0")/.." && pwd)
VSYS=~/.venvs/vestibule
V311=~/.venvs/vestibule311
TOOLS=~/.venvs/tools
SYSPY=/usr/bin/python3
CONSTRAINTS="$REPO/requirements/constraints.txt"
export PYTHONPYCACHEPREFIX=~/.cache/vestibule-pycache
MYPY_CACHE=~/.cache/vestibule-mypy
# One budget for the whole gate, below the gate agent's 10-minute tool limit, so this
# script's own bound fires first and reports FAIL. Each pytest run gets what is left.
GATE_BUDGET_S=540
# Host-dependent skips that do not mean a broken setup (extended deliberately, with a reason).
ALLOWED_SKIPS='podman is installed here'
FAILED=()
# The running pytest's process group (`timeout` leads its own), killed if this script is
# signalled, so an interrupted gate doesn't leave a pytest driving Docker in the background.
CHILD_PGID=
trap 'if [ -n "$CHILD_PGID" ]; then kill -TERM -- "-$CHILD_PGID" "$CHILD_PGID" 2>/dev/null; fi' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

pyver() {  # pyver <python>: "X.Y"
  "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])'
}

build_venv() {  # build_venv <venv dir> <base python>
  local v=$1 py=$2
  "$py" -m venv --clear "$v" || return 1
  "$v/bin/pip" install -q -e "$REPO[dev]" -c "$CONSTRAINTS" || return 1
  echo "   $v: $("$v/bin/python" --version)"
}

setup() {
  echo "== setup: rebuilding venvs from $CONSTRAINTS"
  # An activated venv must not decide which interpreters get used.
  unset VIRTUAL_ENV
  if [ ! -x "$TOOLS/bin/uv" ]; then
    "$SYSPY" -m venv --clear "$TOOLS" && "$TOOLS/bin/pip" install -q uv || return 1
  fi
  "$TOOLS/bin/uv" python install 3.11 >/dev/null || return 1  # errors stay visible
  local py311
  py311=$("$TOOLS/bin/uv" python find --managed-python 3.11) || return 1
  build_venv "$VSYS" "$SYSPY" || return 1
  build_venv "$V311" "$py311" || return 1
}

step() {  # step <label> <command...>: run, report PASS/FAIL, remember failures
  local label=$1
  shift
  echo "== $label"
  if "$@"; then
    echo "-- $label: PASS"
  else
    echo "-- $label: FAIL"
    FAILED+=("$label")
  fi
}

in_repo() {  # in_repo <command...>: run from the repo root
  (cd "$REPO" && "$@")
}

pytest_checked() {  # pytest_checked <venv> <pytest args...>: bounded run; fail on any unallowed skip
  local v=$1
  shift
  local out rc left total allowed
  left=$(( GATE_DEADLINE - $(date +%s) ))
  if [ "$left" -le 20 ]; then
    echo "!! the gate's ${GATE_BUDGET_S} s budget is used up; pytest not started"
    return 1
  fi
  out=$(mktemp)
  # Plain output whatever the environment says (colour codes or PYTEST_ADDOPTS=--no-summary
  # would hide skips from the checks below). Run in the background so its process group is
  # known to the EXIT trap; the output is printed once the run ends.
  (cd "$REPO" && exec env -u PYTEST_ADDOPTS -u FORCE_COLOR -u PY_COLORS \
     timeout -k 20 "$left" "$v/bin/python" -m pytest -q -p no:cacheprovider -rs --color=no "$@") \
     > "$out" 2>&1 &
  CHILD_PGID=$!
  wait "$CHILD_PGID"
  rc=$?
  CHILD_PGID=
  cat "$out"
  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    echo "!! pytest was stopped at the gate's time budget (a hang? see the last test above)"
  fi
  # Skips: the summary line's total must equal the allowlisted ones in the -rs lines.
  total=$(grep -oE '[0-9]+ skipped' "$out" | tail -1 | grep -oE '[0-9]+')
  allowed=$(grep -E '^SKIPPED \[[0-9]+\]' "$out" | grep -E "$ALLOWED_SKIPS" \
            | sed -E 's/^SKIPPED \[([0-9]+)\].*/\1/' | awk '{s += $1} END {print s + 0}')
  if [ "${total:-0}" -gt "$allowed" ]; then
    echo "!! ${total} test(s) skipped on Linux (reasons above); the gate allows no skips except: $ALLOWED_SKIPS"
    rc=1
  fi
  rm -f "$out"
  return "$rc"
}

if [ "${1:-}" = "--setup" ]; then
  setup || { echo "== setup: FAIL"; exit 1; }
fi
for v in "$VSYS" "$V311"; do
  if [ ! -x "$v/bin/python" ]; then
    echo "missing venv $v: run bash scripts/gate-linux.sh --setup"
    exit 1
  fi
done

SYSVER=$(pyver "$SYSPY")
t0=$(date +%s)
GATE_DEADLINE=$(( t0 + GATE_BUDGET_S ))
step "check_env ($SYSVER)" "$VSYS/bin/python" "$REPO/scripts/check_env.py" --expect-python "$SYSVER"
step "check_env (3.11)" "$V311/bin/python" "$REPO/scripts/check_env.py" --expect-python 3.11
step "ruff" in_repo "$VSYS/bin/ruff" check . --no-cache
step "mypy" in_repo "$VSYS/bin/mypy" src --cache-dir "$MYPY_CACHE"
step "pytest ($SYSVER, full suite)" pytest_checked "$VSYS"
step "pytest (3.11, Docker-free)" pytest_checked "$V311" -m "not docker"
step "pytest (3.11, stdio end-to-end)" pytest_checked "$V311" tests/test_stdio_e2e.py

echo "== Linux gate: $(( $(date +%s) - t0 )) s"
if [ ${#FAILED[@]} -gt 0 ]; then
  printf 'FAILED: %s\n' "${FAILED[@]}"
  exit 1
fi
echo "ALL PASS"
