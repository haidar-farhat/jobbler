"""Field classification is the input to two policy rules, so it gets exhaustive coverage."""

from __future__ import annotations

import pytest
from localapply.contracts import ElementRole
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
