import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import yaml

from .common import (check_colcon_event_handlers, get_workspace_dir, get_package,
                     clr, supports_ansi, _fmt_duration, _strip_ansi,
                     _GREEN, _YELLOW, _RED, _BOLD_RED,
                     _BRIGHT_BLUE, _BRIGHT_MAGENTA, _DIM)


def register(subparsers):
    parser = subparsers.add_parser("test", help="Tests a colcon workspace.")
    parser.add_argument("--workspace", "-w", default=".",
                        help="The path to the colcon workspace (default: \".\")")
    packages_group = parser.add_argument_group('Packages', 'Select packages to test.')
    packages_group.add_argument(
        "pkgs", metavar="PKGNAME", nargs='*', type=str,
        help='Explicitly specify a list of specific packages to test.')
    packages_group.add_argument(
        "--this", action="store_true",
        help="Test the package containing the current working directory.")
    packages_group.add_argument(
        "--no-deps", action="store_true",
        help="Only test specified packages, not their dependencies.")
    config_group = parser.add_argument_group(
        'Config', "Parameters for the underlying build system.")
    config_group.add_argument(
        "--colcon-build-args", metavar='ARG', dest='colcon_build_args',
        nargs="+", required=False, type=str, default=None,
        help="Additional arguments for colcon")
    config_group.add_argument("--verbose", "-v", action="store_true",
                              help="Show the status of every individual test case.")
    config_group.add_argument("--results-only", "-r", action="store_true",
                              help="Show results from the last test run without re-running.")
    parser.set_defaults(func=test_command)


def get_xunit_path_from_cmdline(cmdline):
    """Extract the xunit result file path from a run_test.py or --gtest_output FullCommandLine."""
    try:
        tokens = shlex.split(cmdline)
        for i, token in enumerate(tokens):
            if 'run_test.py' in token and i + 1 < len(tokens):
                return tokens[i + 1]
            if token.startswith('--gtest_output=xml:'):
                return token[len('--gtest_output=xml:'):]
            if token == '--xunit-file' and i + 1 < len(tokens):
                return tokens[i + 1]
    except Exception:
        pass
    return None


def parse_xunit_results(xunit_path):
    """Parse a JUnit/xunit/GTest XML file.

    Returns (total, passed, skipped, failures, errors, failed_names, all_cases) or None on error.
    all_cases is a list of (name, status, detail) where status is 'passed', 'failed',
    'skipped', or 'error'.
    """
    try:
        tree = ET.parse(xunit_path)
        root = tree.getroot()
        total = failures = errors = skipped = 0
        failed_names = []
        all_cases = []

        suites = root.findall('testsuite') if root.tag == 'testsuites' else [root]
        for suite in suites:
            total += int(suite.get('tests', 0))
            failures += int(suite.get('failures', 0))
            errors += int(suite.get('errors', 0))
            skipped += int(suite.get('skipped', suite.get('disabled', 0)))
            for tc in suite.findall('testcase'):
                tc_name = tc.get('name', 'unknown')
                detail = None
                fail_el = tc.find('failure')
                err_el = tc.find('error')
                if fail_el is not None:
                    status = 'failed'
                    failed_names.append(tc_name)
                    detail = (fail_el.text or '').strip() or fail_el.get('message', '')
                elif err_el is not None:
                    status = 'error'
                    failed_names.append(tc_name)
                    detail = err_el.get('message') or (err_el.text or '').strip()
                elif tc.find('skipped') is not None or tc.get('status') == 'notrun':
                    status = 'skipped'
                else:
                    status = 'passed'
                all_cases.append((tc_name, status, detail))

        passed = total - failures - errors - skipped
        return total, passed, skipped, failures, errors, failed_names, all_cases
    except Exception:  # missing file, malformed XML, or unexpected schema
        return None


