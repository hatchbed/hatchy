import os
import subprocess
import sys
import time

from .common import (check_colcon_event_handlers, get_workspace_dir, get_package, clr,
                     supports_ansi, _DIM)


def register(subparsers):
    parser = subparsers.add_parser("build", help="Builds a colcon workspace.")
    parser.add_argument("--workspace", "-w", default=".",
                        help="The path to the colcon workspace (default: \".\")")
    packages_group = parser.add_argument_group(
        'Packages', 'Clean workspace subdirectories for the selected profile.')
    packages_group.add_argument(
        "pkgs", metavar="PKGNAME", nargs='*', type=str,
        help='Explicilty specify a list of specific packages to build.')
    packages_group.add_argument(
        "--this", action="store_true",
        help="Build the package containing the current working directory.")
    packages_group.add_argument(
        "--no-deps", action="store_true",
        help="Only build specified packages, not their dependencies.")
    config_group = parser.add_argument_group('Config', "Parameters for the underlying build system.")
    config_group.add_argument(
        "--colcon-build-args", metavar='ARG', dest='colcon_build_args',
        nargs="+", required=False, type=str, default=None,
        help="Additional arguments for colcon")
    config_group.add_argument(
        "--nice", "-n", type=int, help="CPU niceness for build commands. (default: 0)")
    parser.set_defaults(func=build_command)


def _list_packages(workspace, packages, no_deps):
    """Return the list of package names colcon will build, or None on failure."""
    cmd = ["colcon", "list", "-n"]
    if packages:
        cmd += ["--packages-select" if no_deps else "--packages-up-to"] + packages
    try:
        result = subprocess.run(cmd, cwd=workspace, capture_output=True, text=True)
        names = [l.strip() for l in result.stdout.splitlines() if l.strip()]
        return names if names else None
    except Exception:
        return None

def build_command(args):
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

    import yaml
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

    extend_path = config_content.get("extend_path", None)
    extend_prefix = ""
    if extend_path:
        extend_script = os.path.join(extend_path, "setup.bash")
        if not os.path.exists(extend_script):
            print(f"Error: '{extend_script}' does not exist.")
            sys.exit(1)
        extend_prefix = f"source {extend_script} && "

    colcon_cmd = ["colcon", "build"]

    build_space = config_content.get("build_space", "build") or "build"
    colcon_cmd += ['--build-base', build_space]

    install_space = config_content.get("install_space", "install") or "install"
    colcon_cmd += ['--install-base', install_space]

    test_result_space = config_content.get("test_result_space", "test_results") or "test_results"
    colcon_cmd += ['--test-result-base', test_result_space]

    colcon_build_args = config_content.get("colcon_build_args", []) or []
    if args.colcon_build_args:
        colcon_build_args = args.colcon_build_args

    nice = config_content.get("nice", 0) or 0
    if args.nice is not None:
        nice = args.nice

    colcon_cmd += colcon_build_args

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

    if use_status_display:
        from .status_display import run_build_with_status
        pkg_names = _list_packages(workspace, packages, args.no_deps)
        total = len(pkg_names) if pkg_names else None
        env = {**os.environ, 'PYTHONUNBUFFERED': '1', 'VERBOSE': '1'}
        process = subprocess.Popen(
            colcon_shell_cmd,
            cwd=workspace,
            shell=True,
            executable="/bin/bash",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
        returncode = run_build_with_status(process, workspace, nice, total=total, pkg_names=pkg_names)
        sys.exit(returncode)
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
        sys.exit(process.returncode)
