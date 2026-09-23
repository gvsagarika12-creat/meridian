"""Command-line recovery tool. Run it when you cannot get in through the app.

    python admin.py list
    python admin.py set-password you@example.com
    python admin.py set-password you@example.com --password "newpassword"
    python admin.py unlock you@example.com
    python admin.py reset-accounts

Every staff account is reachable from here, which is the point: a locked-out owner
has no other way back in. That also means anyone who can open this folder can take
over the system - so on a real deployment the folder needs OS permissions, not just
the application's own login. On a single admin laptop this is the right trade; on a
shared server it is not.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from app import auth
from app.models import SessionLocal, User, UserRole, log


def cmd_list(args) -> None:
    db = SessionLocal()
    rows = db.query(User).order_by(User.id).all()
    if not rows:
        print("No accounts. Start the app and the setup screen will appear.")
        return
    print(f"{'id':>3}  {'email':32} {'role':13} {'active':7} {'password':9} last sign-in")
    print("-" * 92)
    for u in rows:
        print(f"{u.id:>3}  {u.email:32} {u.role.value:13} "
              f"{'yes' if u.is_active else 'no':7} "
              f"{'set' if u.password_hash else 'none':9} "
              f"{u.last_login.strftime('%Y-%m-%d %H:%M') if u.last_login else 'never'}")
    db.close()


def cmd_set_password(args) -> None:
    db = SessionLocal()
    user = db.query(User).filter(User.email == args.email.strip().lower()).first()
    if not user:
        print(f"No account for {args.email}.")
        print("Run 'python admin.py list' to see what exists.")
        sys.exit(1)

    password = args.password
    if not password:
        password = getpass.getpass("New password: ")
        again = getpass.getpass("Confirm      : ")
        if password != again:
            print("Passwords do not match.")
            sys.exit(1)

    if len(password) < auth.MIN_PASSWORD_LENGTH:
        print(f"Password must be at least {auth.MIN_PASSWORD_LENGTH} characters.")
        sys.exit(1)

    user.password_hash = auth.hash_password(password)
    user.must_change_password = args.force_change
    user.is_active = True
    auth.clear_failures(user.email)
    log(db, "Password Set From Command Line", "user", user.id, user_id=user.id)
    db.commit()
    db.close()

    print(f"\nPassword set for {args.email}.")
    if args.force_change:
        print("They will be asked to choose a new one at first sign-in.")
    print("\nStart the app and sign in.")


def cmd_unlock(args) -> None:
    """Lockouts live in the running process's memory, so this mostly means:
    restart the app. Kept because 'I am locked out' is the obvious thing to type."""
    auth.clear_failures(args.email)
    print(f"Cleared failed attempts for {args.email}.")
    print("Lockouts are held in memory, so closing and reopening the app clears them too.")


def cmd_reset_accounts(args) -> None:
    """Wipe staff accounts so the setup screen reappears. Forms and submissions stay."""
    db = SessionLocal()
    count = db.query(User).count()
    if not args.yes:
        print(f"This removes all {count} staff accounts and reopens the setup screen.")
        print("Forms, clients and submissions are NOT touched.")
        if input("Type 'yes' to continue: ").strip().lower() != "yes":
            print("Cancelled.")
            return
    db.query(User).delete()
    db.commit()
    db.close()
    print(f"Removed {count} accounts. Start the app and the setup screen will appear.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show every staff account").set_defaults(func=cmd_list)

    sp = sub.add_parser("set-password", help="set a password for one account")
    sp.add_argument("email")
    sp.add_argument("--password", default="", help="omit to be prompted (not shown on screen)")
    sp.add_argument("--force-change", action="store_true",
                    help="require them to choose a new one at first sign-in")
    sp.set_defaults(func=cmd_set_password)

    up = sub.add_parser("unlock", help="clear failed sign-in attempts")
    up.add_argument("email")
    up.set_defaults(func=cmd_unlock)

    rp = sub.add_parser("reset-accounts", help="remove all accounts, reopen setup")
    rp.add_argument("--yes", action="store_true", help="skip the confirmation")
    rp.set_defaults(func=cmd_reset_accounts)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