def parse_cpplint_output(output):
    """Parse cpplint output into (total, n_passed, n_failed, error_lines), or None.

    cpplint prints 'Done processing <file>' for every file it completes and error
    lines as 'file:line:  message  [category] [N]'. Failures are counted only for
    files that appear in both lists; files with errors but no 'Done processing' line
    (e.g. third-party headers that caused a processing exception) are included in
    error_lines for display but not counted against the total.
    """
    processed = []
    per_file_errors = {}
    for line in output.splitlines():
        if line.startswith('Done processing '):
            processed.append(line[len('Done processing '):])
        else:
            m = re.match(r'^(.+?):\d+:', line)
            if m and '[' in line and ']' in line:
                per_file_errors.setdefault(m.group(1), []).append(line)
    if not processed:
        return None
    n_failed = sum(1 for f in processed if f in per_file_errors)
    all_error_lines = [line for errors in per_file_errors.values() for line in errors]
    return len(processed), len(processed) - n_failed, n_failed, all_error_lines


def get_latest_ctest_xml(pkg_build_dir):
    """Return the path to the most recent Test.xml for a package, or None."""
    testing_dir = os.path.join(pkg_build_dir, 'Testing')
    if not os.path.isdir(testing_dir):
        return None
    timestamps = sorted([
        d for d in os.listdir(testing_dir)
        if os.path.isdir(os.path.join(testing_dir, d)) and d != 'Temporary'
    ])
    if not timestamps:
        return None
    xml_path = os.path.join(testing_dir, timestamps[-1], 'Test.xml')
    return xml_path if os.path.isfile(xml_path) else None


def _print_diff_detail(detail: str, indent: str) -> None:
    """Print a diff/failure detail block with syntax coloring."""
    for line in detail.splitlines():
        if line.startswith('+') and not line.startswith('+++'):
            color = _GREEN
        elif line.startswith('-') and not line.startswith('---'):
            color = _RED
        elif line.startswith('@@') or line.startswith('---') or line.startswith('+++'):
            color = _BRIGHT_BLUE
        else:
            color = None
        print(f"{indent}{clr(line, color) if color else line}")


def _build_counts_str(xunit, cpplint_result, suite_ok):
    """Build the colored counts string for one suite. Returns (counts_str, visible_len)."""
    if xunit:
        _, n_passed, n_skipped, n_failures, n_errors, _, _ = xunit
        counts = []
        if n_passed:
            counts.append(clr(f"{n_passed} passed", _GREEN))
        if n_skipped:
            counts.append(clr(f"{n_skipped} skipped", _YELLOW))
        if n_failures:
            counts.append(clr(f"{n_failures} failed", _BOLD_RED))
        if n_errors:
            counts.append(clr(f"{n_errors} errors", _BOLD_RED))
        counts_str = ", ".join(counts) if counts else "0 tests"
    elif cpplint_result:
        _, n_passed_cp, n_failed_cp, _ = cpplint_result
        counts = []
        if n_passed_cp:
            counts.append(clr(f"{n_passed_cp} passed", _GREEN))
        if n_failed_cp:
            counts.append(clr(f"{n_failed_cp} failed", _BOLD_RED))
        counts_str = ", ".join(counts) if counts else "0 files"
    else:
        counts_str = clr("passed", _GREEN) if suite_ok else clr("FAILED", _BOLD_RED)
    return counts_str, len(_strip_ansi(counts_str))


