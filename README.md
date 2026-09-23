# Meridian Behavioral Health

Meridian — the practice's own intake and screening platform. Staff run it as a desktop
application; patients fill forms in a browser on their phone.

## Run it

```bash
pip install -r requirements-desktop.txt
python desktop.py
```

`requirements.txt` is the server on its own; `requirements-desktop.txt` adds the
native window on top of it. A machine that only serves the app installs the first.

A native window opens. No browser, no URL bar. If `pywebview` is missing it falls
back to your default browser and tells you so.

The database seeds itself on first launch with the practice's real form and folder names,
read off the IntakeQ screenshots. Client names are invented — **no real patient
data is in this repo.**

## The hospital archive

`data/hospital data analysis.csv` is the practice's own record of past visits —
the Tebra stand-in. Load or reload it with:

```bash
python import_hospital_data.py
```

A patient is linked to it by the **hospital's patient number**, entered on Add
Patient or on their chart. That number is what lets the Records screen compare
what the patient reported against what the archive holds — the **Match** column.

See `data/README.md` for the expected columns.

## What's here

| Screen | Route | Notes |
|---|---|---|
| Sign in | `/login` | Cosmetic for now — no auth enforced yet |
| Dashboard | `/` | Three action tiles, forms received, pending, account events |
| My Form Templates | `/forms` | Card grid with colour stripes, folders, Create New |
| Form editor | `/forms/{id}` | Numbered question list, add / reorder / delete, consent attachment |
| Clients | `/clients` | Demo records |
| Send form | `/send` | Generates a token; delivery not wired up |
| Submissions | `/submissions` | Sent and received forms |
| Account events | `/events` | The audit log |
| Patient form | `/preview/{id}` | practice branding, progress bar, page N of M, `[ClientFirstName]` |

## Layout

```
desktop.py          launches the staff UI in a native window
app/models.py       schema + the append-only audit log
app/seed.py         the practice's real form/folder/consent names
app/main.py         all routes
app/templates/      Jinja templates, staff and patient
app/static/app.css  the whole stylesheet
intake.db           SQLite, created on first run (gitignored)
```

## Design decisions worth knowing

**The audit log is the activity feed.** `AuditEvent` is append-only — nothing in this
codebase updates or deletes a row — and it's also what renders as "Latest Account
Events" on the dashboard. Same table for compliance and for staff. It stays honest
because people actually look at it.

**Consent forms are separate objects.** A questionnaire has many consent forms and a
consent form belongs to many questionnaires, joined through `FormConsent`. That's how
IntakeQ works and it's why "Adult Packet W/Consent" and "Consent Forms" are different
rows rather than one form with a checkbox.

**Forms are versioned.** `Form.version` exists and `Submission.form_version` pins to
it, so editing a live form never changes what a past patient saw. The bump-on-edit
logic isn't wired yet — see below.

**Single practice.** No `practice_id` on any table, no tenancy, no billing, no signup.
Adding tenancy later is a schema migration, not a bolt-on.

## The patient flow

`/send` issues an opaque 256-bit token with a 14-day expiry and lands on the
submission page, where staff can copy the link.

```
GET  /f/{token}           intro, then one page at a time; resumes where they left off
POST /f/{token}/save      upserts that page's answers, moves next or back
POST /f/{token}/submit    validates server-side, signs consents, closes the link
GET  /f/{token}/done      confirmation
```

**Tokens are database rows, not signed payloads.** A signed token is stateless and
therefore cannot be revoked once issued. A random token looked up in the database can
be cancelled the moment a link reaches the wrong person - for PHI that is the property
that matters.

**Validation runs on the server.** The browser marks required fields too, but that is
a convenience. `missing_required()` re-checks every required question at submit time,
so a skipped or hidden field cannot get through.

**Signatures are evidence bundles, not images.** Each `ConsentSignature` stores the
name as typed, timestamp, IP, user agent, and a SHA-256 of the exact document text.
Without the hash you cannot later prove what the wording said when it was signed.

**A matrix is N questions sharing one option set.** `Question.rows_raw` holds the row
labels; the answer is stored as a JSON object keyed by row label - `{"Anxiety": "3 - Marked"}` -
rather than by index, so rows can be reordered or reworded later without corrupting
existing submissions. A required matrix needs *every* row rated, because a symptom
checklist with four of nine rows filled in is not an answered question.

**A mixed-controls block is one question holding many fields.** "Please enter your
information." is one numbered question but eighteen fields. The answer stores as a
JSON object keyed by item **label** - not index - so items can be reordered or added
without corrupting submissions already taken. Required is checked per item, so the
error names the field rather than the block.

**After submit the link stops accepting writes.** Re-opening shows "already submitted";
a POST to `/save` changes nothing.

## Authentication

Deny by default. `auth_middleware` checks every request against an allow-list -
`/login`, `/logout`, `/static/`, `/f/` - and redirects everything else to the sign-in
page. A new staff route is protected the moment it exists; there is no decorator to
forget.

