import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import yaml

from .common import (get_workspace_dir, get_package,
                     get_dependent_packages, delete_matching_dirs,
                     clr, _CYAN, _DIM, _BRIGHT_BLUE, _BRIGHT_MAGENTA, _YELLOW)


def _dir_size(path: str) -> int:
    """Return total bytes used by a directory tree."""
    try:
        result = subprocess.run(['du', '-sb', path], capture_output=True, text=True)
        if result.returncode == 0:
            return int(result.stdout.split()[0])
    except (OSError, ValueError):
        pass
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for fname in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fname))
            except OSError:
                pass
    return total


def _fmt_size(n: int) -> str:
    for unit, threshold in (('GB', 1 << 30), ('MB', 1 << 20), ('KB', 1 << 10)):
        if n >= threshold:
            return f'{n / threshold:.1f} {unit}'
    return f'{n} B'


def _print_size_report(target_paths: list, packages: list) -> None:
    target_names = [os.path.basename(p) for p in target_paths]

    ansi_w = len(clr('', _CYAN))  # width of start+reset escape pair for padding math

    def _sfmt(n: int) -> str:
        return clr(_fmt_size(n), _DIM) if n == 0 else _fmt_size(n)

    if packages:
        # Measure all directories matching each package name within each
        # target path.  Uses rglob to match delete_matching_dirs behaviour —
        # the log space nests packages under timestamped run directories so a
        # direct-child check would always return 0.
        pkg_set = set(packages)
        sizes = {pkg: {t: 0 for t in target_names} for pkg in packages}
        for path, name in zip(target_paths, target_names):
            for subdir in Path(path).rglob('*'):
                if subdir.is_dir() and subdir.name in pkg_set:
                    sizes[subdir.name][name] += _dir_size(str(subdir))

        pkg_col_w = max(len(p) for p in packages)
        col_totals = {t: sum(sizes[p][t] for p in packages) for t in target_names}
        grand_total = sum(col_totals.values())
        pkg_totals = {p: sum(sizes[p][t] for t in target_names) for p in packages}

        size_col_w = {
            t: max(len(t), max(len(_fmt_size(sizes[p][t])) for p in packages),
                   len(_fmt_size(col_totals[t])))
            for t in target_names
        }
        total_col_w = max(len('total'),
                          max(len(_fmt_size(pkg_totals[p])) for p in packages),
                          len(_fmt_size(grand_total)))

        header = f"  {clr('Package', _CYAN):<{pkg_col_w + ansi_w}}"
        for t in target_names:
            header += f"  {clr(t, _CYAN):>{size_col_w[t] + ansi_w}}"
        header += f"  {clr('total', _CYAN):>{total_col_w + ansi_w}}"
        sep_w = pkg_col_w + sum(2 + w for w in size_col_w.values()) + 2 + total_col_w
        sep = clr('  ' + '─' * sep_w, _BRIGHT_MAGENTA)

        print("\nSpace to be recovered:")
        print(header)
        print(sep)
        for pkg in packages:
            row = f"  {clr(pkg, _BRIGHT_BLUE):<{pkg_col_w + ansi_w}}"
            for t in target_names:
                row += f"  {_sfmt(sizes[pkg][t]):>{size_col_w[t]}}"
            row += f"  {_sfmt(pkg_totals[pkg]):>{total_col_w}}"
            print(row)
        print(sep)
        total_row = f"  {'total':<{pkg_col_w}}"
        for t in target_names:
            total_row += f"  {_sfmt(col_totals[t]):>{size_col_w[t]}}"
        total_row += f"  {_sfmt(grand_total):>{total_col_w}}"
        print(total_row)
    else:
        dir_sizes = [(os.path.basename(p), _dir_size(p)) for p in target_paths]
        grand_total = sum(s for _, s in dir_sizes)
        name_w = max(len(n) for n, _ in dir_sizes)
        size_w = max(len(_fmt_size(s)) for _, s in dir_sizes + [('', grand_total)])
        sep_w = name_w + 2 + size_w
        sep = clr('  ' + '─' * sep_w, _BRIGHT_MAGENTA)

        print("\nSpace to be recovered:")
        print(sep)
        for name, s in dir_sizes:
            print(f"  {clr(name, _CYAN):<{name_w + ansi_w}}  {_sfmt(s):>{size_w}}")
        print(sep)
        print(f"  {'total':<{name_w}}  {_sfmt(grand_total):>{size_w}}")


