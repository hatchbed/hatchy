import argparse
import os
import re
import shlex
import sys

import yaml

from .common import (get_workspace_dir, remove_duplicates, print_workspace_state)

BUILD_TYPES = ['Debug', 'Release', 'RelWithDebInfo', 'MinSizeRel', 'Default']
CACHES = ['ccache', 'sccache', 'Default']
BOOL_OPTIONS = ['on', 'off', 'Default']
GENERATORS = ['ninja', 'make', 'Default']
_GENERATOR_MAP = {'ninja': 'Ninja', 'make': 'Unix Makefiles'}

_VERSIONED_GCC_RE = re.compile(r'^gcc(-\d+)?$')
_VERSIONED_CLANG_RE = re.compile(r'^clang(-\d+)?$')
_LINKER_RE = re.compile(r'^(lld(-\d+)?|gold|mold)$')

_CMAKE_BUILD_TYPE_RE = re.compile(r'^-DCMAKE_BUILD_TYPE(:[A-Z_]+)?=', re.IGNORECASE)
_CMAKE_COMPILER_RE = re.compile(r'^-DCMAKE_(C|CXX)_COMPILER(:[A-Z_]+)?=', re.IGNORECASE)
_CMAKE_LINKER_FLAGS_RE = re.compile(r'^-DCMAKE_(EXE|MODULE|SHARED)_LINKER_FLAGS(:[A-Z_]+)?=', re.IGNORECASE)
_CMAKE_COMPILER_LAUNCHER_RE = re.compile(r'^-DCMAKE_(C|CXX)_COMPILER_LAUNCHER(:[A-Z_]+)?=', re.IGNORECASE)
_CMAKE_BUILD_TESTING_RE = re.compile(r'^-DBUILD_TESTING(:[A-Z_]+)?=', re.IGNORECASE)
_CMAKE_EXPORT_COMPILE_COMMANDS_RE = re.compile(r'^-DCMAKE_EXPORT_COMPILE_COMMANDS(:[A-Z_]+)?=', re.IGNORECASE)


def _ci_choice(choices):
    """Return a type converter that accepts any casing and normalizes to the canonical choice."""
    lookup = {c.lower(): c for c in choices}
    def convert(value):
        normalized = lookup.get(value.lower())
        if normalized is None:
            raise argparse.ArgumentTypeError(
                f"invalid choice {value!r} (valid: {', '.join(choices)})")
        return normalized
    return convert


def _normalize_free(value):
    """Lowercase the value, mapping any casing of 'default' to canonical 'Default'."""
    return 'Default' if value.lower() == 'default' else value.lower()


def _set_cmake_args(colcon_args, regex, new_values):
    tokens = []
    for arg in colcon_args:
        tokens.extend(shlex.split(arg))

    filtered = [t for t in tokens if not regex.match(t)]

    if new_values:
        if '--cmake-args' in filtered:
            idx = filtered.index('--cmake-args')
            for j, val in enumerate(new_values):
                filtered.insert(idx + 1 + j, val)
        else:
            filtered.extend(['--cmake-args'] + new_values)
    else:
        # Drop --cmake-args that ended up with no following cmake arguments
        cleaned = []
        for i, token in enumerate(filtered):
            if token == '--cmake-args':
                next_is_cmake_arg = i + 1 < len(filtered) and not filtered[i + 1].startswith('--')
                if not next_is_cmake_arg:
                    continue
            cleaned.append(token)
        filtered = cleaned

    return [' '.join(shlex.quote(t) for t in filtered)] if filtered else []


