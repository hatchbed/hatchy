# Changelog

## [0.5.1]

### Changed
 - Improved output formatting.

## [0.5.0]

### Added
 - --generator argument to config command.
 - --compiler argument to config command.
 - --linker argument to config command.
 - --ccache argument to config command.
 - --compile-commands argument to config command.
 - --build-testing argument to config command.

### Changed
 - Wrap stderr output from package builds instead of truncating.
 - Mark packages that had stderr output during build in summary.

## [0.4.0]

### Added
 - --build-type argument to config command.

### Changed
 - Improved status overlay render during terminal resize.

## [0.3.0] - 2026-05-15

### Added
- Live per-package status overlay during build and test commands.
- Syntax highlighting for GCC/Clang diagnostics, CMake errors, and Python tracebacks in colcon stderr blocks.

### Changed
- Improved formatting for combined status bar overlay.
- Improved build and test terminal output formatting.
- Improved formatting for config display.

### Fixed
- Fixed package selection for test summary.

## [0.2.1] - 2026-04-09

### Fixed
- Fix not passing current environment to build command.

## [0.2.0] - 2026-03-29

### Added
- Implemented `hatch test` command
- `--verbose` / `-v` flag to show individual test case status
- `--results-only` / `-r` flag to display cached results without re-running tests
- Added CONTRIBUTING.md

### Changed
- Migrated package metadata from `setup.py` to `pyproject.toml`

## [0.1.0] - Initial Release

- Initial release
