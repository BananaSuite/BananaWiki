"""Pull and push BananaWiki content to an Obsidian vault."""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers._obsidian_sync import authenticate_obsidian_user, export_obsidian_vault, import_obsidian_vault


def build_parser():
    """Return the argument parser for the Obsidian sync CLI."""
    parser = argparse.ArgumentParser(description="Experimental BananaWiki ↔ Obsidian sync")
    parser.add_argument(
        "--vault",
        default=os.environ.get("BANANAWIKI_OBSIDIAN_VAULT"),
        help="Path to the local Obsidian vault directory",
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("BANANAWIKI_OBSIDIAN_USERNAME"),
        help="BananaWiki username (or BANANAWIKI_OBSIDIAN_USERNAME)",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("BANANAWIKI_OBSIDIAN_PASSWORD"),
        help="BananaWiki password (or BANANAWIKI_OBSIDIAN_PASSWORD)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("pull", "push"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument(
            "--page",
            action="append",
            default=[],
            help="Limit the sync to a specific page slug (repeatable)",
        )
        command_parser.add_argument(
            "--directory",
            action="append",
            default=[],
            help="Limit the sync to a vault/category directory such as Guides/Flask",
        )
        if command == "pull":
            command_parser.add_argument(
                "--skip-home",
                action="store_true",
                help="Do not export the BananaWiki home page",
            )
    return parser


def main(argv=None):
    """Run the Obsidian sync CLI and return an exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    missing = [
        name for name, value in (
            ("--vault", args.vault),
            ("--username", args.username),
            ("--password", args.password),
        ) if not value
    ]
    if missing:
        parser.error("Missing required options: " + ", ".join(missing))

    user = authenticate_obsidian_user(args.username, args.password)
    common_args = {"slugs": args.page, "category_paths": args.directory}
    if args.command == "pull":
        result = export_obsidian_vault(
            user,
            args.vault,
            include_home=not args.skip_home,
            **common_args,
        )
    else:
        result = import_obsidian_vault(user, args.vault, **common_args)

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