| Control | Setting |
|---|---|
| Password storage | `hashlib.scrypt`, per-user random salt, n=2^14 |
| Comparison | `hmac.compare_digest` (constant time) |
| Session | Signed cookie, secret in `config/session.key` (gitignored) |
| Idle timeout | 20 minutes, sliding |
| Lockout | 5 failed attempts, 15 minutes |
| Minimum length | 10 characters (`auth.MIN_PASSWORD_LENGTH`) |

**First run generates a random owner password** and writes it to
`first-run-credentials.txt`. A shipped default password survives into production
because nobody remembers it is there; a generated one has to be read off disk, which
means somebody looked. Sign in, change it on `/account`, delete the file.

**Failed logins say the same thing whether the account exists or not**, so the form
cannot be used to discover who has an account.

**Patient links stay public and must.** Patients have no account - `/f/{token}` is
protected by the token's 256 bits of entropy and its expiry, not by a session.

## Staff accounts

`/users`, gated by `users.manage` (owner and admin only).

Create an account, change someone's role, deactivate or reactivate, reset a password.
Temporary passwords are generated, shown **once**, and force a change at first sign-in -
until then that account can reach `/account` and nothing else, because a password an
administrator has read is a password somebody else has seen.

**Accounts are deactivated, never deleted.** A deleted user leaves audit rows pointing
at a `user_id` that no longer resolves, which quietly ruins the log.

### Guardrails

Four rules stop an administrator locking the practice out of its own system:

| Attempt | Result |
|---|---|
| Deactivate your own account | Refused |
| Change your own role | Refused - it is a one-way door |
| Promote yourself to owner | Refused |
| Create or deactivate an owner, as an admin | Refused - only an owner can |
| Remove the last account that can manage users | Refused |

All enforced server-side in `users.py`, not by hiding buttons.

## Email and SMS

`/send` offers email, SMS, both, or neither. `/outbox` is the delivery log, and each
submission page shows its own history with resend buttons.

### What a message may say

Your form library contains **Edinburgh Scale** (a postnatal depression screen), **MDQ**
(bipolar screening) and **Spravato Consent** (treatment-resistant depression). A text
reading "complete your Edinburgh Scale" discloses a diagnosis to anyone who glances at
the phone, and email and SMS are unencrypted in transit and at rest on the device.

So the body names **no form and no condition**:

```
Hello Maria,

<Practice> has sent you a form to complete before your appointment.

https://.../f/<token>
This link expires on 01 October 2026.

Please do not forward this link - it opens your form.
If you were not expecting this, please call us on (909) 312-3300.
```

`MESSAGING_INCLUDE_FORM_NAME=1` overrides this. It is off by default, and the send
screen warns in amber when it is on.

### Consent is required per channel

`Client.allow_email` and `Client.allow_sms` both default to **False**. Absent consent
is not implied consent. A send without it is recorded as `skipped` with the reason -
"we could not text them because they never consented" has to be answerable from the
log months later, just as much as "we texted them".

### Backends

| Setting | Effect |
|---|---|
| unset / `file` | Writes to `outbox.log`, transmits nothing. The default. |
| `EMAIL_BACKEND=smtp` | Any SMTP relay - SES, SendGrid, Mailgun |
| `SMS_BACKEND=twilio` | Twilio REST API |

Sending is opt-in so a development machine cannot be one misconfiguration away from
texting a real patient. **Every provider carrying PHI needs a BAA with you**, and SMS
also needs 10DLC registration, which takes weeks - start it early.

`MessageLog` records channel, recipient, status and reason. **It never records the
link**, which is a credential that opens a patient's form, not a reference.

## Getting in

The sign-in screen has three doors.

**Forgot your password?** Emails a reset link. Stored, single-use, expires in 2 hours.
The form answers identically whether the account exists or not, so it cannot be used
to find out who works here. Also works for an account that has never had a password.

**First time here? Create an account.** Open sign-up. The account is real and signs in
immediately - but it is created with the `pending` role, which carries **no
permissions at all**. Every route redirects to one screen explaining that an
administrator has to give them a role first.

That separation is the point: *anyone may create a login* and *anyone may read patient
records* are different statements, and only the first is true here.

**Ask an administrator.** Records a request without creating anything. For people who
would rather not choose a password up front.

Approvals happen on **Staff**, where two panels appear: accounts awaiting approval
(self-registered) and access requests. Giving someone a role is one dropdown.

## Not built yet

- **Version bump on edit.** The column exists and submissions pin to it; editing a
  form doesn't increment it.
- **Conditional logic and scoring.** Question types render; branching and PHQ-9 /
  GAD-7 totals are not implemented.
- **PDF render of a submission.**
- **Folder assignment.** Folders exist and count; forms can't be moved into them.

## Related

`../ipmg-prescreening/harvest_forms.py` reconstructs form definitions from completed
IntakeQ submissions. Run it when the API key is available and it will give you draft
YAML for the ~50 real forms, which is how this database gets populated for real.