def set_cmake_generator(colcon_args, generator):
    tokens = []
    for arg in colcon_args:
        tokens.extend(shlex.split(arg))

    # Remove existing -G <value> pair
    filtered = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token == '-G':
            skip_next = True
            continue
        filtered.append(token)

    if generator != 'Default':
        cmake_gen = _GENERATOR_MAP[generator]
        if '--cmake-args' in filtered:
            idx = filtered.index('--cmake-args')
            filtered[idx + 1:idx + 1] = ['-G', cmake_gen]
        else:
            filtered.extend(['--cmake-args', '-G', cmake_gen])
    else:
        # Drop --cmake-args that ended up with no following cmake arguments
        cleaned = []
        for i, token in enumerate(filtered):
            if token == '--cmake-args':
                next_is_cmake_arg = i + 1 < len(filtered) and not filtered[i + 1].startswith('--')
                if not next_is_cmake_arg:
                    continue
            cleaned.append(token)
        filtered = cleaned

    return [' '.join(shlex.quote(t) for t in filtered)] if filtered else []


def set_cmake_build_type(colcon_args, build_type):
    new_values = [] if build_type == 'Default' else [f'-DCMAKE_BUILD_TYPE={build_type}']
    return _set_cmake_args(colcon_args, _CMAKE_BUILD_TYPE_RE, new_values)


def _cxx_from_c_compiler(compiler):
    m = _VERSIONED_GCC_RE.match(compiler)
    if m:
        return f'g++{m.group(1) or ""}'
    m = _VERSIONED_CLANG_RE.match(compiler)
    if m:
        return f'clang++{m.group(1) or ""}'
    return None


def set_cmake_compiler(colcon_args, compiler):
    new_values = []
    if compiler != 'Default':
        cxx = _cxx_from_c_compiler(compiler)
        if cxx is None:
            raise ValueError(f"Unrecognized compiler '{compiler}'. Expected: gcc, gcc-<N>, clang, clang-<N>.")
        new_values = [f'-DCMAKE_C_COMPILER={compiler}', f'-DCMAKE_CXX_COMPILER={cxx}']
    return _set_cmake_args(colcon_args, _CMAKE_COMPILER_RE, new_values)


def set_cmake_linker(colcon_args, linker):
    new_values = []
    if linker != 'Default':
        flag = f'-fuse-ld={linker}'
        new_values = [
            f'-DCMAKE_EXE_LINKER_FLAGS={flag}',
            f'-DCMAKE_MODULE_LINKER_FLAGS={flag}',
            f'-DCMAKE_SHARED_LINKER_FLAGS={flag}',
        ]
    return _set_cmake_args(colcon_args, _CMAKE_LINKER_FLAGS_RE, new_values)


def set_cmake_ccache(colcon_args, cache):
    new_values = []
    if cache != 'Default':
        new_values = [
            f'-DCMAKE_C_COMPILER_LAUNCHER={cache}',
            f'-DCMAKE_CXX_COMPILER_LAUNCHER={cache}',
        ]
    return _set_cmake_args(colcon_args, _CMAKE_COMPILER_LAUNCHER_RE, new_values)


def set_cmake_build_testing(colcon_args, value):
    new_values = [] if value == 'Default' else [f'-DBUILD_TESTING={value.upper()}']
    return _set_cmake_args(colcon_args, _CMAKE_BUILD_TESTING_RE, new_values)


def set_cmake_compile_commands(colcon_args, value):
    new_values = [] if value == 'Default' else [f'-DCMAKE_EXPORT_COMPILE_COMMANDS={value.upper()}']
    return _set_cmake_args(colcon_args, _CMAKE_EXPORT_COMPILE_COMMANDS_RE, new_values)


