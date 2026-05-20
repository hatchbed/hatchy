"""Live per-package status overlay for `hatchy build` and `hatchy test`.

Owns the `StatusDisplay` class (which consumes lines of colcon output and
maintains a scrolling overlay of in-flight packages plus a summary line) and
the `_run_with_status` driver loop that pumps a colcon subprocess into the
display.  Two thin public wrappers — `run_build_with_status` and
`run_test_with_status` — configure the display for each command.

Stderr highlighting is delegated to the `highlighters` module.
"""

import locale
import os
import queue
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .common import (
    clr, supports_ansi, _strip_ansi, _truncate_ansi, _fmt_duration,
    _GREEN, _YELLOW, _RED, _BOLD_RED, _BOLD_GREEN,
    _CYAN, _BRIGHT_BLUE, _BRIGHT_MAGENTA, _DIM, _BOLD,
)
from .highlighters import highlight_stderr

# ---- tunables ----------------------------------------------------------------
# Cap on left-padding for completed-package names so very long names don't blow
# out the terminal width.
_MAX_NAME_WIDTH = 40
# Tail of the colcon stdout.log read on each render to extract progress.
_TAIL_READ_BYTES = 32768
# Sleep between overlay renders.  Lower = smoother spinner, higher CPU.
_RENDER_INTERVAL_S = 0.1
# Minimum interval between renice calls on the colcon process tree.
_RENICE_INTERVAL_S = 1.0
# Idle time after a successful Finished <<< before we flush the buffered
# [ ok ] line.  The buffer exists so a trailing CTest stderr block can flip
# [ ok ] to [ FAIL ]; without a timeout, a quiet build would leave the most
# recent completion invisible until the next colcon event.
_PENDING_FLUSH_S = 0.25
# Window after a SIGWINCH during which we render overlay lines with extra
# right-edge slack so further small width reductions don't wrap the just-
# rendered lines.  Sized to outlast a typical drag session.
_RESIZE_DEBOUNCE_S = 0.6
# Total characters of slack to leave on the right edge of overlay lines in
# steady state.  Lines are truncated to ``cols - margin`` visible characters.
_OVERLAY_LINE_MARGIN = 2
# Wider slack while a resize is in progress; absorbs minor width reductions
# between this render and the next.
_RESIZE_LINE_MARGIN = 8

# DEC mode 2026 — synchronized output.  Bracketing a write sequence with
# BSU/ESU tells the terminal to buffer the output and present it atomically,
# so a SIGWINCH-driven reflow can't interleave with our ANSI sequences in
# mid-render.  Terminals that don't recognise mode 2026 ignore both
# sequences harmlessly.
_BSU = '\033[?2026h'  # Begin Synchronized Update
_ESU = '\033[?2026l'  # End Synchronized Update

_SPIN_FRAMES = ('⠴', '⠦', '⠖', '⠲') \
    if 'utf' in locale.getpreferredencoding(False).lower() \
    else ('/', '-', '\\', '|')

# CTest stderr boilerplate lines that indicate failures but carry no actionable
# details.  Their presence is used to flip a pending [ OK ] to [ FAIL ]; the
# lines themselves are suppressed from the displayed stderr block.
_CTEST_BOILERPLATE = (
    'Errors while running CTest',
    'Output from these tests are in:',
    '--rerun-failed --output-on-failure',
)

# Colcon WARNING lines that are harmless noise (e.g. stale prefix paths after a
# clean).  Matched as substrings so the surrounding module path doesn't matter.
_SUPPRESSED_WARNINGS = (
    "in the environment variable CMAKE_PREFIX_PATH doesn't exist",
    "in the environment variable AMENT_PREFIX_PATH doesn't exist",
)

# Colcon prefixes logger output (WARNING/INFO/etc.) with a wall-clock timestamp.
_COLCON_TS_RE = re.compile(r'^\[\d+(?:\.\d+)?s\]\s+')

# Lines that mark a colcon-level boundary outside any per-package output.  Used
# to confirm a deferred stderr-block close (bare `---`) is really a delimiter
# rather than incidental content.
_COLCON_BOUNDARY_RE = re.compile(
    r'^(?:Starting\s+>>>|Finished\s+<<<|Failed\s+<<<|Aborted\s+<<<|Summary:|\[\s*OK\s*\]|WARNING:)'
)


def _is_ctest_boilerplate(line: str) -> bool:
    stripped = line.strip()
    return any(p in stripped for p in _CTEST_BOILERPLATE)


def _is_colcon_boundary(line: str) -> bool:
    return bool(_COLCON_BOUNDARY_RE.match(_COLCON_TS_RE.sub('', line, count=1)))


def _read_tail(path: str, nbytes: int = _TAIL_READ_BYTES) -> str:
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            return f.read().decode('utf-8', errors='replace')
    except OSError:
        return ''


def _truncate_desc(desc: str, max_len: int) -> str:
    """Truncate a build description to max_len visible characters.

    For descriptions with a path component (e.g. 'Building CXX object src/foo.cc.o'),
    keeps the action prefix and truncates the beginning of the path so the filename
    is always visible: 'Building CXX object .../foo.cc.o'.
    Falls back to truncating from the start for non-path descriptions.
    """
    if len(desc) <= max_len or max_len <= 3:
        return desc

    words = desc.split(' ')
    for i, word in enumerate(words):
        if i > 0 and '/' in word:
            action = ' '.join(words[:i])
            path = desc[len(action) + 1:]
            remaining = max_len - len(action) - 4  # 4 for ' ...'
            if remaining > 4:
                return f"{action} ...{path[-remaining:]}"
            break

    return '...' + desc[-(max_len - 3):]


# CTest stdout.log parser patterns.  Compiled at module load so the hot path
# in _parse_test_progress doesn't recompile per-line.  The per-test patterns
# capture the leading test index as a group rather than hard-coding it, so a
# single compiled regex covers any test number.
_CTEST_RESULT_RE = re.compile(r'^(\d+)/(\d+)\s+Test\s+#\d+:')
_CTEST_START_RE = re.compile(r'^\s*Start\s+(\d+):\s+(.+)$')
_GTEST_RUN_RE = re.compile(r'^(\d+):\s*\[\s*RUN\s*\]\s+(.+)$')
_GTEST_END_RE = re.compile(r'^(\d+):\s*\[\s*(?:OK|FAILED)\s*\]')
_PYUNIT_START_RE = re.compile(r'^(\d+):\s+(\w+)\s+\([^)]+\)\s+\.\.\.')
_PYUNIT_END_RE = re.compile(r'^(\d+):\s+(?:ok|FAIL(?:ED)?|ERROR|Ran\s+\d+)')

