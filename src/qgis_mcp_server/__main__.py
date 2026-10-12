"""Command-line entry point.

Validation runs before the server starts serving. ``spec/tasks.md`` Task 1 makes
that the acceptance condition: a wrong QGIS path must produce a clear diagnostic
at startup, not an obscure import error at the first tool call. A server that
accepts a connection and then fails every call is worse than one that refuses to
start, because the agent reads the failure as "QGIS cannot do this" rather than
"this server was never configured".
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from qgis_mcp_server.config import Config, ConfigError
from qgis_mcp_server.qgis_install import QgisNotFound, discover_installs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qgis-mcp",
        description="MCP server exposing QGIS over live and headless paths.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "validate the configuration and exit. Starts QGIS once to read "
            "back its real version and registered Processing providers."
        ),
    )
    parser.add_argument(
        "--list-installs",
        action="store_true",
        help="list the QGIS installs found in the default locations, and exit.",
    )
    parser.add_argument(
        "--qgis-root",
        type=Path,
        default=None,
        help="QGIS install directory. Overrides discovery and QGIS_MCP_QGIS_ROOT.",
    )
    parser.add_argument(
        "--project",
        type=Path,
        default=None,
        help="project file the headless path operates against (REQ-2).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to qgis-mcp.toml. Defaults to one in the working directory.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="headless execution timeout in seconds.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit --check output as JSON rather than text.",
    )
    return parser


def _print_report(report: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, indent=2, default=str))
        return

    print(f"QGIS install     {report['qgis_label']}")
    print(f"  root           {report['qgis_root']}")
    print(f"  prefix path    {report['prefix_path']}")
    print(f"  interpreter    {report['python_exe']}")
    print(f"  qgis_process   {report['qgis_process_exe']}")
    print(f"  bindings       {report['bindings_dir']}")
    if report.get("env_file"):
        print(f"  env file       {report['env_file']}")
    print(f"headless timeout {report['headless_timeout']:.0f}s")
    print(f"project          {report['project_path'] or '(none configured)'}")

    if "qgis_version" in report:
        print()
        print(f"reported version {report['qgis_version']}")
        print(f"  Python         {report['python_version']}")
        print(f"  Qt             {report['qt_version']}")
        print(f"  GDAL           {report['gdal_version']}")
        loaded = report.get("providers_loaded") or []
        print(f"  providers      {', '.join(loaded) or '(none)'}")
        # design.md §6.4: a provider that is present but not loaded is exactly
        # the case a hardcoded list misreports, so it is named rather than
        # folded into the line above.
        dormant = report.get("providers_present_not_loaded") or []
        if dormant:
            print(
                f"  not loaded     {', '.join(dormant)} "
                f"(algorithms from these are unavailable)"
            )

    for warning in report.get("warnings", []):
        print(f"\nwarning: {warning}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_installs:
        installs = discover_installs()
        if not installs:
            print("no usable QGIS install found.", file=sys.stderr)
            return 1
        for install in installs:
            hint = install.version_hint
            version = ".".join(str(part) for part in hint) if hint else "unknown"
            print(f"{version:<10} {install.root}")
        return 0

    try:
        config = Config.load(
            config_file=args.config,
            qgis_root=args.qgis_root,
            project_path=args.project,
            headless_timeout=args.timeout,
        )
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    # Validate before anything else, including before --check, so the two paths
    # cannot disagree about what counts as a usable install.
    try:
        report = config.validate(probe=args.check)
    except QgisNotFound as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        return 2

    if args.check:
        _print_report(report, as_json=args.json)
        return 0

    for warning in report.get("warnings", []):
        print(f"warning: {warning}", file=sys.stderr)

    from qgis_mcp_server.server import build_server

    server, context = build_server(config)
    try:
        server.run(transport="stdio")
    finally:
        context.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
