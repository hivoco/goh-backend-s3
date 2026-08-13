"""Create or reset a panel login from the command line.

Normally the super-admin manages accounts from the panel's Admins page. This
script is the way back in when that isn't possible — a forgotten super-admin
password, or a first deploy where you'd rather not paste a bcrypt hash into .env.

    cd backend

    # create a super-admin (prompts for the password, twice)
    PYTHONPATH=. .venv/bin/python scripts/create_admin.py \
        --username super-admin --role superadmin

    # reset an existing account's password
    PYTHONPATH=. .venv/bin/python scripts/create_admin.py \
        --username someone --reset

    # list who exists
    PYTHONPATH=. .venv/bin/python scripts/create_admin.py --list

The password is never taken as an argument — that would put it in your shell
history and in `ps` output. It's prompted for, hidden.
"""

import argparse
import getpass
import os
import sys

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(BACKEND_DIR, ".env")


def load_env() -> None:
    """Load .env into the environment, exactly like the API does."""
    if not os.path.isfile(ENV_PATH):
        print(f"✗ No .env found at {ENV_PATH}\n"
              "  Copy .env.example to .env and fill in DATABASE_URL first.")
        raise SystemExit(1)
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def prompt_password(admin_service) -> str:
    from app.services.admin_service import PasswordPolicyError
    while True:
        first = getpass.getpass("New password: ")
        second = getpass.getpass("Confirm password: ")
        if first != second:
            print("  ✗ Passwords don't match, try again.\n")
            continue
        try:
            admin_service.validate_password(first)
        except PasswordPolicyError as e:
            print(f"  ✗ {e}\n")
            continue
        return first


def main() -> int:
    # Parse args BEFORE touching app config, so --help works without a .env.
    parser = argparse.ArgumentParser(description="Create or reset a Grains of Hope admin login.")
    parser.add_argument("--username", help="Account username (case-insensitive)")
    parser.add_argument("--role", choices=["admin", "superadmin"], default="admin",
                        help="Role to create with (ignored on --reset). Default: admin")
    parser.add_argument("--reset", action="store_true",
                        help="Reset the password of an existing account instead of creating one")
    parser.add_argument("--list", action="store_true", help="List all accounts and exit")
    args = parser.parse_args()

    if not args.list and not args.username:
        parser.error("--username is required (or use --list)")

    load_env()

    try:
        from app.core.database import SessionLocal
        from app.services import admin_service
        from app.services.admin_service import PasswordPolicyError
    except Exception as e:  # missing/invalid .env values surface as a pydantic error
        print(f"✗ Couldn't load the app config: {e}")
        return 1

    db = SessionLocal()
    try:
        if args.list:
            rows = admin_service.list_admins(db)
            if not rows:
                print("No admin accounts yet.")
                return 0
            print(f"{'ID':<5}{'USERNAME':<24}{'ROLE':<13}{'ACTIVE':<8}LAST LOGIN")
            for a in rows:
                last = a.last_login_at.strftime("%Y-%m-%d %H:%M") if a.last_login_at else "—"
                print(f"{a.id:<5}{a.username:<24}{a.role:<13}{'yes' if a.is_active else 'no':<8}{last}")
            return 0

        existing = admin_service.get_by_username(db, args.username)

        if args.reset:
            if not existing:
                print(f"✗ No account named '{args.username}'. Drop --reset to create it.")
                return 1
            admin_service.set_password(db, existing, prompt_password(admin_service))
            print(f"✓ Password reset for '{existing.username}' ({existing.role}). "
                  "Their other sessions are now signed out.")
            return 0

        if existing:
            print(f"✗ '{existing.username}' already exists ({existing.role}). "
                  "Use --reset to change its password.")
            return 1

        admin = admin_service.create_admin(
            db, args.username, prompt_password(admin_service), args.role, created_by="cli",
        )
        print(f"✓ Created {admin.role} '{admin.username}' (id={admin.id}).")
        return 0
    except (PasswordPolicyError, ValueError) as e:
        print(f"✗ {e}")
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