# ExternalProject step-name tables used by _format_ninja_desc.
_EP_STEPS: Dict[str, str] = {
    'gitclone': 'clone', 'gitupdate': 'update',
    'mkdirs': 'mkdir',   'mkdir': 'mkdir',
    'patch': 'patch',    'configure': 'configure',
    'build': 'build',    'install': 'install',
    'download': 'download', 'test': 'test',
    'update': 'update',  'clone': 'clone',
}
_EP_STEP_PAT = '|'.join(_EP_STEPS)
_EP_SCRIPT_RE = re.compile(r'^(.+?)-(' + _EP_STEP_PAT + r')\.cmake$')
_EP_STAMP_RE = re.compile(r'^(.+?)-(' + _EP_STEP_PAT + r')$')


def _format_make_desc(desc: str) -> str:
    """Simplify a CMake/Make progress description to a compact label."""
    # Building (C|CXX) object <path>/<file.ext>.o  →  Compiling <file.ext>
    m = re.match(r'^Building (?:C|CXX) object .*/([^/]+\.[a-zA-Z]+)\.o$', desc)
    if m:
        return f"Compiling {m.group(1)}"
    # Linking (C|CXX) (executable|shared library|...) <path>  →  Linking <basename>
    m = re.match(r'^Linking (?:C|CXX) (?:executable|shared library|static library|shared module) (.+)$', desc)
    if m:
        return f"Linking {os.path.basename(m.group(1))}"
    # Performing <step> step [(<detail>)] for '<name>'  →  <name>: <step>
    m = re.match(r"^Performing (\S+) step.*? for '(.+)'$", desc)
    if m:
        step = m.group(1)
        if step == 'download':
            step = 'clone'
        return f"{m.group(2)}: {step}"
    return desc


def _format_ninja_desc(desc: str) -> str:
    """Simplify a verbose ninja step command to a compact human-readable label.

    Handles the compound ``cd dir && cmd1 && cmd2`` forms produced by
    ``ninja -v``, ExternalProject cmake ``-P`` scripts, cmake configure/build
    steps, compiler ``-c`` invocations, and linker ``-o`` invocations.
    """
    # Strip leading/trailing ninja link-step markers (`: && ... && :`)
    desc = re.sub(r'^:\s*[;&]+\s*', '', desc).strip()
    desc = re.sub(r'\s*&&\s*:\s*$', '', desc).strip()

    # Peel off `cd <dir> && `, keeping the dir as context for `--build .`
    cd_dir: Optional[str] = None
    m = re.match(r'^cd\s+(\S+)\s*&&\s*(.*)', desc, re.DOTALL)
    if m:
        cd_dir = m.group(1)
        desc = m.group(2).strip()

    # Walk each `&&`-chained command.  cmake -E stamp/utility calls are skipped
    # but the last stamp path is remembered as a fallback label.
    stamp_fallback: Optional[str] = None
    for cmd in re.split(r'\s*&&\s*', desc):
        cmd = cmd.strip()
        if not cmd or re.match(r'^:$|^true$', cmd):
            continue

        # cmake -E touch <stamp> — skip but capture as fallback
        m = re.match(r'.*cmake\s+-E\s+touch\s+(\S+)', cmd)
        if m:
            sm = _EP_STAMP_RE.match(os.path.basename(m.group(1)))
            if sm and stamp_fallback is None:
                stamp_fallback = f"{sm.group(1)}: {_EP_STEPS.get(sm.group(2), sm.group(2))}"
            continue

        # cmake -E copy/copy_directory/copy_if_different <src> <dest>  →  primary action
        m = re.match(r'.*cmake\s+-E\s+copy(?:_directory|_if_different)?\s+\S+\s+(\S+)', cmd)
        if m:
            return f"cmake copy: {os.path.basename(m.group(1))}"

        # Other cmake -E utilities (rm, make_directory, echo_append, …) — skip
        if re.search(r'\bcmake\s+-E\b', cmd):
            continue

        # cmake -P <script>  →  ExternalProject named step
        m = re.search(r'\bcmake\s+(?:-\S+\s+)*-P\s+(\S+)', cmd)
        if m:
            sm = _EP_SCRIPT_RE.match(os.path.basename(m.group(1)))
            if sm:
                return f"{sm.group(1)}: {_EP_STEPS.get(sm.group(2), sm.group(2))}"
            return f"cmake script: {os.path.basename(m.group(1))}"

        # cmake --build <dir>  →  "cmake build: <name>"
        m = re.search(r'\bcmake\s+--build\s+(\S+)', cmd)
        if m:
            d = m.group(1)
            if d == '.' and cd_dir:
                d = cd_dir
            name = os.path.basename(d)
            if name.endswith('-build'):
                name = name[:-6]
            return f"cmake build: {name}"

        # cmake configure  (-S <src> -B <build>)  →  "cmake configure: <name>"
        ms = re.search(r'(?:^|\s)-S\s+(\S+)', cmd)
        if ms and re.search(r'(?:^|\s)-B\s+', cmd):
            return f"cmake configure: {os.path.basename(ms.group(1))}"

        # Static archiver: ar qc/rcs/cr/rc <target.a> ...
        m = re.search(r'\bar\s+\w*[qrc]\w*\s+(\S+)', cmd)
        if m:
            return f"Archiving {os.path.basename(m.group(1))}"

        # For tool-based patterns, work from the command's executable basename.
        cmd_parts = cmd.split()
        exe_base = os.path.basename(cmd_parts[0]) if cmd_parts else ''

        # xacro: use -o output if present, otherwise the first .xacro input
        if exe_base == 'xacro':
            m = re.search(r'(?:^|\s)-o\s+(\S+)', cmd)
            if m:
                return f"xacro: {os.path.basename(m.group(1))}"
            m = re.search(r'(\S+\.xacro(?:\.\w+)?)', cmd)
            if m:
                return f"xacro: {os.path.basename(m.group(1))}"
            return "xacro"

        # Python3 interpreter: python3 <script> [args]
        if exe_base in ('python', 'python2', 'python3'):
            script = os.path.basename(cmd_parts[1]) if len(cmd_parts) > 1 else 'python'
            return f"Running {script}"

        # Direct Python script invocation: <script>.py [args]
        if exe_base.endswith('.py'):
            mo = re.search(r'(?:^|\s)(?:-o|--output)\s+(\S+)', cmd)
            if mo:
                return f"Running {exe_base}: {os.path.basename(mo.group(1))}"
            last_file = next(
                (os.path.basename(p) for p in reversed(cmd_parts[1:])
                 if not p.startswith('-') and '.' in p),
                None)
            if last_file:
                return f"Running {exe_base}: {last_file}"
            return f"Running {exe_base}"

        # Compile step: -c <source>
        m = re.search(r'(?:^|\s)-c\s+(\S+)', cmd)
        if m:
            return f"Compiling {os.path.basename(m.group(1))}"

        # Link step: -o <target>
        m = re.search(r'(?:^|\s)-o\s+(\S+)', cmd)
        if m:
            return f"Linking {os.path.basename(m.group(1))}"

    return stamp_fallback or desc


