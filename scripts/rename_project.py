#!/usr/bin/env python3
"""
Script to tactically rename BananaWiki to bWiki or vice versa in the project.
"""

import os
import sys
import argparse


def replace_in_file(file_path, old_str, new_str, dry_run=False):
    """Replace occurrences of old_str with new_str in a file.

    Returns True if any replacement was made, False otherwise.
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except UnicodeDecodeError:
        # Skip binary files
        return False
    except Exception as e:
        print(f"Error reading {file_path}: {e}", file=sys.stderr)
        return False

    if old_str not in content:
        return False

    new_content = content.replace(old_str, new_str)

    if dry_run:
        print(f"Would change: {file_path}")
        return True
    else:
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            print(f"Changed: {file_path}")
            return True
        except Exception as e:
            print(f"Error writing {file_path}: {e}", file=sys.stderr)
            return False


def main():
    parser = argparse.ArgumentParser(
        description='Rename BananaWiki to bWiki or vice versa in the project.'
    )
    parser.add_argument(
        'direction',
        choices=['to_bwiki', 'to_bananawiki'],
        help='Direction of rename'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Only show what would be changed'
    )
    args = parser.parse_args()

    if args.direction == 'to_bwiki':
        old_str = "BananaWiki"
        new_str = "bWiki"
    else:  # to_bananawiki
        old_str = "bWiki"
        new_str = "BananaWiki"

    root_dir = os.getcwd()
    # Directories to skip entirely
    skip_dirs = {
        '.git', '__pycache__', '.venv', '.venv_py13', '.pytest_cache',
        'logs', 'instance', 'site', 'package', 'translations', 'hosting'
    }

    changed_files = 0
    for dirpath, dirnames, filenames in os.walk(root_dir):
        # Modify dirnames in-place to skip directories
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for filename in filenames:
            file_path = os.path.join(dirpath, filename)
            if replace_in_file(file_path, old_str, new_str, args.dry_run):
                changed_files += 1

    if args.dry_run:
        print(f"\nDry run: {changed_files} files would be changed.")
    else:
        print(f"\nChanged {changed_files} files.")


if __name__ == '__main__':
    main()