def register(subparsers):
    parser = subparsers.add_parser("config", help="Configures a colcon workspace's context.")
    parser.add_argument("--workspace", "-w", default=".",
                        help="The path to the colcon workspace (default: \".\")")
    behavior_group = parser.add_argument_group('Behavior', 'Options affecting argument handling.')
    behavior_group.add_argument("--append-args", "-a", action="store_true",
                                help="Append elements to list-type arguments")
    behavior_group.add_argument("--remove-args", "-r", action="store_true",
                                help="Remove elements from list-type arguments")
    context_group = parser.add_argument_group(
        'Workspace Context', 'Options affecting the context of the workspace.')
    context_group.add_argument("--extend", "-e",
                               help="Extend the result-space of another colcon workspace")
    context_group.add_argument("--no-extend", action="store_true",
                               help="Unset the explicit extension of another workspace")
    spaces_group = parser.add_argument_group('Spaces', 'Location of parts of the colcon workspace.')
    spaces_group.add_argument("--build-space", "--build", "-b", help="Path to the build space")
    spaces_group.add_argument("--default-build-space", action="store_true",
                              help="Use the default build space ('build')")
    spaces_group.add_argument("--install-space", "--install", "-i",
                              help="Path to the install space")
    spaces_group.add_argument("--default-install-space", action="store_true",
                              help="Use the default install space ('install')")
    spaces_group.add_argument("--test-result-space", "--test", "-t",
                              help="Path to the test result space")
    spaces_group.add_argument("--default-test-result-space", action="store_true",
                              help="Use the default test result space ('test_results')")
    spaces_group.add_argument("--space-suffix", "-x",
                              help="Suffix for build, test results, and install space")
    build_group = parser.add_argument_group(
        'Build Options', 'Options for configuring the way packages are built.')
    build_group.add_argument("--no-colcon-build-args", action="store_true",
                             help="Pass no additional arguments to colcon")
    build_group.add_argument(
        "--colcon-build-args", metavar='ARG', dest='colcon_build_args',
        nargs="+", required=False, type=str, default=None,
        help="Additional arguments for colcon")
    build_group.add_argument(
        "--generator", choices=GENERATORS, metavar='GENERATOR',
        type=_ci_choice(GENERATORS),
        help=f"CMake generator: {', '.join(GENERATORS)}. "
             "'Default' removes -G from colcon build args.")
    build_group.add_argument(
        "--build-type", choices=BUILD_TYPES, metavar='TYPE',
        type=_ci_choice(BUILD_TYPES),
        help=f"CMake build type: {', '.join(BUILD_TYPES)}. "
             "'Default' removes -DCMAKE_BUILD_TYPE from colcon build args.")
    build_group.add_argument(
        "--compiler", metavar='COMPILER',
        type=_normalize_free,
        help="C/C++ compiler: gcc, gcc-<N>, clang, clang-<N>, Default. "
             "'Default' removes -DCMAKE_C/CXX_COMPILER from colcon build args.")
    build_group.add_argument(
        "--linker", metavar='LINKER',
        type=_normalize_free,
        help="Linker: lld, lld-<N>, gold, mold, Default. "
             "'Default' removes linker flags from colcon build args.")
    build_group.add_argument(
        "--ccache", choices=CACHES, metavar='CACHE',
        type=_ci_choice(CACHES),
        help=f"Compiler cache: {', '.join(CACHES)}. "
             "'Default' removes compiler launcher flags from colcon build args.")
    build_group.add_argument(
        "--build-testing", choices=BOOL_OPTIONS, metavar='VALUE',
        type=_ci_choice(BOOL_OPTIONS),
        help=f"Build test targets (-DBUILD_TESTING): {', '.join(BOOL_OPTIONS)}. "
             "'Default' removes -DBUILD_TESTING from colcon build args.")
    build_group.add_argument(
        "--compile-commands", choices=BOOL_OPTIONS, metavar='VALUE',
        type=_ci_choice(BOOL_OPTIONS),
        help=f"Export compile commands (-DCMAKE_EXPORT_COMPILE_COMMANDS): {', '.join(BOOL_OPTIONS)}. "
             "'Default' removes the flag from colcon build args.")
    build_group.add_argument(
        "--nice", "-n", type=int, help="CPU niceness for build commands. (default: 0)")
    build_group.add_argument(
        "--cpus", "-c", type=int, help="Max CPU budget.")
    parser.set_defaults(func=config_command)


