"""Fill every empty ConsentForm.body with a labeled draft.

    python fill_draft_consents.py
    TARGET_DATABASE_URL=... python fill_draft_consents.py

Every document is marked, top and bottom, as an unreviewed draft standing in
for real attorney-approved text. That marking is not decoration - it is the
difference between "the mechanism has something to show" and "a real legal
document went live with nobody having read it first."

This is a content change, not a schema one - nothing here alters a table, so
it is not one of the add_*.py migration scripts. It is committed anyway,
because the alternative is the only record of what these 20 documents say
living in two databases and nowhere a diff could ever show what changed or
why. Safe to run twice: it only ever fills a document that is still empty,
and never overwrites real wording once somebody has written it through the
app's own /consents screen.

Kept in the repository as a record of what was written, not as a tool meant
for repeated use - the real workflow for changing a consent's wording from
here on is the /consents screen in the app itself, which every existing
signature's stored hash is unaffected by, exactly as this script's writes
were.
"""

from __future__ import annotations

import os
import sys

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.models import ConsentForm, log                             # noqa: E402

_target = os.environ.get("TARGET_DATABASE_URL", "").strip()
if _target:
    _url = (_target.replace("postgres://", "postgresql+psycopg://", 1)
                   .replace("postgresql://", "postgresql+psycopg://", 1))
    SessionLocal = sessionmaker(bind=sa.create_engine(_url), expire_on_commit=False)
else:
    from app.models import SessionLocal                              # noqa: E402

PRACTICE = "Meridian Behavioral Health"
ADDRESS = "1200 Meridian Avenue, Suite 300, Redlands, CA 92375"
PHONE = "(909) 312-3300"

TOP = (
    "[DRAFT — NOT YET REVIEWED BY LEGAL COUNSEL. This is placeholder text so "
    "the consent mechanism can be exercised; it must be replaced with "
    "attorney-approved wording before any real patient signs it.]\n\n"
)
BOTTOM = (
    "\n\n[END OF DRAFT. Replace this entire document with reviewed wording "
    "before use with a real patient. Until then this text exists only to "
    "demonstrate the signing flow.]"
)

