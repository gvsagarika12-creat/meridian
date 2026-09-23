"""Staff account management.

The interesting part of this module is not creating users - it is the guardrails
that stop an administrator locking the practice out of its own system.

Four rules, each enforced server-side:

1. **You cannot deactivate yourself.** The classic way to lose access is to tidy
   up your own account.
2. **You cannot change your own role.** Demoting yourself out of `users.manage`
   is a one-way door - there would be nobody left able to put you back.
3. **The last account that can manage users cannot be removed or demoted.** If
   the action would leave zero active administrators, it is refused.
4. **Only an owner can create or promote to owner.** Otherwise an admin can
   escalate to owner, and the distinction means nothing.

Accounts are deactivated, never deleted. A deleted user leaves audit rows pointing
at a `user_id` that no longer resolves, which quietly ruins the log.
"""

from __future__ import annotations

import secrets

from sqlalchemy.orm import Session

from . import permissions as perms
from .models import User, UserRole


class Refused(Exception):
    """A guardrail said no. The message is shown to the user as-is."""


def temp_password() -> str:
    return secrets.token_urlsafe(12)


def _managers(db: Session) -> list[User]:
    """Active accounts whose role can manage users."""
    return [u for u in db.query(User).filter_by(is_active=True).all()
            if perms.can(u, perms.USERS_MANAGE)]


def _would_orphan(db: Session, target: User) -> bool:
    """True if removing this account's rights leaves nobody able to manage users."""
    managers = _managers(db)
    return len(managers) <= 1 and any(m.id == target.id for m in managers)


def check_create(actor: User, role: UserRole) -> None:
    if role == UserRole.owner and actor.role != UserRole.owner:
        raise Refused("Only an owner can create another owner.")


def check_role_change(db: Session, actor: User, target: User, new_role: UserRole) -> None:
    if actor.id == target.id:
        raise Refused("You cannot change your own role. Ask another administrator.")
    if new_role == UserRole.owner and actor.role != UserRole.owner:
        raise Refused("Only an owner can promote someone to owner.")
    if target.role == UserRole.owner and actor.role != UserRole.owner:
        raise Refused("Only an owner can change an owner's role.")
    if perms.USERS_MANAGE not in perms.permissions_for(new_role) and _would_orphan(db, target):
        raise Refused("This is the last account that can manage users. "
                      "Promote someone else first.")


def check_deactivate(db: Session, actor: User, target: User) -> None:
    if actor.id == target.id:
        raise Refused("You cannot deactivate your own account.")
    if target.role == UserRole.owner and actor.role != UserRole.owner:
        raise Refused("Only an owner can deactivate an owner.")
    if _would_orphan(db, target):
        raise Refused("This is the last account that can manage users. "
                      "Promote someone else first.")


def check_reset(actor: User, target: User) -> None:
    if target.role == UserRole.owner and actor.role != UserRole.owner:
        raise Refused("Only an owner can reset an owner's password.")


def normalise_email(raw: str) -> str:
    return (raw or "").strip().lower()
