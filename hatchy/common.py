import os
import re
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import yaml
from pathlib import Path

# ANSI color codes
_RESET = "\033[0m"
_DIM = "\033[2m"
_BOLD = "\033[1m"

# Standard colors
_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"

# Bold variants
_BOLD_RED = "\033[1;31m"
_BOLD_GREEN = "\033[1;32m"

# Bright (high-intensity) variants
_BRIGHT_BLUE = "\033[94m"
_BRIGHT_MAGENTA = "\033[95m"


# Matches OSC sequences (ESC ] ... BEL or ESC ] ... ST) and CSI/Fe sequences.
_ANSI_RE = re.compile(
    r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)'
    r'|\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])'
)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text)


def _truncate_ansi(text: str, max_visible: int) -> str:
    """Truncate ``text`` to at most ``max_visible`` visible characters,
    preserving (non-printable) ANSI escape sequences.

    Any escape sequences immediately following the truncation point are kept
    so a trailing reset (``\\x1b[0m``) flushes through.  A defensive reset is
    appended whenever truncation occurs, in case the cut left an SGR active.
    """
    if max_visible <= 0:
        return ''
    out = []
    visible = 0
    i = 0
    while i < len(text) and visible < max_visible:
        if text[i] == '\x1b':
            m = _ANSI_RE.match(text, i)
            if m:
                out.append(text[m.start():m.end()])
                i = m.end()
                continue
        out.append(text[i])
        visible += 1
        i += 1
    truncated = i < len(text)
    while i < len(text) and text[i] == '\x1b':
        m = _ANSI_RE.match(text, i)
        if not m:
            break
        out.append(text[m.start():m.end()])
        i = m.end()
    if truncated:
        out.append(_RESET)
    return ''.join(out)


def supports_ansi() -> bool:
    """True when stdout looks like a terminal that can render ANSI escapes."""
    return sys.stdout.isatty() and os.environ.get('TERM', '') not in ('', 'dumb')


def clr(text, code):
    """Wrap text in an ANSI color code when stdout supports it."""
    if supports_ansi():
        return f"{code}{text}{_RESET}"
    return text


def _fmt_duration(secs: float) -> str:
    if secs < 60:
        return f"{secs:.1f}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{int(m)}min {s:.1f}s"
    h, m = divmod(int(m), 60)
    return f"{h}h {int(m)}min {s:.1f}s"


def remove_duplicates(lst):
    seen = set()
    return [x for x in lst if not (x in seen or seen.add(x))]


def delete_matching_dirs(root_dir, names_to_delete):
    root_path = Path(root_dir)
    for subdir in root_path.rglob('*'):
        if subdir.is_dir() and subdir.name in names_to_delete:
            shutil.rmtree(subdir)


def get_dependent_packages(packages):
    cmd = ["colcon", "list", "-n", "--packages-above"] + packages
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    pkgs = [pkg for pkg in result.stdout.splitlines() if "not found" not in pkg.lower()]
    return pkgs


def split_arguments(args, splitter_index):
    start_index = splitter_index + 1
    end_index = args.index('--', start_index) if '--' in args[start_index:] else None

    if end_index:
        return (
            args[0:splitter_index],
            args[start_index:end_index],
            args[(end_index + 1):]
        )
    else:
        return (
            args[0:splitter_index],
            args[start_index:],
            []
        )


def get_colcon_build_args(verb, args):
    if verb not in ['build', 'config', 'test']:
        return args, []

    ordered_splitters = reversed(
        [(i, t) for i, t in enumerate(args) if t in ['--colcon-build-args']])

    head_args = args
    tail_args = []
    colcon_build_args = []
    for index, name in ordered_splitters:
        head_args, colcon_args, tail = split_arguments(head_args, splitter_index=index)
        tail_args.extend(tail)
        colcon_build_args.extend(colcon_args)

    args = head_args + tail_args
    return args, colcon_build_args


def get_workspace_dir(current_dir):
    current_dir = os.path.abspath(current_dir)
    while current_dir != os.path.dirname(current_dir):
        src_path = os.path.join(current_dir, 'src')
        config_path = os.path.join(current_dir, '.hatch', 'config.yaml')
        if os.path.isdir(src_path) and os.path.isfile(config_path):
            return current_dir
        current_dir = os.path.dirname(current_dir)
    return None