def config_command(args):
    workspace = os.path.abspath(args.workspace)

    if not os.path.exists(workspace):
        print(f"Error: The specified workspace directory '{workspace}' does not exist.")
        sys.exit(1)

    workspace = get_workspace_dir(workspace)
    if workspace is None:
        print(f"Error: Parent colcon workspace directory does not exist.")
        sys.exit(1)

    config_file = os.path.join(workspace, ".hatch", "config.yaml")

    if not os.path.exists(config_file):
        print(f"Error: Workspace has not been initialized. Run 'hatch init' first.")
        sys.exit(1)

    config_content = {
        "build_space": "build",
        "colcon_build_args": [],
        "extend_path": "",
        "install_space": "install",
        "test_result_space": "test_results"
    }

    with open(config_file, "r") as f:
        config_content = yaml.safe_load(f)

    if args.extend:
        config_content['extend_path'] = args.extend
    elif args.no_extend:
        config_content['extend_path'] = ""

    if args.build_space:
        config_content['build_space'] = args.build_space
    elif args.default_build_space:
        config_content['build_space'] = "build"

    if args.install_space:
        config_content['install_space'] = args.install_space
    elif args.default_install_space:
        config_content['install_space'] = "install"

    if args.test_result_space:
        config_content['test_result_space'] = args.test_result_space
    elif args.default_test_result_space:
        config_content['test_result_space'] = "test_results"

    if args.space_suffix:
        if config_content["build_space"] == "build":
            config_content["build_space"] = "build" + args.space_suffix
        if config_content["install_space"] == "install":
            config_content["install_space"] = "install" + args.space_suffix
        if config_content["test_result_space"] == "test_results":
            config_content["test_result_space"] = "test_results" + args.space_suffix

    if args.colcon_build_args:
        colcon_args = config_content.get('colcon_build_args', []) or []
        if args.append_args:
            config_content['colcon_build_args'] = colcon_args + args.colcon_build_args
        elif args.remove_args and config_content['colcon_build_args']:
            config_content['colcon_build_args'] = [
                a for a in colcon_args if a not in args.colcon_build_args]
        else:
            config_content['colcon_build_args'] = args.colcon_build_args
        config_content['colcon_build_args'] = remove_duplicates(
            config_content['colcon_build_args'])

    if args.no_colcon_build_args:
        config_content['colcon_build_args'] = []

    if args.generator:
        colcon_args = config_content.get('colcon_build_args', []) or []
        config_content['colcon_build_args'] = set_cmake_generator(colcon_args, args.generator)

    if args.build_type:
        colcon_args = config_content.get('colcon_build_args', []) or []
        config_content['colcon_build_args'] = set_cmake_build_type(colcon_args, args.build_type)

    if args.compiler:
        if args.compiler != 'Default' and _cxx_from_c_compiler(args.compiler) is None:
            print(f"Error: unrecognized compiler '{args.compiler}'. "
                  "Expected: gcc, gcc-<N>, clang, clang-<N>, or Default.")
            sys.exit(1)
        colcon_args = config_content.get('colcon_build_args', []) or []
        config_content['colcon_build_args'] = set_cmake_compiler(colcon_args, args.compiler)

    if args.linker:
        if args.linker != 'Default' and not _LINKER_RE.match(args.linker):
            print(f"Error: unrecognized linker '{args.linker}'. "
                  "Expected: lld, lld-<N>, gold, mold, or Default.")
            sys.exit(1)
        colcon_args = config_content.get('colcon_build_args', []) or []
        config_content['colcon_build_args'] = set_cmake_linker(colcon_args, args.linker)

    if args.ccache:
        colcon_args = config_content.get('colcon_build_args', []) or []
        config_content['colcon_build_args'] = set_cmake_ccache(colcon_args, args.ccache)

    if args.build_testing:
        colcon_args = config_content.get('colcon_build_args', []) or []
        config_content['colcon_build_args'] = set_cmake_build_testing(colcon_args, args.build_testing)

    if args.compile_commands:
        colcon_args = config_content.get('colcon_build_args', []) or []
        config_content['colcon_build_args'] = set_cmake_compile_commands(colcon_args, args.compile_commands)

    if args.nice:
        config_content['nice'] = args.nice

    if args.cpus:
        config_content['cpus'] = args.cpus

    with open(config_file, "w") as f:
        yaml.dump(config_content, f, default_flow_style=False)

    print_workspace_state(workspace)
    sys.exit(0)
