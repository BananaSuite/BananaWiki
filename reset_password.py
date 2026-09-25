#!/usr/bin/env python3
"""
BananaWiki: Admin password-reset tool

Intended to be run over SSH by the server admin:

    python reset_password.py

The tool lists all registered users, lets the admin pick one, then
prompts for (and confirms) a new password before saving it.
"""

import sys
import os
import getpass
import uuid

# Ensure the project root is on the path when the script is executed from
# a different working directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


_MIN_PW_LEN = 8
_MAX_PW_LEN = 1000


def _print_user_table(users):
    """Print a formatted table of users."""
    print(f"\n{'#':<4} {'ID':<6} {'Username':<24} {'Role':<10} {'Status'}")
    print("-" * 60)
    for idx, user in enumerate(users, start=1):
        status = "suspended" if user["suspended"] else "active"
        print(f"{idx:<4} {user['id']:<6} {user['username']:<24} {user['role']:<10} {status}")
    print()


def main():
    import db  # noqa: E402

    db.init_db()

    users = db.list_users()
    if not users:
        print("No users found in the database.")
        sys.exit(0)

    print("=== BananaWiki Password Reset Tool ===")
    _print_user_table(users)

    # user selection ---
    while True:
        raw = input("Enter the number of the user to reset (or 'q' to quit): ").strip()
        if raw.lower() in ("q", "quit", "exit"):
            print("Aborted.")
            sys.exit(0)
        try:
            choice = int(raw)
        except ValueError:
            print("Please enter a valid number.")
            continue
        if 1 <= choice <= len(users):
            selected = users[choice - 1]
            break
        print(f"Please enter a number between 1 and {len(users)}.")

    print(f"\nResetting password for: {selected['username']} (role: {selected['role']})")

    # new password ---
    while True:
        new_pw = getpass.getpass("New password: ")
        if len(new_pw) < _MIN_PW_LEN:
            print(f"Password must be at least {_MIN_PW_LEN} characters. Try again.")
            continue
        if len(new_pw) > _MAX_PW_LEN:
            print(f"Password cannot exceed {_MAX_PW_LEN} characters. Try again.")
            continue
        confirm_pw = getpass.getpass("Confirm password: ")
        if new_pw != confirm_pw:
            print("Passwords do not match. Try again.")
            continue
        break

    from helpers._passwords import generate_password_hash
    hashed = generate_password_hash(new_pw)
    update_fields = {"password": hashed}
    if db.get_site_settings()["session_limit_enabled"]:
        update_fields["session_token"] = uuid.uuid4().hex
    db.update_user(selected["id"], **update_fields)
    db.revoke_all_user_sessions(selected["id"])
    revoked = db.revoke_user_api_service_tokens(
        selected["id"], reason="password reset with reset_password.py"
    )

    print(f"\nPassword for '{selected['username']}' has been updated successfully.")
    if revoked:
        print(f"{revoked} API token(s) of this account were revoked.")


if __name__ == "__main__":
    main()