def _collect_pkg_suites(build_dir, pkg):
    """Parse CTest XML for one package.

    Returns (suite_data, stats) or None if no results exist.
    suite_data is a list of (name, label, exec_time, xunit, suite_ok, cpplint_result).
    stats is a dict with keys: n_suites, suites_passed, tests, passed, skipped,
    failed_tests, any_failed.
    """
    ctest_xml = get_latest_ctest_xml(os.path.join(build_dir, pkg))
    if ctest_xml is None:
        return None
    try:
        root = ET.parse(ctest_xml).getroot()
    except Exception:  # missing file or malformed XML
        return None

    test_entries = root.findall('.//Testing/Test')
    if not test_entries:
        return None

    suite_data = []
    pkg_tests = pkg_passed = pkg_skipped = pkg_failed_tests = pkg_suites_passed = 0
    pkg_failed = False

    for entry in test_entries:
        name = entry.findtext('Name', '')
        cmdline = entry.findtext('FullCommandLine', '')
        suite_ok = entry.get('Status') == 'passed'

        exec_time = None
        for nm in entry.findall('.//NamedMeasurement'):
            if nm.get('name') == 'Execution Time':
                try:
                    exec_time = float(nm.findtext('Value', '0'))
                except ValueError:
                    pass

        labels = [lbl.text for lbl in entry.findall('.//Label') if lbl.text]
        label = labels[0] if labels else ''

        xunit_path = get_xunit_path_from_cmdline(cmdline)
        xunit = None
        if xunit_path and os.path.isfile(xunit_path):
            xunit = parse_xunit_results(xunit_path)

        if xunit:
            n_total, n_passed, n_skipped, n_failures, n_errors, _, _ = xunit
            pkg_tests += n_total
            pkg_passed += n_passed
            pkg_skipped += n_skipped
            pkg_failed_tests += n_failures + n_errors
            if n_failures or n_errors:
                suite_ok = False
            if not label and '--gtest_output' in cmdline:
                label = 'gtest'

        if suite_ok:
            pkg_suites_passed += 1
        else:
            pkg_failed = True

        cpplint_result = None
        if not xunit and 'cpplint' in cmdline.lower():
            if not label:
                label = 'cpplint'
            cpplint_result = parse_cpplint_output(entry.findtext('.//Measurement/Value', ''))
            if cpplint_result:
                n_total, n_passed_cp, n_failed_cp, _ = cpplint_result
                pkg_tests += n_total
                pkg_passed += n_passed_cp
                pkg_failed_tests += n_failed_cp

        suite_data.append((name, label, exec_time, xunit, suite_ok, cpplint_result))

    stats = {
        'n_suites': len(test_entries),
        'suites_passed': pkg_suites_passed,
        'tests': pkg_tests,
        'passed': pkg_passed,
        'skipped': pkg_skipped,
        'failed_tests': pkg_failed_tests,
        'any_failed': pkg_failed,
    }
    return suite_data, stats


def _print_suite_entry(name, label, exec_time, xunit, suite_ok, cpplint_result,
                       name_w, label_w, counts_str, padding, verbose):
    """Print one suite's result line and optional per-test details."""
    tag = clr("[ ok ]", _GREEN) if suite_ok else clr("[FAIL]", _BOLD_RED)
    label_str = f" [{label}]" if label else ""
    time_str = (f"  ({clr(f'{exec_time:.2f}s', _BRIGHT_BLUE)})"
                if exec_time is not None else "")

    print(f"  {tag} {name:<{name_w}}{label_str:<{label_w}}  "
          f"{counts_str}{padding}{time_str}")

    if xunit:
        _, _, _, _, _, failed_names, all_cases = xunit
        if verbose:
            for tc_name, tc_status, detail in all_cases:
                tc_tag = (clr("[ ok ]", _GREEN) if tc_status == 'passed' else
                          clr("[skip]", _YELLOW) if tc_status == 'skipped' else
                          clr("[FAIL]", _BOLD_RED))
                print(f"       {tc_tag} {tc_name}")
                if detail and tc_status in ('failed', 'error'):
                    _print_diff_detail(detail, "              ")
        elif failed_names:
            for tc_name, tc_status, detail in all_cases:
                if tc_status not in ('failed', 'error'):
                    continue
                print(f"         FAILED: {tc_name}")
                if detail:
                    _print_diff_detail(detail, "                ")
    elif cpplint_result:
        _, _, n_failed_cp, error_lines = cpplint_result
        if n_failed_cp:
            for err_line in error_lines:
                print(f"         {clr(err_line, _RED)}")