def parse_package_name(file_path):
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
        if root.tag != "package":
            return None
        name_element = root.find("name")
        if name_element is None:
            return None
        return name_element.text.strip() if name_element.text else None
    except Exception:
        return None


def get_package(current_dir):
    current_dir = os.path.abspath(current_dir)
    while current_dir != os.path.dirname(current_dir):
        package_file = os.path.join(current_dir, 'package.xml')
        if os.path.isfile(package_file):
            name = parse_package_name(package_file)
            if name:
                return name
        current_dir = os.path.dirname(current_dir)
    return None


# Width of the status tag column ("[exists] " padded to match "[missing] ").
_STATUS_TAG_W = len("[missing] ")


_GENERATOR_DISPLAY = {'Ninja': 'ninja', 'Unix Makefiles': 'make'}


def _tool_exists(tool_type: str, val: str) -> bool:
    if tool_type == 'generator':
        return shutil.which(val) is not None
    elif tool_type == 'linker':
        return (shutil.which(f'ld.{val}') is not None
                or shutil.which(val) is not None)
    else:  # compiler, ccache
        if os.path.isabs(val):
            return os.path.isfile(val)
        return shutil.which(val) is not None


def parse_cmake_settings(colcon_build_args):
    tokens = []
    for arg in colcon_build_args or []:
        tokens.extend(shlex.split(arg))

    build_type = None
    compiler = None
    linker = None
    ccache = None
    build_testing = None
    compile_commands = None
    generator = None

    skip = set()
    for i, token in enumerate(tokens):
        if i in skip:
            continue

        if token == '-G' and i + 1 < len(tokens):
            raw = tokens[i + 1]
            generator = _GENERATOR_DISPLAY.get(raw, raw)
            skip.add(i + 1)
            continue

        m = re.match(r'^-DCMAKE_BUILD_TYPE(?::[A-Z_]+)?=(.+)$', token, re.IGNORECASE)
        if m:
            build_type = m.group(1)
            continue

        # Match C_COMPILER= but NOT C_COMPILER_LAUNCHER= (the _LAUNCHER suffix
        # prevents the optional (?::[A-Z_]+)? group from consuming it, so '='
        # fails to match the underscore and the regex correctly rejects it).
        m = re.match(r'^-DCMAKE_C_COMPILER(?::[A-Z_]+)?=(.+)$', token, re.IGNORECASE)
        if m:
            compiler = m.group(1)
            continue

        m = re.match(r'^-DCMAKE_EXE_LINKER_FLAGS(?::[A-Z_]+)?=-fuse-ld=(.+)$', token, re.IGNORECASE)
        if m:
            linker = m.group(1)
            continue

        m = re.match(r'^-DCMAKE_C_COMPILER_LAUNCHER(?::[A-Z_]+)?=(.+)$', token, re.IGNORECASE)
        if m:
            ccache = m.group(1)
            continue

        m = re.match(r'^-DBUILD_TESTING(?::[A-Z_]+)?=(.+)$', token, re.IGNORECASE)
        if m:
            build_testing = m.group(1).lower()
            continue

        m = re.match(r'^-DCMAKE_EXPORT_COMPILE_COMMANDS(?::[A-Z_]+)?=(.+)$', token, re.IGNORECASE)
        if m:
            compile_commands = m.group(1).lower()
            continue

    return {
        'generator': generator,
        'build_type': build_type,
        'compiler': compiler,
        'linker': linker,
        'ccache': ccache,
        'build_testing': build_testing,
        'compile_commands': compile_commands,
    }


