"""Create a login user from the CLI: ``python -m expense_analyzer.create_user``.

Bootstraps the first user (there is no public registration — a household app).
Additional users can then be added from the logged-in Users page.

    python -m expense_analyzer.create_user --username pawel --name "Paweł"
"""

import argparse
import getpass
import sys

from sqlmodel import Session

from expense_analyzer.db import get_engine
from expense_analyzer.queries import users


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an Expense Analyzer login user.")
    parser.add_argument("--username", required=True, help="login handle (unique)")
    parser.add_argument("--name", help="display name (defaults to the username)")
    args = parser.parse_args()

    password = getpass.getpass("Password: ")
    if not password:
        sys.exit("aborted: empty password")
    if password != getpass.getpass("Confirm password: "):
        sys.exit("aborted: passwords do not match")

    with Session(get_engine()) as session:
        if users.get_by_username(session, args.username.strip()) is not None:
            sys.exit(f"error: username {args.username.strip()!r} already exists")
        user = users.create_user(
            session,
            username=args.username,
            name=args.name or args.username,
            password=password,
        )

    role = "admin" if user.is_admin else "member"
    print(f"Created user {user.username!r} (id={user.id}, {role}).")


if __name__ == "__main__":
    main()