def _parse_test_progress(log_path: Optional[str]) -> Optional[Tuple[int, str]]:
    """Return (percent, description) from a CTest stdout.log.

    CTest writes verbose output with each line prefixed by the test index
    (e.g. '1: [ RUN      ] Suite.TestCase') so we can extract the active
    gtest case from the same file without reading any secondary log.
    """
    if not log_path:
        return None
    tail = _strip_ansi(_read_tail(log_path))
    completed = 0
    total = None
    current_test: Optional[str] = None
    current_test_num: Optional[int] = None
    current_case: Optional[str] = None

    for line in tail.splitlines():
        stripped = line.strip()

        # Result: "3/10 Test #3: test_name .......... Passed  0.01 sec"
        m = _CTEST_RESULT_RE.match(stripped)
        if m:
            completed = int(m.group(1))
            total = int(m.group(2))
            current_test = None
            current_test_num = None
            current_case = None
            continue

        # Start: "    Start 4: test_name"  (leading whitespace in CTest output)
        m = _CTEST_START_RE.match(line)
        if m:
            current_test_num = int(m.group(1))
            current_test = m.group(2).strip()
            current_case = None
            continue

        # Per-test verbose output: "4: [ RUN      ] Suite.TestCase"  (gtest)
        # or "4: test_name (module.Class.test_name) ..."  (Python unittest /
        # launch_test).  Only honor patterns whose leading index matches the
        # currently-running test number — other indices are output from
        # already-completed tests still being flushed by ctest.
        if current_test_num is not None:
            m = _GTEST_RUN_RE.match(stripped)
            if m and int(m.group(1)) == current_test_num:
                current_case = m.group(2).strip()
                continue
            m = _GTEST_END_RE.match(stripped)
            if m and int(m.group(1)) == current_test_num:
                current_case = None
                continue
            m = _PYUNIT_START_RE.match(stripped)
            if m and int(m.group(1)) == current_test_num:
                current_case = m.group(2).strip()
                continue
            m = _PYUNIT_END_RE.match(stripped)
            if m and int(m.group(1)) == current_test_num:
                current_case = None

    if total is None and current_test is None:
        return None
    pct = int(100 * completed / total) if total else 0
    if current_test is not None:
        desc = f"{current_test}: {current_case}" if current_case else current_test
    else:
        desc = f"{completed}/{total}" if total else "starting..."
    return pct, desc


def _extract_build_errors(log_path: str) -> List[str]:
    """Return error-relevant lines from a build stdout.log.

    Filters out the verbose compiler invocation lines (long lines full of -D/-I
    flags) while keeping diagnostics, context lines, and terminal error markers.
    Returns an empty list when no error signal is found.
    """
    try:
        tail = _strip_ansi(_read_tail(log_path))
    except OSError:
        return []
    result = []
    for line in tail.splitlines():
        # Drop ninja/make progress step lines — not useful in error context.
        if re.match(r'^\s*\[\d+[/%]', line):
            continue
        # Drop long compiler invocation lines (compiler path + -D/-I flags).
        if len(line) > 200 and re.search(r'\s+-[DI]\S', line):
            continue
        result.append(line)
    # Only return content if there is at least one error/failure indicator.
    _ERROR_RE = re.compile(
        r':\s*(?:error|fatal error):|ninja: build stopped|make.*\*\*\*|^FAILED:', re.IGNORECASE)
    if any(_ERROR_RE.search(ln) for ln in result):
        return result
    return []


def _parse_progress(log_path: Optional[str]) -> Optional[Tuple[int, str]]:
    """Return (percent, description) from the most recent build progress line."""
    if not log_path:
        return None
    tail = _strip_ansi(_read_tail(log_path))
    for line in reversed(tail.splitlines()):
        line = line.strip()
        # cmake/make: [67%] Building CXX object src/foo.cc.o
        m = re.match(r'^\[\s*(\d+)%\]\s+(.*)', line)
        if m:
            return int(m.group(1)), _format_make_desc(m.group(2).strip())
        # ninja: [67/100] ...
        m = re.match(r'^\[(\d+)/(\d+)\]\s+(.*)', line)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            pct = int(100 * a / b) if b else 0
            return pct, _format_ninja_desc(m.group(3).strip())
    return None


def _infer_phase(last_progress: Optional[Tuple[int, str]]) -> str:
    if last_progress is None:
        return 'cmake'
    dl = last_progress[1].lower()
    if 'install' in dl:
        return 'install'
    if 'link' in dl:
        return 'link'
    return 'build'


@dataclass
class _PkgState:
    name: str
    start: float
    log_path: str
    end: Optional[float] = None
    ok: Optional[bool] = None
    aborted: bool = False
    stderr: List[str] = field(default_factory=list)
    has_stderr: bool = False
    last_progress: Optional[Tuple[int, str]] = None


