import os
import shutil
import subprocess
import sys

from .common import get_workspace_dir, parse_package_name, clr, _CYAN, _DIM, _BRIGHT_BLUE, _BRIGHT_MAGENTA


def register(subparsers):
    parser = subparsers.add_parser(
        "list", help="Lists colcon packages in the workspace or other arbitrary folders.")
    list_subparsers = parser.add_subparsers(dest="list_command")

    packages_parser = list_subparsers.add_parser("packages", help="List packages in workspace.")
    packages_parser.add_argument("--workspace", "-w", default=".",
                                 help="The path to the colcon workspace (default: \".\")")
    packages_parser.set_defaults(func=list_packages_command)

    repos_parser = list_subparsers.add_parser("repos", help="List repos in workspace.")
    repos_parser.add_argument("--workspace", "-w", default=".",
                              help="The path to the colcon workspace (default: \".\")")
    repos_parser.set_defaults(func=list_repos_command)


_VCS_MARKERS = {
    ".git": "git",
    ".hg":  "hg",
    ".svn": "svn",
    ".bzr": "bzr",
}


def find_packages(src_dir):
    """Walk src_dir and return a sorted list of (name, rel_path) for each package.xml found."""
    workspace = os.path.dirname(src_dir)
    packages = []
    for dirpath, dirnames, filenames in os.walk(src_dir):
        if "package.xml" in filenames:
            name = parse_package_name(os.path.join(dirpath, "package.xml"))
            if name:
                rel = os.path.relpath(dirpath, workspace)
                packages.append((name, rel))
    return sorted(packages, key=lambda p: p[0])


def find_repos(src_dir):
    """Walk src_dir and return a sorted list of repo dicts, not descending into nested repos."""
    workspace = os.path.dirname(src_dir)
    repos = []

    def _walk(directory):
        try:
            entries = list(os.scandir(directory))
        except PermissionError:
            return
        subdirs = []
        vcs_type = None
        for entry in entries:
            if entry.is_dir(follow_symlinks=False) and entry.name in _VCS_MARKERS:
                vcs_type = _VCS_MARKERS[entry.name]
            elif entry.is_dir(follow_symlinks=False):
                subdirs.append(entry.path)
        if vcs_type:
            info = {"path": os.path.relpath(directory, workspace), "type": vcs_type}
            if vcs_type == "git":
                url = subprocess.run(
                    ["git", "-C", directory, "remote", "get-url", "origin"],
                    capture_output=True, text=True).stdout.strip()
                branch = subprocess.run(
                    ["git", "-C", directory, "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True, text=True).stdout.strip()
                info["url"] = url or ""
                info["version"] = branch or ""
            repos.append(info)
        else:
            for subdir in subdirs:
                _walk(subdir)

    _walk(src_dir)
    return sorted(repos, key=lambda r: r["path"])


def _resolve_workspace(args):
    workspace = get_workspace_dir(os.path.abspath(args.workspace))
    if workspace is None:
        print(f"Error: Could not find a hatch workspace from '{args.workspace}'.")
        sys.exit(1)
    src_dir = os.path.join(workspace, "src")
    if not os.path.isdir(src_dir):
        print(f"Error: No 'src' directory found in workspace '{workspace}'.")
        sys.exit(1)
    return workspace, src_dir


def _col(label, width):
    """Colorize label in cyan and pad to width with spaces."""
    return clr(label, _CYAN) + ' ' * (width - len(label))


def list_packages_command(args):
    workspace, src_dir = _resolve_workspace(args)
    packages = find_packages(src_dir)
    if not packages:
        print("No packages found.")
        return

    name_w = max(max(len(name) for name, _ in packages), len("name"))
    path_w = max(max(len(p) for _, p in packages), len("path"))
    ansi_w = len(clr('', _CYAN))
    term_w = shutil.get_terminal_size().columns
    sep = clr("─" * min(name_w + 2 + path_w, term_w), _BRIGHT_MAGENTA)

    print(sep)
    print(f"{_col('name', name_w)}  {clr('path', _CYAN)}")
    print(sep)
    for name, rel_path in packages:
        print(f"{clr(name, _BRIGHT_BLUE):<{name_w + ansi_w}}  {clr(rel_path, _DIM)}")
    print(sep)


def list_repos_command(args):
    workspace, src_dir = _resolve_workspace(args)
    repos = find_repos(src_dir)
    if not repos:
        print("No repositories found.")
        return

    path_w = max(max(len(r["path"]) for r in repos), len("path"))
    type_w = max(max(len(r["type"]) for r in repos), len("type"))
    url_w = max(max(len(r.get("url", "")) for r in repos), len("url"))
    ver_w = max(max(len(r.get("version", "")) for r in repos), len("version"))
    ansi_w = len(clr('', _CYAN))
    term_w = shutil.get_terminal_size().columns
    sep = clr("─" * min(path_w + 2 + type_w + 2 + url_w + 2 + ver_w, term_w), _BRIGHT_MAGENTA)

    print(sep)
    print(f"{_col('path', path_w)}  {_col('type', type_w)}  {_col('url', url_w)}  {clr('version', _CYAN)}")
    print(sep)
    for repo in repos:
        path_col = clr(repo['path'], _BRIGHT_BLUE) + ' ' * (path_w - len(repo['path']))
        type_col = f"{repo['type']:<{type_w}}"
        url = repo.get('url', '')
        url_col = clr(url, _DIM) + ' ' * (url_w - len(url))
        version = repo.get('version', '')
        print(f"{path_col}  {type_col}  {url_col}  {version}".rstrip())
    print(sep)