def register(subparsers):
    parser = subparsers.add_parser("clean", help="Deletes various products of the build verb.")
    parser.add_argument("--workspace", "-w", default=".",
                        help="The path to the colcon workspace (default: \".\")")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Assume \"yes\" to all interactive checks.")
    spaces_group = parser.add_argument_group(
        'Spaces', 'Clean workspace subdirectories for the selected profile.')
    spaces_group.add_argument("--build-space", "--build", "-b", action="store_true",
                              help="Remove the entire build space")
    spaces_group.add_argument("--install-space", "--install", "-i", action="store_true",
                              help="Remove the entire install space")
    spaces_group.add_argument("--test-result-space", "--test", "-t", action="store_true",
                              help="Remove the entire test result space")
    spaces_group.add_argument("--log-space", "--logs", "-l", action="store_true",
                              help="Remove the entire log space")
    packages_group = parser.add_argument_group(
        'Packages', 'Clean workspace subdirectories for the selected profile.')
    packages_group.add_argument(
        "pkgs", metavar="PKGNAME", nargs='*', type=str,
        help='Explicilty specify a list of specific packages to clean from the build, '
             'devel, and install space.')
    packages_group.add_argument(
        "--this", action="store_true",
        help="Clean the package containing the current working directory from the build "
             "and install space.")
    packages_group.add_argument(
        "--dependents", "--dep", action="store_true",
        help="Clean the packages which depend on the packages to be cleaned.")
    parser.set_defaults(func=clean_command)


def clean_command(args):
    workspace = os.path.abspath(args.workspace)

    if not os.path.exists(workspace):
        print(f"Error: The specified workspace directory '{workspace}' does not exist.")
        sys.exit(1)

    workspace = get_workspace_dir(workspace)
    if workspace is None:
        print(f"Error: Parent colcon workspace directory does not exist.")
        sys.exit(1)

    config_file = os.path.join(workspace, ".hatch", "config.yaml")

    config_content = {
        "build_space": "build",
        "install_space": "install",
        "test_result_space": "test_results"
    }
    if os.path.exists(config_file):
        with open(config_file, "r") as f:
            config_content.update(yaml.safe_load(f))

    build_space = config_content.get("build_space", "build") or "build"
    install_space = config_content.get("install_space", "install") or "install"
    test_result_space = config_content.get("test_result_space", "test_results") or "test_results"

    targets = []
    if args.build_space:
        targets.append(build_space)
    if args.install_space:
        targets.append(install_space)
    if args.test_result_space:
        targets.append(test_result_space)
    if args.log_space:
        targets.append("log")
    if len(targets) == 0:
        targets = [build_space, install_space, test_result_space, "log"]

    target_paths = [
        os.path.join(workspace, t)
        for t in targets
        if os.path.isdir(os.path.join(workspace, t))
    ]

    if len(target_paths) == 0:
        print("Nothing to clean.")
        return

    packages = args.pkgs
    if args.this:
        current_package = get_package(args.workspace)
        if current_package:
            packages.append(current_package)

    if len(packages) > 0 and args.dependents:
        packages = get_dependent_packages(packages)

    if len(packages) > 0:
        print("Cleaning the following packages:")
        pkgs_str = textwrap.fill(' '.join(packages), width=70)
        pkgs_str = "\n".join(
            ['    ' + ' '.join(clr(p, _BRIGHT_BLUE) for p in line.split())
             for line in pkgs_str.splitlines()])
        print(pkgs_str)
        print("  from:")
        print("\n".join(['    ' + clr(t, _DIM) for t in target_paths]))
    else:
        print("Cleaning:")
        print("\n".join(['    ' + clr(t, _DIM) for t in target_paths]))

    _print_size_report(target_paths, packages)

    if not args.yes:
        try:
            response = input(f"\nAre you sure you want to continue? {clr('(y/N)', _YELLOW)}: ").strip().lower()
        except KeyboardInterrupt:
            print()
            print(clr("Aborting.", _DIM))
            exit(1)
        if response not in ("y", "yes"):
            print(clr("Aborting.", _DIM))
            exit(1)

    if len(packages) > 0:
        for target_path in target_paths:
            delete_matching_dirs(target_path, packages)
    else:
        for target_path in target_paths:
            shutil.rmtree(target_path)