BODIES = {

"Acknowledgment of Privacy Practice": f"""NOTICE OF PRIVACY PRACTICES — ACKNOWLEDGMENT OF RECEIPT

{PRACTICE} is required by law to maintain the privacy of your protected health
information ("PHI"), to provide you with a Notice of Privacy Practices
describing our legal duties and privacy practices, and to notify you following
a breach of unsecured PHI.

Our Notice of Privacy Practices describes how we may use and disclose your PHI
for treatment, payment, and healthcare operations, and the circumstances in
which we may share your information without your written authorization,
including for public health, legal, and safety reasons as permitted by law.
It also describes your rights, which include the right to:

  - Request restrictions on certain uses and disclosures of your PHI
  - Receive confidential communications by an alternative means or location
  - Inspect and obtain a copy of your PHI
  - Request an amendment to your PHI
  - Receive an accounting of certain disclosures
  - Receive a paper copy of this notice on request
  - File a complaint with us or with the Secretary of Health and Human
    Services if you believe your privacy rights have been violated, without
    retaliation

By signing below, you acknowledge that you have been given the opportunity to
review our current Notice of Privacy Practices. Signing this acknowledgment
does not waive any of your rights under that notice.

Questions about our privacy practices may be directed to our Privacy Officer
at {PHONE}.""",

"Consumer Notice of Rights and Responsibilities": f"""PATIENT RIGHTS AND RESPONSIBILITIES

As a patient of {PRACTICE}, you have the right to:

  - Considerate, respectful care without discrimination
  - Be informed about your diagnosis, treatment options, and prognosis in
    terms you can understand
  - Participate in decisions about your care and to refuse treatment to the
    extent permitted by law
  - Confidentiality of your health information, as described in our Notice
    of Privacy Practices
  - Access your medical records and request corrections
  - Know the names and roles of the people providing your care
  - Receive information about our fees and billing practices
  - Voice a complaint or grievance without fear of reprisal, and to be told
    how to do so

As a patient, you are responsible for:

  - Providing accurate and complete information about your health, medications,
    and history
  - Asking questions when you do not understand your care or instructions
  - Following the treatment plan you have agreed to, or discussing concerns
    about it with your provider
  - Keeping scheduled appointments or providing timely notice when you cannot
  - Meeting the financial obligations of your care
  - Treating staff and other patients with courtesy and respect""",

"FAMILY COURT/MEDIATION/HEALTH ASSESSMENT": f"""CONSENT FOR EVALUATION IN CONNECTION WITH A LEGAL OR FAMILY COURT MATTER

I understand that the evaluation or services being provided by {PRACTICE} are,
in whole or in part, requested in connection with a family court, mediation,
custody, or similar legal proceeding.

I understand that:

  - The usual limits on confidentiality in a therapeutic relationship may not
    apply. Records, findings, or testimony from this evaluation may be
    provided to the court, to attorneys involved in the matter, or to other
    parties as ordered or permitted by law.
  - This evaluation does not guarantee any particular outcome, finding, or
    recommendation, and the provider's role is to give an honest professional
    assessment, not to advocate for either party.
  - Fees for record preparation, deposition, or court testimony related to
    this matter are separate from ordinary clinical fees and are described in
    our Professional Service & Documentation Fees disclosure.
  - I may be asked questions whose answers become part of a record reviewed
    by people other than my treating provider.

By signing, I consent to this evaluation being conducted and understood on
these terms.""",

"Financial Policy": f"""FINANCIAL POLICY

Thank you for choosing {PRACTICE}. The following describes our financial
policy so there are no surprises about your responsibility for payment.

  - Payment for the estimated patient-responsible portion (copay, deductible,
    or self-pay fee) is due at the time services are provided.
  - As a courtesy, we will submit claims to your insurance carrier when we
    have your current insurance information. Billing your insurance is not a
    guarantee of payment; you remain responsible for any balance your plan
    does not pay, including deductibles, coinsurance, and non-covered
    services.
  - It is your responsibility to know your plan's benefits, whether we are an
    in-network provider, and whether a referral or authorization is required.
  - Appointments cancelled with less than 24 hours' notice, or missed without
    notice, may be subject to a late-cancellation or no-show fee, which is not
    billable to insurance.
  - Accounts with an outstanding balance may be sent to a collections agency
    after {PRACTICE} has made reasonable attempts to contact you, and may be
    reported to consumer credit agencies as permitted by law.
  - Returned payments (declined card, bounced check) may incur an additional
    fee.

Questions about a bill or this policy can be directed to our billing office
at {PHONE}.""",

"Genetic Testing MTHFR Gene": f"""CONSENT FOR MTHFR GENETIC TESTING

I understand that my provider has recommended testing for variants in the
MTHFR (methylenetetrahydrofolate reductase) gene, which affects how the body
processes folate and may be relevant to medication selection or dosing.

I understand that:

  - This test is voluntary. I may decline it and continue to receive care.
  - A specimen (typically saliva or blood) will be collected and sent to a
    laboratory for analysis.
  - Results may show a variant of uncertain significance, and a normal result
    does not rule out other genetic or metabolic factors relevant to my care.
  - Genetic information is protected under the Genetic Information
    Nondiscrimination Act (GINA) and applicable state law, which restrict its
    use by health insurers and employers, though GINA does not cover life,
    disability, or long-term care insurance.
  - Results will be entered into my medical record and may be shared with
    other treating providers for continuity of care, consistent with our
    Notice of Privacy Practices.
  - I may ask questions about this test before deciding whether to proceed,
    and I may revoke this consent prior to specimen collection.

By signing, I consent to this testing and acknowledge that its purpose and
limitations have been explained to me.""",

"IPMG HCCF": f"""CONSENT TO HEALTH CARE

I voluntarily consent to evaluation and treatment by {PRACTICE} and its
clinicians. I understand that:

  - Psychiatric and behavioral health treatment can include evaluation,
    psychotherapy, medication management, and other services as clinically
    indicated.
  - No guarantee has been made about the outcome of any evaluation or
    treatment.
  - I have the right to ask questions about any recommended treatment,
    including its risks, benefits, and alternatives, including the
    alternative of no treatment.
  - I may withdraw this consent and stop treatment at any time, except where
    doing so would conflict with a legal requirement (such as an involuntary
    hold ordered under applicable law).
  - In an emergency, treating clinicians may take reasonable action to protect
    my safety or the safety of others, consistent with law and professional
    ethics, even absent my specific consent in the moment.

By signing below, I confirm that I have had the opportunity to ask questions
and that I consent to care on these terms.""",

"IPMG Release of Information": f"""AUTHORIZATION FOR RELEASE OF HEALTH INFORMATION

I authorize {PRACTICE} to disclose, and/or to obtain from the party named
below, the health information described below.

  Name of individual/organization to disclose to or obtain from: ______________
  Information to be released/obtained (e.g., visit summary, diagnosis,
  medication list, full record): ______________
  Purpose of this disclosure: ______________
  This authorization expires: ______________ (or, if left blank, one year
  from the date signed, or upon revocation, whichever is earlier)

I understand that:

  - I may revoke this authorization in writing at any time, except to the
    extent that {PRACTICE} has already acted in reliance on it.
  - Treatment, payment, and enrollment in a health plan cannot be conditioned
    on my signing this authorization, except as permitted by law.
  - Information disclosed under this authorization may be subject to
    re-disclosure by the recipient and, once received by them, may no longer
    be protected by federal privacy regulations.

By signing, I authorize the release or receipt of the information described
above.""",

"IPMG Social Media Release Form": f"""SOCIAL MEDIA AND MARKETING RELEASE

I voluntarily consent to {PRACTICE} using my name, likeness, photograph,
video, and/or a written or verbal testimonial I provide, in the following
ways: on the practice's website, printed materials, and social media accounts,
for the purpose of promoting the practice's services.

I understand that:

  - Participation is entirely voluntary and has no effect on my care.
  - No clinical detail, diagnosis, or treatment information will be included
    without my separate, specific written authorization.
  - I will not receive payment or other compensation for this use.
  - I may withdraw this consent at any time by notifying {PRACTICE} in
    writing, though material already published before my withdrawal may not
    be able to be fully recalled (for example, printed materials already
    distributed).

By signing, I consent to the use described above.""",

"Locations": f"""SERVICE LOCATIONS

This is an informational notice, not a consent to be signed — it is included
here because a location list was part of the source paperwork this list was
built from.

{PRACTICE} provides in-person services at:

  {ADDRESS}

Telehealth appointments, where clinically appropriate and consented to
separately (see our Telemedicine consent), are also available.

For directions, parking, or accessibility information, please contact us at
{PHONE}.""",

"Mental Health Disclosure Form": f"""MENTAL HEALTH RECORDS DISCLOSURE ACKNOWLEDGMENT

I understand that records of mental health treatment, including psychotherapy
notes where applicable, may receive a higher level of confidentiality
protection under state and federal law than general medical records.

I understand that:

  - My diagnosis and treatment plan will be discussed with me directly, and I
    may request a written summary.
  - Psychotherapy process notes, if kept separately from my medical record,
    generally require my specific additional authorization before they can be
    released, even to other treating providers.
  - Limits on confidentiality still apply in situations required by law, such
    as a credible threat of serious harm to myself or an identifiable other,
    suspected abuse of a child, elder, or dependent adult, or a valid court
    order.
  - I may ask my provider at any time what information has been recorded and
    who it has been shared with.

By signing, I acknowledge that this has been explained to me.""",

"PROFESSIONAL SERVICE & DOCUMENTATION FEES & DISCLAIMER": f"""PROFESSIONAL SERVICE AND DOCUMENTATION FEES

Certain services provided by {PRACTICE} are administrative in nature and are
not billable to insurance. These may include, without limitation:

  - Completion of disability, FMLA, work, or school forms
  - Letters of support or medical necessity
  - Copies of records requested for purposes other than continuing your own
    care
  - Missed-appointment and late-cancellation fees (see our Financial Policy)

A fee schedule for these services is available on request from our front
office. Fees for documentation are due at the time the request is made and
are the patient's responsibility regardless of insurance coverage, since
insurance plans generally do not reimburse for non-clinical paperwork.

By signing, I acknowledge that I have been informed such fees may apply.""",

"Research Release Form": f"""CONSENT TO BE CONTACTED ABOUT RESEARCH PARTICIPATION

I consent to {PRACTICE} reviewing my clinical information to determine whether
I may be a candidate for a clinical trial or research study the practice is
participating in, and to being contacted about such opportunities.

I understand that:

  - Being identified as a possible candidate is not an enrollment in any
    study, and I am free to decline without affecting my ongoing care.
  - If I am eligible and choose to participate, I will be given a separate,
    study-specific informed consent document, reviewed and approved by an
    Institutional Review Board (IRB), describing that study's purpose,
    procedures, risks, and benefits before I agree to take part.
  - Participation in any study is voluntary, and I may withdraw at any time
    without penalty or effect on my regular care.

By signing, I consent only to being considered and contacted as described
above.""",

"SP - Acknowledgment of Privacy Practice": f"""NOTICE OF PRIVACY PRACTICES — ACKNOWLEDGMENT OF RECEIPT (SP)

[Note: this document's title carries an "SP -" prefix whose meaning was not
specified in the source data — possibly a site, service line, or language
designation. This draft mirrors the standard Acknowledgment of Privacy
Practice; confirm with the practice whether this should be a distinct
document or consolidated with "Acknowledgment of Privacy Practice."]

{PRACTICE} is required by law to maintain the privacy of your protected health
information and to provide you with a Notice of Privacy Practices describing
our legal duties, our privacy practices, and your rights, including the right
to request restrictions on certain disclosures, to inspect and copy your
records, to request amendments, to receive an accounting of certain
disclosures, and to file a complaint without retaliation.

By signing below, you acknowledge that you have been given the opportunity to
review our current Notice of Privacy Practices.""",

"SP - Consent to Treat Children - Copy": f"""CONSENT TO TREAT A MINOR

I am the parent or legal guardian of the minor named on this form, or I am a
minor authorized under applicable state law to consent to my own outpatient
mental health treatment.

I understand that:

  - Consent to treatment for a minor is generally given by a parent or legal
    guardian with legal authority to make healthcare decisions for the child.
  - Some states (including California, for outpatient mental health services,
    under certain conditions) allow a minor above a specified age to consent
    to their own treatment without parental consent; whether that applies here
    should be confirmed with the practice before relying on it.
  - Where a custody order limits or shares decision-making authority, a copy
    of the relevant order may be requested.
  - Information about a minor's treatment may be shared with a
    non-consenting parent only to the extent permitted by law and by any
    applicable custody order.

By signing, I confirm my legal authority to consent to this minor's treatment
and I consent to their evaluation and care as described in our Consent to
Health Care document.""",

"Spravato Consent": f"""CONSENT FOR SPRAVATO (ESKETAMINE) TREATMENT

I consent to treatment with Spravato (esketamine) nasal spray as recommended
by my provider.

I understand that:

  - Spravato is administered only under direct observation, as required by
    its FDA Risk Evaluation and Mitigation Strategy (REMS) program, and I must
    remain at the treatment location for a monitoring period after each dose.
  - I must arrange for someone else to drive me home after each treatment
    session and must not drive or operate machinery for the remainder of that
    day.
  - Possible side effects include sedation, dissociation (feeling disconnected
    from reality), dizziness, nausea, and changes in blood pressure, which
    will be monitored during my observation period.
  - Separate enrollment paperwork required by the Spravato REMS program will
    also need to be completed before treatment begins.
  - I may stop treatment at any time by informing my provider.

By signing, I consent to treatment on these terms and confirm I have had the
opportunity to ask questions.""",

"TMS Communication Consent": f"""CONSENT TO COMMUNICATION ABOUT TMS TREATMENT

I consent to {PRACTICE} contacting me by the method(s) I have indicated in my
patient contact preferences (phone, email, and/or text message) regarding
scheduling, reminders, and clinical updates related to Transcranial Magnetic
Stimulation (TMS) treatment.

I understand that:

  - Standard email and SMS text messages are not fully secure, and messages
    sent this way may reference that I have an appointment without describing
    its clinical purpose.
  - I may withdraw or change this consent at any time by updating my contact
    preferences with the front office.
  - This consent covers scheduling and administrative communication; it does
    not itself authorize disclosure of my records to any third party.

By signing, I consent to be contacted as described above.""",

"TMS Not Interested": f"""RECORD OF DECLINED TMS INFORMATION / TREATMENT

This document records that Transcranial Magnetic Stimulation (TMS) treatment
or further information about it was offered and declined at this time.

  - I have been informed that TMS is available as a treatment option.
  - I am choosing not to pursue TMS treatment or further information about it
    at this time.
  - This decision does not affect my access to other treatment offered by
    {PRACTICE}, and I may ask about TMS again in the future if I change my
    mind.

This is a record of a decision made, not a consent to any treatment.""",

"TMS Questionnaire Consent": f"""CONSENT TO COMPLETE A TMS SCREENING QUESTIONNAIRE

I consent to complete a screening questionnaire to help my provider determine
whether Transcranial Magnetic Stimulation (TMS) may be an appropriate
treatment option for me.

I understand that:

  - My answers will be used to assess candidacy against standard TMS
    inclusion and exclusion criteria (for example, seizure history or certain
    implanted medical devices).
  - Completing this questionnaire does not guarantee that TMS will be
    recommended or that I will be found to be a candidate.
  - My answers become part of my medical record.

By signing, I consent to complete this questionnaire on these terms.""",

"Talent Release Form": f"""TALENT / TESTIMONIAL RELEASE

I agree to allow {PRACTICE} to record and use my image, voice, and/or a
testimonial I provide, for use in the practice's marketing materials,
website, and social media.

I understand that:

  - My participation is voluntary and I will not be compensated for it.
  - No clinical or diagnostic information will be disclosed as part of this
    release without my separate written authorization.
  - {PRACTICE} may edit the recording or testimonial for length and clarity,
    without changing its substantive meaning.
  - I may withdraw this consent for future use at any time in writing,
    though material already published may not be fully recallable.

By signing, I grant this release on the terms above.""",

"Telemedicine": f"""INFORMED CONSENT FOR TELEHEALTH SERVICES

I consent to receive care from {PRACTICE} by telehealth (secure video or
telephone) when clinically appropriate.

I understand that:

  - Telehealth involves the use of electronic communication to enable my
    provider and me to interact in real time when we are not in the same
    physical location.
  - The same standards of professional care and confidentiality apply to a
    telehealth visit as to an in-person visit.
  - As with any technology, there is a small risk of interruption, poor
    connection quality, or a technical failure during a session; if this
    happens, my provider and I will reschedule or continue by telephone.
  - I am responsible for participating from a private location where our
    conversation cannot be overheard.
  - Telehealth is not appropriate for a medical or psychiatric emergency. In
    an emergency, I will call 911 or go to the nearest emergency room rather
    than wait for a telehealth appointment.
  - I may request an in-person visit instead of telehealth at any time.

By signing, I consent to receive care by telehealth on these terms.""",
}


def main():
    db = SessionLocal()
    rows = {r.name: r for r in db.query(ConsentForm).all()}
    written, skipped, unmatched = [], [], []

    for name, text in BODIES.items():
        row = rows.get(name)
        if row is None:
            unmatched.append(name)
            continue
        if (row.body or "").strip():
            skipped.append(name)
            continue
        row.body = TOP + text.strip() + BOTTOM
        log(db, f"Consent wording written (draft): {row.name}", "consent", row.id)
        written.append(name)

    still_empty = [r.name for r in rows.values() if not (r.body or "").strip()]

    db.commit()
    print(f"written : {len(written)}")
    for n in written: print(f"   {n}")
    if skipped:
        print(f"\nskipped (already had text): {len(skipped)}")
        for n in skipped: print(f"   {n}")
    if unmatched:
        print(f"\nBODIES had a name not found in the database: {len(unmatched)}")
        for n in unmatched: print(f"   {n}")
    print(f"\nstill empty after this run: {len(still_empty)}")
    for n in still_empty: print(f"   {n}")


if __name__ == "__main__":
    main()