def print_test_results(workspace, build_space, verbose=False, packages=None, elapsed=None):
    """Parse CTest XML files and print a nested test result summary.

    Returns 0 if all tests passed, 1 if any failed.
    If packages is provided, only results for those packages are shown.
    """
    build_dir = os.path.join(workspace, build_space)
    if not os.path.isdir(build_dir):
        print("No build directory found, no test results to show.")
        return 1

    pkg_list = sorted([
        d for d in os.listdir(build_dir)
        if os.path.isdir(os.path.join(build_dir, d, 'Testing'))
    ])
    if packages:
        pkg_list = [p for p in pkg_list if p in packages]
    if not pkg_list:
        print("No test results found.")
        return 0

    total_suites = total_suites_passed = 0
    total_tests = total_passed = total_skipped = total_failed = 0
    any_failure = False

    sep = clr("─" * min(70, shutil.get_terminal_size().columns), _BRIGHT_MAGENTA)
    print()
    print(sep)

    for pkg in pkg_list:
        result = _collect_pkg_suites(build_dir, pkg)
        if result is None:
            continue
        suite_data, stats = result

        if stats['any_failed']:
            any_failure = True
        total_suites += stats['n_suites']
        total_suites_passed += stats['suites_passed']
        total_tests += stats['tests']
        total_passed += stats['passed']
        total_skipped += stats['skipped']
        total_failed += stats['failed_tests']

        if stats['tests'] > 0:
            parts = [clr(f"{stats['passed']} passed", _GREEN)]
            if stats['skipped']:
                parts.append(clr(f"{stats['skipped']} skipped", _YELLOW))
            if stats['failed_tests']:
                parts.append(clr(f"{stats['failed_tests']} failed", _BOLD_RED))
            print(f"{pkg}: {stats['suites_passed']}/{stats['n_suites']} suites passed"
                  f"  ({', '.join(parts)})")
        else:
            print(f"{pkg}: {stats['suites_passed']}/{stats['n_suites']} suites passed")

        name_w = max(len(s[0]) for s in suite_data)
        label_w = max((len(f" [{s[1]}]") if s[1] else 0) for s in suite_data)

        suite_counts = [
            _build_counts_str(xunit, cpplint_result, suite_ok)
            for _, _, _, xunit, suite_ok, cpplint_result in suite_data
        ]
        counts_w = max(vis for _, vis in suite_counts)

        for (name, label, exec_time, xunit, suite_ok, cpplint_result), \
                (counts_str, counts_vis) in zip(suite_data, suite_counts):
            padding = " " * (counts_w - counts_vis)
            _print_suite_entry(name, label, exec_time, xunit, suite_ok, cpplint_result,
                               name_w, label_w, counts_str, padding, verbose)

        print()

    print(sep)
    suite_str = f"{total_suites_passed}/{total_suites} suites"
    summary_status = clr("FAILED", _BOLD_RED) if any_failure else clr("passed", _GREEN)
    elapsed_str = (f" ({clr(_fmt_duration(elapsed), _BRIGHT_BLUE)})"
                   if elapsed is not None else "")
    if total_tests > 0:
        test_parts = [clr(f"{total_passed} passed", _GREEN)]
        if total_skipped:
            test_parts.append(clr(f"{total_skipped} skipped", _YELLOW))
        if total_failed:
            test_parts.append(clr(f"{total_failed} failed", _BOLD_RED))
        print(f"Summary: {suite_str} | {', '.join(test_parts)} -- {summary_status}{elapsed_str}")
    else:
        print(f"Summary: {suite_str} -- {summary_status}{elapsed_str}")
    print(sep)

    return 1 if any_failure else 0


def _list_packages(workspace, packages, no_deps):
    """Return the list of package names colcon will test, or None on failure."""
    cmd = ["colcon", "list", "-n"]
    if packages:
        cmd += ["--packages-select" if no_deps else "--packages-up-to"] + packages
    try:
        result = subprocess.run(cmd, cwd=workspace, capture_output=True, text=True)
        names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return names if names else None
    except Exception:
        return None


