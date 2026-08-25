"""Field classification is the input to two policy rules, so it gets exhaustive coverage."""

from __future__ import annotations

import pytest
from localapply.contracts import ElementRole, ObservedElement
from localapply.policy.field_classifier import FieldClass, classify

NEVER = [
    "Electronic signature",
    "Please sign here",
    "Social Security Number",
    "Have you ever been convicted of a felony?",
    "Do you have a disability?",
    "Are you a protected veteran?",
    "Race / Ethnicity",
    "Date of birth",
    "Password",
    "Credit card number",
    "Passport number",
    "I certify that the above is true",
]

REVIEW = [
    "Expected salary",
    "Desired compensation",
    "Will you require sponsorship?",
    "Are you legally authorized to work in the US?",
    "Are you willing to relocate?",
    "Notice period",
    "How many years of experience do you have with Kubernetes?",
    "Why do you want to work here?",
    "Please provide two references",
    "Reason for leaving your current employer",
]

SAFE = [
    ("First name", "first_name"),
    ("Given Name", "first_name"),
    ("Legal First Name", "first_name"),
    ("Last name", "last_name"),
    ("Surname", "last_name"),
    ("Email address", "email"),
    ("Phone number", "phone"),
    ("LinkedIn profile", "linkedin_url"),
    ("GitHub profile", "github_url"),
    ("City", "city"),
    ("Upload your CV", "resume_path"),
]


@pytest.mark.parametrize("name", NEVER)
def test_never_autofill(make_element, name):
    assert classify(make_element(name=name)).field_class is FieldClass.NEVER_AUTOFILL


@pytest.mark.parametrize("name", REVIEW)
def test_review_required(make_element, name):
    assert classify(make_element(name=name)).field_class is FieldClass.REVIEW_REQUIRED


@pytest.mark.parametrize(("name", "profile_key"), SAFE)
def test_safe_autofill(make_element, name, profile_key):
    result = classify(make_element(name=name))
    assert result.field_class is FieldClass.SAFE_AUTOFILL
    assert result.profile_key == profile_key


def test_unknown_field_defaults_to_review_not_safe(make_element):
    """The important default. An unrecognised field on an unknown site is exactly where
    guessing is worst, so the system must fail towards asking."""
    result = classify(make_element(name="Sprocket alignment preference"))
    assert result.field_class is FieldClass.REVIEW_REQUIRED


def test_most_restrictive_class_wins(make_element):
    """A label matching more than one pattern takes the strictest classification."""
    result = classify(make_element(name="Expected salary — signature required"))
    assert result.field_class is FieldClass.NEVER_AUTOFILL


def test_password_input_type_is_never_regardless_of_label(make_element):
    result = classify(make_element(name="Favourite colour", input_type="password"))
    assert result.field_class is FieldClass.NEVER_AUTOFILL


def test_unidentified_textarea_is_review(make_element):
    result = classify(make_element(name="Additional notes", role=ElementRole.TEXTAREA))
    assert result.field_class is FieldClass.REVIEW_REQUIRED


# --------------------------------------------------------------------------------------
# What a real board actually looks like
#
# Every test above this line uses a label we invented. Running the observer against real
# Greenhouse, Lever and Ashby pages turned up two ways the classifier said SAFE_AUTOFILL
# about something it should not have -- and SAFE_AUTOFILL is the only class that authorises
# acting without a human, so it is the only one where being wrong costs anything.
# --------------------------------------------------------------------------------------


def element(name: str, role: ElementRole = ElementRole.TEXTBOX, **kw) -> ObservedElement:
    return ObservedElement(ref="e1", role=role, name=name, **kw)


def test_a_job_listing_link_is_not_a_city_field():
    """Verified on job-boards.greenhouse.io/vercel: this exact string classified as
    SAFE_AUTOFILL with profile_key=city, because the location contains the word."""
    result = classify(
        element("Director, Major Sales\nHybrid - New York City", ElementRole.LINK)
    )
    assert result.field_class is FieldClass.REVIEW_REQUIRED


def test_a_question_containing_a_safe_word_is_not_a_safe_field():
    """The version that would actually matter: on a form, this would have been filled with
    the candidate's home city and nobody would have been asked."""
    result = classify(
        element(
            "Which office would you like to work from? (New York City, London, Berlin)",
            ElementRole.COMBOBOX,
        )
    )
    assert result.field_class is FieldClass.REVIEW_REQUIRED


@pytest.mark.parametrize(
    "role", [ElementRole.LINK, ElementRole.BUTTON, ElementRole.HEADING, ElementRole.OTHER]
)
def test_only_something_you_can_type_into_is_ever_safe(role):
    """A link cannot be filled, so "safe to fill automatically" is meaningless about one."""
    assert classify(element("Email", role)).field_class is not FieldClass.SAFE_AUTOFILL


@pytest.mark.parametrize(
    "role",
    [ElementRole.TEXTBOX, ElementRole.COMBOBOX, ElementRole.FILE_INPUT, ElementRole.CHECKBOX],
)
def test_a_real_field_is_still_classified(role):
    assert classify(element("Email address", role)).field_class is FieldClass.SAFE_AUTOFILL


def test_a_label_wrapped_across_two_lines_is_still_a_label():
    """Markup breaks labels across lines all the time. Treating that as page text would send
    an ordinary field to a human for approval."""
    result = classify(element("First\n  name"))
    assert result.field_class is FieldClass.SAFE_AUTOFILL
    assert result.profile_key == "first_name"


@pytest.mark.parametrize(
    "label",
    ["City", "City / Town", "Current city of residence", "Postal code", "Resume/CV",
     "E-mail", "LinkedIn Profile", "Phone number"],
)
def test_ordinary_labels_are_unaffected(label):
    assert classify(element(label)).field_class is FieldClass.SAFE_AUTOFILL


def test_the_length_rule_never_makes_something_safe_that_was_not():
    """The asymmetry the whole fix rests on: failing the label check can only ever move a
    field towards asking a human, never away from it."""
    long_and_dangerous = (
        "By typing your full legal name below you provide your electronic signature and "
        "consent to the terms of this application"
    )
    assert classify(element(long_and_dangerous)).field_class is FieldClass.NEVER_AUTOFILL

    long_and_reviewable = (
        "Please describe why you are interested in this role and what salary you would "
        "expect for it"
    )
    assert classify(element(long_and_reviewable)).field_class is FieldClass.REVIEW_REQUIRED