class StatusDisplay:
    """
    Live per-package status overlay used by both build and test commands.

    Completed packages are printed once into scroll history. In-progress
    packages are shown in a redrawn overlay: one line per package with
    cmake/ninja progress from the colcon log files, plus a compact summary
    line at the bottom.
    """

    def __init__(self, workspace: str, total: Optional[int] = None,
                 log_subdir: str = 'latest_build',
                 progress_fn=None,
                 show_build_summary: bool = True,
                 pkg_names: Optional[List[str]] = None,
                 phase: Optional[str] = None):
        self._log_base = os.path.join(workspace, 'log', log_subdir)
        self._progress_fn = progress_fn or _parse_progress
        self._show_build_summary = show_build_summary
        self._build_start = time.monotonic()
        self._building: Dict[str, _PkgState] = {}
        self._done: List[_PkgState] = []
        self._stderr_pkg: Optional[str] = None
        self._in_stderr = False
        self._in_summary = False
        self._pending_stderr_close = False
        self._live_lines = 0
        self._live_strs: List[str] = []
        self._live_cols: int = 0
        # Cached terminal size from the most recent render().
        self._cached_size: Optional[os.terminal_size] = None
        self._winch_time: float = 0.0
        self._prev_winch = None
        # Self-pipe whose read end the main loop selects on instead of using
        # time.sleep().  The SIGWINCH handler writes to it only when we're
        # transitioning from steady state into the resize window, so the loop
        # wakes up immediately for the first redraw with the wider margin and
        # then resumes the normal _RENDER_INTERVAL_S cadence.
        self._wakeup_pipe: Optional[Tuple[int, int]] = None
        self._tty = supports_ansi()
        if self._tty:
            try:
                r, w = os.pipe()
                os.set_blocking(r, False)
                os.set_blocking(w, False)
                self._wakeup_pipe = (r, w)
            except OSError:
                self._wakeup_pipe = None
            try:
                self._prev_winch = signal.signal(
                    signal.SIGWINCH, self._on_winch)
            except (OSError, ValueError):
                pass
        self._total = total
        self._fixed_phase = phase
        self._status_offset = 0
        self._interrupted = False
        self._pending_state: Optional[_PkgState] = None
        self._pending_state_time: float = 0.0
        # Set when we enter a resize burst, cleared after the post-settle
        # CPR-based reposition runs.  Lets us re-anchor the overlay to the
        # bottom of the terminal once a drag has finished, without paying
        # CPR overhead on every render.
        self._needs_settle_reposition = False
        # Scroll-history lines that arrived during a resize burst.  Writing
        # to absolute rows while the terminal is reflowing risks landing
        # at the wrong physical row, and settle_reposition's drift-erase
        # may wipe whatever we wrote.  _scroll_print stashes lines here
        # while a SIGWINCH is recent; settle_reposition drains the queue
        # after it has repositioned the overlay and refreshed _gap_rows.
        self._pending_scroll_lines: List[str] = []
        # Sorted-ascending list of 0-indexed terminal rows that are currently
        # blank between scroll history and the overlay (left there by past
        # settle repositions).  New scroll-history lines via _scroll_print
        # pop the topmost row off this list and write there (no scroll) until
        # the list is empty.  Tracked as explicit rows (rather than a count)
        # so we still know where the blanks are after N changes (a package
        # finished) or a second resize adds more blanks at different rows.
        self._gap_rows: List[int] = []
        self._ctest_error_pkgs: set = set()
        self._spin_idx: int = 0
        self._name_width = (
            min(max((len(n) for n in pkg_names), default=0), _MAX_NAME_WIDTH)
            if pkg_names else 0
        )

    def _on_winch(self, _signum, _frame) -> None:
        """SIGWINCH handler.  Records the time and, when this SIGWINCH is the
        entry into a new resize burst (we were outside the debounce window),
        nudges the wakeup pipe so the main loop renders immediately with the
        wider resize margin rather than waiting for the next sleep tick.
        Subsequent SIGWINCHes during the same burst don't wake the loop —
        the rendered lines are already short, so the normal cadence is fine.
        """
        now = time.monotonic()
        entering_resize = (now - self._winch_time) >= _RESIZE_DEBOUNCE_S
        self._winch_time = now
        if entering_resize:
            # Each fresh burst arms one post-settle reposition.  We do NOT
            # reset _gap_rows here — any still-unfilled blanks from previous
            # bursts remain on screen and should still be filled by future
            # scroll-history lines.  settle_reposition() will add any newly
            # created blank rows to the existing list.
            self._needs_settle_reposition = True
            if self._wakeup_pipe is not None:
                try:
                    os.write(self._wakeup_pipe[1], b'\x00')
                except (BlockingIOError, OSError):
                    pass

    @property
    def wakeup_fd(self) -> int:
        """File descriptor signalled when the main loop should wake early."""
        return self._wakeup_pipe[0] if self._wakeup_pipe else -1

    def _in_resize_burst(self) -> bool:
        """True while we're inside the post-SIGWINCH debounce window."""
        return (time.monotonic() - self._winch_time) < _RESIZE_DEBOUNCE_S

    def needs_settle_reposition(self) -> bool:
        """True iff a resize burst armed a reposition and has now settled
        (no SIGWINCH for at least _RESIZE_DEBOUNCE_S).  The main loop should
        query CPR and call ``settle_reposition`` when this is True."""
        return (self._needs_settle_reposition
                and self._tty
                and self._live_lines > 0
                and not self._in_resize_burst())

    def settle_reposition(self, cursor_row: Optional[int]) -> None:
        """Post-resize reposition.  If the overlay drifted up from the
        bottom row, erase the drifted rows in place and rewrite the overlay
        at the natural bottom position (``rows-N-1..rows-2``).  This leaves
        ``drift`` blank rows between scroll history and the overlay; we
        record those row indices in ``_gap_rows`` so the next scroll-history
        prints can fill those blanks from the top down instead of triggering
        scrolls that would just shift the blanks around.

        Always clears the pending flag so a stuck CPR query (returns None)
        doesn't keep retrying.
        """
        self._needs_settle_reposition = False
        if cursor_row is None:
            return
        if not self._tty or self._live_lines == 0 or self._cached_size is None:
            return
        rows = self._cached_size.lines
        n = self._live_lines
        # The cursor sits on the overlay's last logical row (render writes
        # n-1 newlines, no trailing newline on the last line), so the overlay
        # occupies rows ``cursor_row - n + 1`` .. ``cursor_row`` inclusive.
        # Bottom-anchored target is rows-n .. rows-1.
        overlay_top = cursor_row - n + 1
        desired_top = rows - n
        if overlay_top < 0 or overlay_top >= desired_top:
            return

        # Erase the n rows the overlay currently sits on, then rewrite the
        # overlay at the bottom-anchored position with n-1 newlines (no
        # trailing \n on the last line) so the last row sits at rows-1.
        buf = [_BSU]
        for i in range(n):
            row = overlay_top + 1 + i  # 1-indexed
            if 1 <= row <= rows:
                buf.append(f'\033[{row};1H\033[K')
        buf.append(f'\033[{desired_top + 1};1H')
        for i, line in enumerate(self._live_strs):
            if i < len(self._live_strs) - 1:
                buf.append(f'\033[K{line}\n')
            else:
                buf.append(f'\033[K{line}')
        buf.append(_ESU)
        sys.stdout.write(''.join(buf))
        sys.stdout.flush()
        # Record the new blank rows (the drifted-overlay's old position
        # that's now empty between scroll history and the bottom-anchored
        # overlay).  Merge with any pre-existing gap rows so _scroll_print
        # can fill them in order from the top.
        new_blanks = list(range(overlay_top, desired_top))
        self._gap_rows = sorted(set(self._gap_rows) | set(new_blanks))

        # Drain any scroll-history lines that were buffered during the
        # burst now that the overlay is bottom-anchored and _gap_rows
        # reflects the fresh drift gaps. Each pending line takes the
        # normal gap-fill / fallback path inside _scroll_print.
        if self._pending_scroll_lines:
            pending = self._pending_scroll_lines
            self._pending_scroll_lines = []
            for piece in pending:
                self._scroll_print(piece)

    def process_line(self, raw: str) -> None:
        line = _strip_ansi(raw).rstrip()

        # Resolve a pending stderr-close from the previous bare-`---` line.
        # The block only closes if this line is a colcon boundary; otherwise
        # the `---` was incidental content and we keep collecting.
        if self._pending_stderr_close:
            self._pending_stderr_close = False
            if _is_colcon_boundary(line):
                self._commit_stderr_close()
                # fall through to process the boundary line
            else:
                self._append_stderr_line('---')
                # fall through; _in_stderr is still True so the line continues
                # to be collected as stderr content below

        # Suppress colcon's summary block — we print our own in finalize()
        if self._in_summary:
            return
        if re.match(r'^Summary:', line):
            self._in_summary = True
            return

        # If a completion is buffered, flush it now unless this line is the
        # stderr block header for that same package (which we still need to
        # inspect for CTest errors before committing the result).
        if self._pending_state is not None and not self._in_stderr:
            m_check = re.match(r'^---\s+stderr:\s+(.+?)\s*(?:---\s*)?$', line)
            if not (m_check and m_check.group(1).strip() == self._pending_state.name):
                self._flush_pending()

        # Package started
        m = re.match(r'^Starting\s+>>>\s+(.+)$', line)
        if m:
            pkg = m.group(1).strip()
            self._building[pkg] = _PkgState(
                name=pkg,
                start=time.monotonic(),
                log_path=os.path.join(self._log_base, pkg, 'stdout.log'),
            )
            return

        # Package finished
        for pat, ok, aborted in (
            (r'^Finished\s+<<<\s+(.+?)\s+\[.+\]$', True, False),
            (r'^\[\s*OK\s*\]\s+(.+?)\s+\(.+\)$', True, False),
            (r'^Failed\s+<<<\s+(.+?)\s+\[.+\]$', False, False),
            (r'^Aborted\s+<<<\s+(.+?)\s+\[.+\]$', False, True),
        ):
            m = re.match(pat, line)
            if m:
                pkg = m.group(1).strip()
                state = self._building.pop(pkg, _PkgState(pkg, time.monotonic(), ''))
                state.end = time.monotonic()
                state.ok = ok
                state.aborted = aborted
                self._done.append(state)
                if ok:
                    if pkg in self._ctest_error_pkgs:
                        # CTest already reported errors via a preceding stderr block.
                        state.ok = False
                        self._ctest_error_pkgs.discard(pkg)
                        self._flush_completed(state)
                    else:
                        # Buffer — a following stderr block may contain CTest errors.
                        self._pending_state = state
                        self._pending_state_time = time.monotonic()
                else:
                    self._ctest_error_pkgs.discard(pkg)
                    self._flush_completed(state)
                return

        # Stderr block start — strip optional trailing ' ---' from the header
        # (colcon formats it as '--- stderr: PKG ---')
        m = re.match(r'^---\s+stderr:\s+(.+?)\s*(?:---\s*)?$', line)
        if m:
            self._stderr_pkg = m.group(1).strip()
            self._in_stderr = True
            return

        # Stderr block end — defer until the next line confirms it's a real
        # delimiter (i.e. followed by a colcon boundary).  This keeps a bare
        # `---` that appears inside content from prematurely closing the block.
        if line == '---' and self._in_stderr:
            self._pending_stderr_close = True
            return

        # Collect stderr lines
        if self._in_stderr and self._stderr_pkg:
            if _is_ctest_boilerplate(line):
                # Suppress CTest boilerplate; its presence means tests failed.
                # Works whether stderr arrives before or after Finished <<<.
                self._ctest_error_pkgs.add(self._stderr_pkg)
            else:
                self._append_stderr_line(line)
            return

        # Warning lines from colcon/CMake — color yellow, suppress known noise.
        # Colcon prefixes logger lines with a wall-clock timestamp; strip it
        # before matching so both "[0.3s] WARNING:..." and "WARNING:..." work.
        bare = _COLCON_TS_RE.sub('', line, count=1)
        if bare.startswith('WARNING:'):
            if not any(s in bare for s in _SUPPRESSED_WARNINGS):
                self._scroll_print(clr(bare, _YELLOW))
            return

        # Unknown lines — pass through for forward compatibility
        if line:
            self._scroll_print(line)

    def _append_stderr_line(self, line: str) -> None:
        """Append a line to the active stderr block (no-op if no block is open)."""
        if not self._stderr_pkg:
            return
        target = self._building.get(self._stderr_pkg) or next(
            (s for s in self._done if s.name == self._stderr_pkg), None)
        if target is not None:
            target.stderr.append(line)
            target.has_stderr = True

    def _commit_stderr_close(self) -> None:
        """Close the current stderr block and print its highlighted contents."""
        self._in_stderr = False
        pkg_name = self._stderr_pkg
        self._stderr_pkg = None
        if not pkg_name:
            return
        # If this closes a buffered completion, flush it now (possibly as
        # [ FAIL ] if CTest boilerplate was detected) so the result line
        # appears before the stderr output.
        if self._pending_state is not None and pkg_name == self._pending_state.name:
            self._flush_pending()
        state = self._building.get(pkg_name) or next(
            (s for s in self._done if s.name == pkg_name), None)
        if state and state.stderr:
            is_error = (state.ok is False) or (state.name in self._ctest_error_pkgs)
            color = _RED if is_error else _YELLOW
            self._scroll_print(f"\n{clr(f'--- stderr: {state.name} ---', color)}")
            for ln in highlight_stderr(state.stderr):
                self._scroll_print(f"  {ln}")
            self._scroll_print(clr('---', color))
            state.stderr = []  # clear so finalize() doesn't double-print

    def _build_overlay_lines(self, cols: int, spin: str = ' ') -> List[str]:
        """Build the list of overlay lines without any terminal I/O."""
        lines: List[str] = []

        for pkg, state in sorted(self._building.items()):
            elapsed = _fmt_duration(time.monotonic() - state.start)
            prog = self._progress_fn(state.log_path)
            if prog:
                state.last_progress = prog
            tag = clr(f'[run{spin}]', _CYAN)
            phase_str = self._fixed_phase or _infer_phase(state.last_progress)
            phase = f':{clr(phase_str, _BRIGHT_MAGENTA)}'
            if state.last_progress:
                pct, desc = state.last_progress
                visible_prefix = f"[run{spin}] {pkg}:{phase_str} ({elapsed}) [{pct}%] "
                max_desc = cols - len(visible_prefix) - 1
                desc = _truncate_desc(desc, max_desc)
                lines.append(f"{tag} {pkg}{phase} ({clr(elapsed, _BRIGHT_BLUE)}) [{clr(f'{pct}%', _BRIGHT_MAGENTA)}] {clr(desc, _DIM)}")
            else:
                lines.append(f"{tag} {pkg}{phase} ({clr(elapsed, _BRIGHT_BLUE)})")

        total_elapsed = _fmt_duration(time.monotonic() - self._build_start)
        n_done = len(self._done)
        n_total = self._total if self._total is not None else n_done + len(self._building)

        all_parts = []
        for pkg, state in sorted(self._building.items()):
            elapsed = _fmt_duration(time.monotonic() - state.start)
            if state.last_progress:
                pct, _ = state.last_progress
                all_parts.append(f"[{pkg} {clr(f'{pct}%', _BRIGHT_MAGENTA)} - {clr(elapsed, _BRIGHT_BLUE)}]")
            else:
                all_parts.append(f"[{pkg} - {clr(elapsed, _BRIGHT_BLUE)}]")

        self._status_offset = max(0, min(self._status_offset, max(0, len(all_parts) - 1)))
        offset = self._status_offset
        left_ind = f"{clr('<', _BOLD)} " if offset > 0 else ""
        header = f"[{clr(total_elapsed, _BRIGHT_BLUE)}] [{clr(str(n_done), _BOLD_GREEN)}/{clr(str(n_total), _GREEN)} done] {left_ind}"

        budget = cols - len(_strip_ansi(header)) - 2  # reserve 2 for ' >'
        kept = []
        for part in all_parts[offset:]:
            needed = len(_strip_ansi(part)) + (1 if kept else 0)
            if budget >= needed:
                kept.append(part)
                budget -= needed
            else:
                break

        has_right = (offset + len(kept)) < len(all_parts)
        right_ind = f" {clr('>', _BOLD)}" if has_right else ""
        lines.append(header + " ".join(kept) + right_ind)

        # Hard-cap each line's visible length.  The per-line truncation
        # heuristics inside this function can leave a line longer than the
        # available width — for example ``_truncate_desc`` returns the desc
        # unchanged when ``max_len <= 3`` (no room for an ellipsis), and a
        # long package-name prefix can already exceed cols on its own.
        max_visible = max(0, cols - 1)
        return [
            line if len(_strip_ansi(line)) <= max_visible
            else _truncate_ansi(line, max_visible)
            for line in lines
        ]

    @staticmethod
    def _phys_lines(lines: List[str], cols: int) -> int:
        """Physical terminal rows occupied by lines at a given column width."""
        return sum(max(1, (len(_strip_ansi(l)) + cols - 1) // cols) for l in lines)

    def render(self) -> None:
        """Redraw the live overlay."""
        if not self._tty:
            return

        # Flush a buffered [ ok ] once it's been idle long enough that no
        # trailing stderr block is going to arrive.  Without this, the most
        # recent completion stays invisible in a quiet build until the next
        # colcon event.
        if (self._pending_state is not None
                and time.monotonic() - self._pending_state_time >= _PENDING_FLUSH_S):
            self._flush_pending()

        if not self._building:
            self._erase_live()
            return

        self._cached_size = shutil.get_terminal_size((80, 24))
        cols = self._cached_size.columns
        # Advance the spinner only when we're actually about to draw a frame,
        # so the animation doesn't skip while the overlay is hidden.
        self._spin_idx = (self._spin_idx + 1) % len(_SPIN_FRAMES)
        # During the resize window, build lines with extra right-edge slack
        # so a subsequent small width reduction between this render and the
        # next won't wrap the just-rendered lines.  ``_build_overlay_lines``
        # already reserves a 1-char trailing margin internally, so we
        # subtract the remaining slack from the cols we pass in.
        margin = _RESIZE_LINE_MARGIN if self._in_resize_burst() else _OVERLAY_LINE_MARGIN
        line_cols = max(1, cols - (margin - 1))
        new_lines = self._build_overlay_lines(line_cols, _SPIN_FRAMES[self._spin_idx])

        # Bracket the full redraw in a synchronized-output block so the
        # terminal applies erase + writes atomically.  Without this, a
        # SIGWINCH-induced reflow can land between our \033[NA and \033[J or
        # between successive line writes and shift content under our feet.
        #
        # The overlay's bottom logical row is the cursor row (we don't add
        # a trailing \n after the last line — keeping the status flush with
        # the terminal's bottom edge instead of leaving a blank cursor row
        # underneath).  That means cursor sits inside the overlay's last
        # row (which is fine — the cursor is hidden via \033[?25l), and
        # the relative erase needs to move up (n_phys - 1) rows to reach
        # the overlay's top.
        buf: List[str] = [_BSU]
        new_n = len(new_lines)
        if self._live_lines > 0:
            # Use min(live_cols, current_cols) so that if the terminal was
            # narrowed since the last render, wrapped lines are counted correctly.
            erase_cols = min(self._live_cols, cols) if self._live_cols else cols
            n_phys = self._phys_lines(self._live_strs, erase_cols)
            # \r resets the cursor to column 0 before ESC[J — without it,
            # the cursor sits at the end-column of the last overlay line we
            # wrote (we omit the trailing \n to keep the overlay flush with
            # the bottom row), and ESC[NA preserves the column, so ESC[J
            # would only erase from that mid-row column rightward and leave
            # the left half of the top overlay row stale.
            if n_phys > 1:
                buf.append(f'\033[{n_phys - 1}A\r\033[J')
            else:
                # Cursor already on the only overlay row; just erase from it.
                buf.append('\r\033[J')
            # If the overlay shrinks (a package finished), move the cursor
            # down by the difference. After the erase, cursor sits at the
            # old overlay's top row; writing new_n < old_n lines from there
            # would leave the new overlay occupying old_top..old_top+new_n-1
            # with blank rows below — visually "raising" the status bar.
            # Shifting cursor down by (old_n - new_n) anchors the new
            # overlay's bottom row at the same row as the old overlay's.
            shrink = self._live_lines - new_n
            if shrink > 0:
                buf.append(f'\033[{shrink}B')
                # Those (shrink) rows just above the new overlay are now
                # blank. Assuming the overlay was bottom-anchored, those
                # blank rows are at (rows - old_n) .. (rows - new_n - 1).
                # Track them so the next scroll-history prints fill them
                # via gap-fill rather than triggering a scroll.
                if self._cached_size is not None:
                    rows = self._cached_size.lines
                    new_blanks = range(
                        rows - self._live_lines, rows - new_n)
                    self._gap_rows = sorted(
                        set(self._gap_rows) | set(new_blanks))
            elif shrink < 0 and self._gap_rows:
                # Growth: writing new_n > old_n lines from the old top
                # overflows the bottom of the screen, so the terminal
                # scrolls by (-shrink) rows. _gap_rows stores absolute
                # terminal rows, so any unfilled blanks shifted up by
                # the scroll amount — adjust references and drop any
                # that fell off the top or landed inside the new overlay.
                scroll = -shrink
                rows_total = (self._cached_size.lines
                              if self._cached_size is not None else None)
                new_top = (rows_total - new_n
                           if rows_total is not None else None)
                adjusted: List[int] = []
                for r in self._gap_rows:
                    r_new = r - scroll
                    if r_new < 0:
                        continue
                    if new_top is not None and r_new >= new_top:
                        continue
                    adjusted.append(r_new)
                self._gap_rows = adjusted

        for i, line in enumerate(new_lines):
            if i < len(new_lines) - 1:
                buf.append(f'{line}\n')
            else:
                # No trailing \n on the last line: the cursor ends on the
                # last overlay row so the overlay's bottom row is the
                # terminal's bottom row.
                buf.append(line)

        buf.append(_ESU)
        sys.stdout.write(''.join(buf))
        sys.stdout.flush()
        self._live_lines = len(new_lines)
        self._live_strs = new_lines
        self._live_cols = cols

    def finalize(self) -> None:
        if self._prev_winch is not None:
            try:
                signal.signal(signal.SIGWINCH, self._prev_winch)
            except (OSError, ValueError):
                pass
            self._prev_winch = None
        # Close the wakeup pipe once the signal handler is restored so no
        # writes can race with the close.
        if self._wakeup_pipe is not None:
            for fd in self._wakeup_pipe:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._wakeup_pipe = None
        # Clear winch_time so any remaining _scroll_print calls during
        # finalize bypass the resize-burst buffer — no more settle_reposition
        # will fire, so anything still buffered must print directly.
        self._winch_time = 0.0
        if self._pending_scroll_lines:
            pending = self._pending_scroll_lines
            self._pending_scroll_lines = []
            for piece in pending:
                self._scroll_print(piece)
        # Resolve any unresolved deferred close (input ended right after `---`).
        if self._pending_stderr_close:
            self._pending_stderr_close = False
            self._commit_stderr_close()
        self._flush_pending()
        self._erase_live()

        # Any packages still in-progress at interrupt time become aborted.
        if self._interrupted:
            now = time.monotonic()
            for state in sorted(self._building.values(), key=lambda s: s.name):
                state.end = now
                state.ok = False
                state.aborted = True
                self._done.append(state)
            self._building.clear()

        # Show stderr for any packages whose blocks weren't flushed inline.
        for state in self._done:
            if state.stderr:
                color = _RED if not state.ok else _YELLOW
                print(f"\n{clr(f'--- stderr: {state.name} ---', color)}")
                for ln in highlight_stderr(state.stderr):
                    print(f"  {ln}")
                print(clr('---', color))

        # For failed packages that produced no colcon stderr block, fall back to
        # stdout.log — build tool errors (compiler, linker) go there, not stderr.
        for state in self._done:
            if not state.ok and not state.has_stderr and state.log_path:
                lines = _extract_build_errors(state.log_path)
                if lines:
                    print(f"\n{clr(f'--- stdout: {state.name} ---', _RED)}")
                    for ln in highlight_stderr(lines):
                        print(f"  {ln}")
                    print(clr('---', _RED))

        if not self._show_build_summary:
            return

        # Summary
        n_ok = sum(1 for s in self._done if s.ok)
        n_fail = sum(1 for s in self._done if not s.ok and not s.aborted)
        n_abrt = sum(1 for s in self._done if s.aborted)
        total = len(self._done)
        failed_names = [s.name for s in self._done if not s.ok and not s.aborted]
        aborted_names = [s.name for s in self._done if s.aborted]
        warn_names = [s.name for s in self._done if s.ok and s.has_stderr]

        elapsed = f"({clr(_fmt_duration(time.monotonic() - self._build_start), _BRIGHT_BLUE)})"
        print()
        if self._interrupted:
            n_total = self._total or total
            print(f"{clr('Build interrupted', _YELLOW)}: "
                  f"{n_ok} of {n_total} package{'s' if n_total != 1 else ''} completed. {elapsed}")
            if aborted_names:
                print(f"  {clr('Aborted', _YELLOW)}: {', '.join(aborted_names)}")
            if failed_names:
                print(f"  {clr('Failed', _RED)}: {', '.join(failed_names)}")
        elif n_fail == 0 and n_abrt == 0:
            print(f"{clr('Build complete', _GREEN)}: "
                  f"{total} package{'s' if total != 1 else ''} built successfully. {elapsed}")
        else:
            print(f"{clr('Build failed', _RED)}: "
                  f"{n_ok} of {total} package{'s' if total != 1 else ''} succeeded. {elapsed}")
            if failed_names:
                print(f"  {clr('Failed', _RED)}: {', '.join(failed_names)}")
            if aborted_names:
                print(f"  {clr('Aborted', _YELLOW)}: {', '.join(aborted_names)}")
        if warn_names:
            print(f"  {clr('Warnings', _YELLOW)}: {', '.join(warn_names)}")
        print()

    def scroll_status(self, direction: int) -> None:
        """Shift the status bar view left (-1) or right (+1)."""
        self._status_offset = max(0, self._status_offset + direction)

    def _flush_pending(self) -> None:
        """Flush a buffered completion, applying CTest-detected failure if needed."""
        if self._pending_state is not None:
            if self._pending_state.name in self._ctest_error_pkgs:
                self._pending_state.ok = False
                self._ctest_error_pkgs.discard(self._pending_state.name)
            self._flush_completed(self._pending_state)
            self._pending_state = None

    def _flush_completed(self, state: _PkgState) -> None:
        """Print a completed-package line into scroll history."""
        dur = f"({clr(_fmt_duration((state.end or time.monotonic()) - state.start), _BRIGHT_BLUE)})"
        name = state.name.ljust(self._name_width) if self._name_width else state.name
        if state.ok:
            self._scroll_print(f"{clr('[ ok ]', _GREEN)} {name} {dur}")
        elif state.aborted:
            self._scroll_print(f"{clr('[ABRT]', _YELLOW)} {name} {dur}")
        else:
            self._scroll_print(f"{clr('[FAIL]', _BOLD_RED)} {name} {dur}")

    def _scroll_print(self, text: str) -> None:
        """Add ``text`` as a scroll-history line.

        If there are blank rows between the top of the scroll-history region
        and the overlay (from a recent settle reposition, tracked in
        ``_gap_rows``), fill the topmost blank in place — no scroll,
        overlay stays where it is.  Once the gap is exhausted (or wasn't
        present to begin with) the call falls through to the normal
        ``_erase_live`` + ``print`` pattern that lets the overlay scroll
        with the rest of the terminal.

        Multi-line ``text`` (containing embedded ``\\n``) is split into
        pieces; empty pieces are skipped so they don't waste gap rows.
        """
        pieces = text.split('\n') if '\n' in text else [text]
        for piece in pieces:
            if not piece:
                continue
            # Defer printing during a resize burst: writing absolute rows
            # while the terminal is reflowing leads to displaced content,
            # and settle_reposition's drift-erase may wipe whatever we
            # wrote. settle_reposition drains the queue once the burst
            # ends and the overlay has been re-anchored.
            if self._tty and self._in_resize_burst():
                self._pending_scroll_lines.append(piece)
                continue
            if (self._gap_rows
                    and self._tty
                    and self._cached_size is not None
                    and self._live_lines > 0):
                rows = self._cached_size.lines
                target_row = self._gap_rows[0]
                if 0 <= target_row < rows:
                    # Save the cursor, jump up to the topmost still-blank row
                    # tracked in _gap_rows, write the line there, then restore
                    # the cursor.  Writing at an explicit tracked row (rather
                    # than computing from current N) keeps gap-fill correct
                    # even when N has changed since the blank was created.
                    buf = [
                        _BSU,
                        '\033[s',           # save cursor
                        f'\033[{target_row + 1};1H',
                        '\033[K',
                        piece,
                        '\033[u',           # restore cursor
                        _ESU,
                    ]
                    sys.stdout.write(''.join(buf))
                    sys.stdout.flush()
                    self._gap_rows.pop(0)
                    continue
            # No gap (or not applicable) — standard erase + print flow,
            # which will let the next render redraw the overlay below.
            self._erase_live()
            if self._tty:
                sys.stdout.write('\033[?7h')
                sys.stdout.flush()
            print(piece, flush=True)
            if self._tty:
                sys.stdout.write('\033[?7l')
                sys.stdout.flush()

    def _erase_live(self) -> None:
        if not self._tty or self._live_lines == 0:
            return
        current_cols = shutil.get_terminal_size((80, 24)).columns
        cols = min(self._live_cols, current_cols) if self._live_cols else current_cols
        n_phys = self._phys_lines(self._live_strs, cols)
        # Cursor sits inside the overlay's last physical row (we don't trail
        # a \n on the last line) — go up (n_phys - 1) rows to reach the top.
        # Wrap in synchronized-output so a concurrent SIGWINCH-induced reflow
        # can't land between the cursor-up and the erase.
        # \r resets to col 0 so ESC[J erases the full row, not just the
        # tail past the cursor (see render() for the rationale).
        if n_phys > 1:
            sys.stdout.write(f'{_BSU}\033[{n_phys - 1}A\r\033[J{_ESU}')
        else:
            sys.stdout.write(f'{_BSU}\r\033[J{_ESU}')
        sys.stdout.flush()
        self._live_lines = 0
        self._live_strs = []
        self._live_cols = 0


class _KeyWatcher:
    """Non-blocking key reader in cbreak mode. Returns 'LEFT', 'RIGHT', or a char."""

    def __enter__(self):
        self._fd = None
        self._saved = None
        self._buf = b''
        if sys.stdin.isatty():
            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        return self

    def __exit__(self, *_):
        if self._saved is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)

    def read(self) -> Optional[str]:
        if self._fd is None:
            return None
        # Drain all newly available bytes into the buffer.
        if select.select([self._fd], [], [], 0)[0]:
            self._buf += os.read(self._fd, 32)
        if not self._buf:
            return None
        # Complete arrow-key escape sequence: ESC [ C/D
        if len(self._buf) >= 3 and self._buf[0] == 0x1b and self._buf[1] == ord('['):
            key = {ord('C'): 'RIGHT', ord('D'): 'LEFT'}.get(self._buf[2])
            if key:
                self._buf = self._buf[3:]
                return key
        # Partial escape sequence — wait briefly for the rest.
        if self._buf[0] == 0x1b and len(self._buf) < 3:
            if select.select([self._fd], [], [], 0.05)[0]:
                self._buf += os.read(self._fd, 32)
                if len(self._buf) >= 3 and self._buf[1] == ord('['):
                    key = {ord('C'): 'RIGHT', ord('D'): 'LEFT'}.get(self._buf[2])
                    if key:
                        self._buf = self._buf[3:]
                        return key
            self._buf = self._buf[1:]
            return None
        # Printable ASCII character.
        ch = self._buf[0]
        self._buf = self._buf[1:]
        return chr(ch) if ch >= 32 else None

    def query_cursor_row(self) -> Optional[int]:
        """Send a CPR request and return the 0-based cursor row, or None.

        Any bytes that arrive before/after the CPR response are kept in the
        internal buffer so they are not lost for subsequent read() calls.
        """
        if self._fd is None:
            return None
        sys.stdout.write('\033[6n')
        sys.stdout.flush()
        deadline = time.monotonic() + 0.1
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            if select.select([self._fd], [], [], remaining)[0]:
                self._buf += os.read(self._fd, 32)
            m = re.search(rb'\033\[(\d+);(\d+)R', self._buf)
            if m:
                row = int(m.group(1)) - 1  # convert to 0-based
                self._buf = self._buf[:m.start()] + self._buf[m.end():]
                return row
        return None


def _run_with_status(process, nice: int, display: StatusDisplay) -> int:
    """Drive a colcon subprocess with the given live display.

    Returns the process exit code (1 on KeyboardInterrupt).
    """
    q: queue.Queue = queue.Queue()

    def _reader():
        try:
            for raw in iter(process.stdout.readline, b''):
                q.put(raw.decode('utf-8', errors='replace').rstrip('\n'))
        finally:
            q.put(None)

    threading.Thread(target=_reader, daemon=True).start()

    last_nice = 0.0
    done = False
    using_ansi = supports_ansi()

    if using_ansi:
        sys.stdout.write('\033[?25l\033[?7l')  # hide cursor, disable line wrap
        sys.stdout.flush()

    try:
        with _KeyWatcher() as keys:
            while not done:
                while True:
                    try:
                        line = q.get_nowait()
                    except queue.Empty:
                        break
                    if line is None:
                        done = True
                        break
                    display.process_line(line)

                key = keys.read()
                if key in ('RIGHT', 'd'):
                    display.scroll_status(1)
                elif key in ('LEFT', 'a'):
                    display.scroll_status(-1)

                if not done:
                    display.render()
                    # One-shot post-resize reposition.  If a resize burst
                    # just settled, query CPR to see where the overlay
                    # actually ended up and snap it back to the bottom if
                    # it drifted up.  Only runs once per burst.
                    if display.needs_settle_reposition():
                        row = keys.query_cursor_row()
                        display.settle_reposition(row)

                now = time.monotonic()
                if now - last_nice >= _RENICE_INTERVAL_S and nice != 0:
                    subprocess.run(
                        f"renice -n {nice} -p "
                        f"$(pgrep -g $(ps -o pgid= -p {process.pid}))",
                        shell=True, executable='/bin/bash',
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    last_nice = now

                if not done:
                    # Sleep on the wakeup pipe instead of time.sleep so a
                    # resize-mode-entry SIGWINCH can interrupt the wait and
                    # let the next render fire immediately with the wider
                    # margin.  Falls back to plain sleep when no pipe is
                    # available (non-tty / pipe-creation failed).
                    wakeup_fd = display.wakeup_fd
                    if wakeup_fd != -1:
                        try:
                            ready, _, _ = select.select(
                                [wakeup_fd], [], [], _RENDER_INTERVAL_S)
                        except (InterruptedError, OSError):
                            ready = ()
                        if ready:
                            try:
                                os.read(wakeup_fd, 4096)  # drain
                            except OSError:
                                pass
                    else:
                        time.sleep(_RENDER_INTERVAL_S)
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        # Drain any final output the reader thread already queued.
        while True:
            try:
                line = q.get_nowait()
            except queue.Empty:
                break
            if line is None:
                break
            display.process_line(line)
        display._interrupted = True
        display.finalize()
        return 1
    finally:
        if using_ansi:
            sys.stdout.write('\033[?7h\033[?25h')  # re-enable line wrap, show cursor
            sys.stdout.flush()

    process.wait()  # normal exit path
    display.finalize()
    return process.returncode


def run_build_with_status(process, workspace: str, nice: int, total: Optional[int] = None,
                          pkg_names: Optional[List[str]] = None) -> int:
    """Drive a colcon build subprocess with a live per-package status display."""
    display = StatusDisplay(workspace, total=total, pkg_names=pkg_names)
    return _run_with_status(process, nice, display)


def run_test_with_status(process, workspace: str, nice: int, total: Optional[int] = None,
                         pkg_names: Optional[List[str]] = None) -> int:
    """Drive a colcon test subprocess with a live per-package status display.

    Returns the process exit code.  The caller is responsible for running
    print_test_results() afterward to show the per-test breakdown.
    """
    display = StatusDisplay(
        workspace, total=total,
        log_subdir='latest_test',
        progress_fn=_parse_test_progress,
        show_build_summary=False,
        pkg_names=pkg_names,
        phase='test',
    )
    return _run_with_status(process, nice, display)