def test_command(args):
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
        "nice": 0,
        "extend_path": "",
        "install_space": "install",
        "test_result_space": "test_results"
    }
    with open(config_file, "r") as f:
        config_content.update(yaml.safe_load(f))
    # The `or` guards below handle keys explicitly set to null/empty in the YAML.

    extend_path = config_content.get("extend_path", None)
    extend_prefix = ""
    if extend_path:
        extend_script = os.path.join(extend_path, "setup.bash")
        if not os.path.exists(extend_script):
            print(f"Error: '{extend_script}' does not exist.")
            sys.exit(1)
        extend_prefix = f"source {extend_script} && "

    build_space = config_content.get("build_space") or "build"

    if args.results_only:
        packages = args.pkgs
        if args.this:
            current_package = get_package(args.workspace)
            if current_package:
                packages.append(current_package)
        # Expand to the full dependency set colcon would have tested, so the
        # summary matches what a prior `hatchy test <pkg>` would have shown.
        resolved_pkgs = _list_packages(workspace, packages, args.no_deps) if packages else None
        result_code = print_test_results(
            workspace, build_space, verbose=args.verbose,
            packages=resolved_pkgs)
        sys.exit(result_code)

    colcon_cmd = ["colcon", "test"]
    colcon_cmd += ['--build-base', build_space]

    test_result_space = config_content.get("test_result_space") or "test_results"
    colcon_cmd += ['--test-result-base', test_result_space]

    if args.colcon_build_args:
        colcon_cmd += args.colcon_build_args

    nice = config_content.get("nice") or 0

    packages = args.pkgs
    if args.this:
        current_package = get_package(args.workspace)
        if current_package:
            packages.append(current_package)

    if packages:
        if args.no_deps:
            colcon_cmd += ['--packages-select'] + packages
        else:
            colcon_cmd += ['--packages-up-to'] + packages

    use_status_display = supports_ansi()

    if use_status_display:
        if check_colcon_event_handlers(workspace, extend_prefix, ['status-', 'parallel_status-']):
            colcon_cmd += ['--event-handlers', 'status-', 'parallel_status-']
        elif check_colcon_event_handlers(workspace, extend_prefix, ['status-']):
            colcon_cmd += ['--event-handlers', 'status-']

    colcon_shell_cmd = extend_prefix + ' '.join(colcon_cmd)

    print(clr(f"Running: {colcon_shell_cmd}", _DIM))

    # Resolve the full set of packages colcon will actually test (the explicit
    # selection plus dependencies, unless --no-deps), so the post-run summary
    # reflects this run rather than every package with stale test_results.
    pkg_names = _list_packages(workspace, packages, args.no_deps)
    total = len(pkg_names) if pkg_names else None

    test_start = time.monotonic()
    if use_status_display:
        from .status_display import run_test_with_status
        env = {**os.environ, 'PYTHONUNBUFFERED': '1'}
        process = subprocess.Popen(
            colcon_shell_cmd,
            cwd=workspace,
            shell=True,
            executable="/bin/bash",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
        test_returncode = run_test_with_status(
            process, workspace, nice, total=total, pkg_names=pkg_names)
    else:
        process = subprocess.Popen(
            colcon_shell_cmd,
            cwd=workspace,
            shell=True,
            executable="/bin/bash",
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        while process.poll() is None:
            subprocess.run(
                f"renice -n {nice} -p $(pgrep -g $(ps -o pgid= -p {process.pid}))",
                shell=True,
                executable="/bin/bash",
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            time.sleep(1)
        test_returncode = process.returncode
    test_elapsed = time.monotonic() - test_start

    result_code = print_test_results(
        workspace, build_space, verbose=args.verbose,
        packages=pkg_names,
        elapsed=test_elapsed)
    sys.exit(max(test_returncode, result_code))