def print_workspace_state(workspace):
    src_dir = os.path.join(workspace, "src")
    config_file = os.path.join(workspace, ".hatch", "config.yaml")

    colcon_build_args = []
    extend_path = None
    build_space = "build"
    install_space = "install"
    test_result_space = "test_results"
    nice = 0

    if os.path.exists(config_file):
        with open(config_file, "r") as f:
            config = yaml.safe_load(f)
            colcon_build_args = config.get('colcon_build_args', [])
            extend_path = config.get('extend_path', "") or None
            if extend_path is not None and len(extend_path.strip()) == 0:
                extend_path = None
            build_space = config.get("build_space", "build") or "build"
            install_space = config.get("install_space", "install") or "install"
            test_result_space = config.get("test_result_space", "test_results") or "test_results"
            nice = config.get("nice", 0) or 0

    build_dir = os.path.join(workspace, build_space)
    install_dir = os.path.join(workspace, install_space)
    test_results_dir = os.path.join(workspace, test_result_space)
    env_extend_path = os.environ.get("COLCON_PREFIX_PATH", None)

    term_w = shutil.get_terminal_size().columns
    sep = clr("─" * min(70, term_w), _BRIGHT_MAGENTA)

    # Status-section keys are padded so their status tags ("[exists]" /
    # "[missing]") start at the same column; plain-section keys are padded so
    # their values start at the column where status-section *values* begin.
    status_keys = ["Build Space:", "Install Space:", "Test Result Space:", "Source Space:"]
    key_w = max(len(k) for k in status_keys) + 1
    value_col = key_w + _STATUS_TAG_W

    def _key_pad(label, width):
        # Pad the plain label to width, then colorize. ANSI codes don't affect
        # visible width, so we must compute padding before coloring.
        return clr(label, _CYAN) + ' ' * (width - len(label))

    def _space_status(path, missing_color=_YELLOW):
        # "[exists]" is 1 char shorter than "[missing]" — pad to the longer
        # width so paths align at the same column for both states.
        if os.path.exists(path):
            label, color = "[exists]", _GREEN
        else:
            label, color = "[missing]", missing_color
        gap = ' ' * (_STATUS_TAG_W - len(label))
        return f"{clr(label, color)}{gap}{path}"

    print(sep)
    if extend_path is None:
        if env_extend_path is None:
            print(_key_pad('Extending:', value_col))
        else:
            print(f"{_key_pad('Extending:', value_col)}{clr('[env]', _DIM)} {env_extend_path}")
    else:
        print(f"{_key_pad('Extending:', value_col)}{extend_path}")
    print(f"{_key_pad('Workspace:', value_col)}{workspace}")
    print(sep)
    print(f"{_key_pad('Build Space:', key_w)}{_space_status(build_dir)}")
    print(f"{_key_pad('Install Space:', key_w)}{_space_status(install_dir)}")
    print(f"{_key_pad('Test Result Space:', key_w)}{_space_status(test_results_dir)}")
    print(f"{_key_pad('Source Space:', key_w)}{_space_status(src_dir, missing_color=_RED)}")
    cmake = parse_cmake_settings(colcon_build_args)

    def _cmake_status(val, default=''):
        if val is None:
            tag = '[default]'
            gap = ' ' * (_STATUS_TAG_W - len(tag))
            return f"{clr(tag, _DIM)}{gap}{default}"
        return ' ' * _STATUS_TAG_W + val

    def _tool_status(val, tool_type):
        if val is None:
            tag = '[default]'
            gap = ' ' * (_STATUS_TAG_W - len(tag))
            return f"{clr(tag, _DIM)}{gap}"
        if _tool_exists(tool_type, val):
            label, color = '[exists]', _GREEN
        else:
            label, color = '[missing]', _YELLOW
        gap = ' ' * (_STATUS_TAG_W - len(label))
        return f"{clr(label, color)}{gap}{val}"

    print(sep)
    print(f"{_key_pad('CPU Niceness:', value_col)}{nice}")
    if not colcon_build_args:
        print(f"{_key_pad('Colcon Build Args:', value_col)}None")
    else:
        print(f"{_key_pad('Colcon Build Args:', value_col)}{colcon_build_args[0]}")
        for arg in colcon_build_args[1:]:
            print(f"{' ' * value_col}{arg}")
    print(f"{_key_pad('Generator:', key_w)}{_tool_status(cmake['generator'], 'generator')}")
    print(f"{_key_pad('Build Type:', key_w)}{_cmake_status(cmake['build_type'])}")
    print(f"{_key_pad('Compiler:', key_w)}{_tool_status(cmake['compiler'], 'compiler')}")
    print(f"{_key_pad('Linker:', key_w)}{_tool_status(cmake['linker'], 'linker')}")
    print(f"{_key_pad('Compiler Cache:', key_w)}{_tool_status(cmake['ccache'], 'ccache')}")
    print(f"{_key_pad('Build Testing:', key_w)}{_cmake_status(cmake['build_testing'], 'on')}")
    print(f"{_key_pad('Compile Commands:', key_w)}{_cmake_status(cmake['compile_commands'], 'off')}")
    print(sep)
