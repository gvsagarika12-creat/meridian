"""Who can do what.

One table, read top to bottom, is the whole policy. Keeping it in a single literal
matters more than any cleverness: a permission system nobody can read in one sitting
is a permission system nobody audits, and an unaudited one drifts until everybody is
effectively an administrator again.

Two rules the code follows:

* **Deny by default.** `can()` returns False for an unknown permission, an unknown
  role, or no user at all. Adding a permission without granting it to anyone means
  nobody has it - which is the safe direction to fail.

* **The UI hides what the server refuses.** Templates ask `can()` to decide whether
  to draw a button, and routes ask `can()` again before acting. Hiding alone is
  decoration; enforcing alone is a UI full of buttons that error. Both, always.
"""

from __future__ import annotations

from .models import User, UserRole

# --- permissions ----------------------------------------------------------

FORMS_VIEW = "forms.view"
FORMS_CREATE = "forms.create"
FORMS_EDIT = "forms.edit"
FORMS_DELETE = "forms.delete"
CLIENTS_VIEW = "clients.view"
CLIENTS_EDIT = "clients.edit"
CLINICAL_EDIT = "clients.clinical"
SCHEDULE_EDIT = "schedule.edit"
SUBMISSIONS_VIEW = "submissions.view"
SUBMISSIONS_SEND = "submissions.send"
EVENTS_VIEW = "events.view"
USERS_MANAGE = "users.manage"
INTEGRATIONS_IMPORT = "integrations.import"
PRESCRIBE = "clinical.prescribe"
BILLING_EDIT = "billing.edit"
# Split out from CLIENTS_VIEW rather than folded into it: a role can see
# patients and the calendar without also seeing scanned insurance cards and
# referral letters, and the receptionist role is exactly that case.
DOCUMENTS_VIEW = "documents.view"

LABELS = {
    FORMS_VIEW: "See the form library",
    FORMS_CREATE: "Create new forms",
    FORMS_EDIT: "Edit form questions and consents",
    FORMS_DELETE: "Delete forms and questions",
    CLIENTS_VIEW: "See the client list",
    CLIENTS_EDIT: "Add and edit clients",
    CLINICAL_EDIT: "Record diagnoses and medications (doctors)",
    SCHEDULE_EDIT: "Book and change appointments",
    SUBMISSIONS_VIEW: "Read submitted forms",
    SUBMISSIONS_SEND: "Send a form to a patient",
    EVENTS_VIEW: "Read the audit log",
    USERS_MANAGE: "Manage staff accounts",
    INTEGRATIONS_IMPORT: "Import the patient list from IntakeQ",
    PRESCRIBE: "Write and sign prescriptions (prescribers only)",
    BILLING_EDIT: "Code and manage charges",
    DOCUMENTS_VIEW: "See and file patient documents",
}

# --- the policy -----------------------------------------------------------
ROLE_PERMISSIONS: dict[UserRole, set[str]] = {
    # Deliberately empty. Someone who signed up themselves has not yet been
    # vouched for by anyone, so they get a login and nothing behind it.
    UserRole.pending: set(),
    # A pure administrator here, not a prescriber - runs the system, does not
    # practise medicine, same split as UserRole.admin one step down. In a
    # practice where the owner also sees patients, add CLINICAL_EDIT and
    # PRESCRIBE back onto this line - nothing else needs to change.
    UserRole.owner: {
        FORMS_VIEW, FORMS_CREATE, FORMS_EDIT, FORMS_DELETE,
        CLIENTS_VIEW, CLIENTS_EDIT, SCHEDULE_EDIT,
        SUBMISSIONS_VIEW, SUBMISSIONS_SEND,
        EVENTS_VIEW, USERS_MANAGE, INTEGRATIONS_IMPORT,
        BILLING_EDIT, DOCUMENTS_VIEW,
    },
    # Runs the system, does not practise medicine. An administrator can reach
    # every screen and every account, and still cannot write a diagnosis - which
    # is the point: system power and prescribing authority are different things.
    UserRole.admin: {
        FORMS_VIEW, FORMS_CREATE, FORMS_EDIT, FORMS_DELETE,
        CLIENTS_VIEW, CLIENTS_EDIT, SCHEDULE_EDIT,
        SUBMISSIONS_VIEW, SUBMISSIONS_SEND,
        # Pulling the whole patient population across is an administrative act,
        # not a clinical one, and it is deliberately not on CLIENTS_EDIT - front
        # desk edits one patient at a time and has no business starting a job
        # that writes thousands of rows.
        EVENTS_VIEW, USERS_MANAGE, INTEGRATIONS_IMPORT,
        #  Billing yes, prescribing no. An administrator who can
        #  write a prescription is a prescriber, whatever the org
        #  chart says - and this one does not practise medicine.
        BILLING_EDIT, DOCUMENTS_VIEW,
    },
    # Clinicians build and read, but do not destroy. Deleting a form that has
    # submissions attached is an administrative act with records consequences.
    UserRole.practitioner: {
        FORMS_VIEW, FORMS_CREATE, FORMS_EDIT,
        CLIENTS_VIEW, CLIENTS_EDIT, CLINICAL_EDIT, SCHEDULE_EDIT,
        SUBMISSIONS_VIEW, SUBMISSIONS_SEND, PRESCRIBE, DOCUMENTS_VIEW,
    },
    # Receptionist: patients and the calendar, nothing else. Registering
    # somebody and booking them in is the whole job - forms, submissions,
    # billing and documents belong to other roles, not because a receptionist
    # is untrusted with them but because this app should only show a role the
    # screens its job actually needs.
    UserRole.front_desk: {
        CLIENTS_VIEW, CLIENTS_EDIT, SCHEDULE_EDIT,
    },
    # Compliance and audit. Sees everything, changes nothing - including the
    # audit log, which is the one thing they are usually here to read.
    UserRole.read_only: {
        FORMS_VIEW, CLIENTS_VIEW, SUBMISSIONS_VIEW, EVENTS_VIEW, DOCUMENTS_VIEW,
    },
}


#  What each role is called on screen. Separate from the stored value on
#  purpose: the database column is a Postgres enum, and renaming a value there
#  means an ALTER TYPE on every copy plus anything that has the old string
#  written down. A display name changes what people read without touching what
#  is stored, and the two are allowed to differ - the practice says "doctor",
#  the schema says "practitioner", and neither has to move for the other.
ROLE_LABEL = {
    "practitioner": "doctor",
    "front_desk": "Receptionist",
    "read_only": "read only",
}


def role_label(role) -> str:
    value = getattr(role, "value", role) or ""
    return ROLE_LABEL.get(value, value.replace("_", " "))


def can(user: User | None, permission: str) -> bool:
    """Deny by default: no user, unknown role, or unknown permission all fail."""
    if user is None or not user.is_active:
        return False
    return permission in ROLE_PERMISSIONS.get(user.role, set())


def permissions_for(role: UserRole) -> set[str]:
    return ROLE_PERMISSIONS.get(role, set())


def matrix() -> list[tuple[str, str, dict[str, bool]]]:
    """(permission, label, {role name: allowed}) - for the /permissions screen."""
    rows = []
    for perm, label in LABELS.items():
        rows.append((perm, label,
                     {r.value: perm in perms for r, perms in ROLE_PERMISSIONS.items()}))
    return rows
