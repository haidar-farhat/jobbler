"""Connectors to job boards that publish a documented public JSON API.

Three of them, and the choice of three is the whole ToS posture in one decision. Greenhouse,
Lever and Ashby each publish an endpoint intended for public consumption: no key, no
registration, no signup, no headers. Reading those is the thing the README already committed
to -- as opposed to scraping HTML that needs a browser to render, which is the thing it
committed *not* to do.

A connector's only job is to turn one board's JSON into `Posting` objects. It does not touch
the database, does not decide anything, and cannot advance a job: everything it produces is
third-party text that flows into `jobs.description` and nowhere else.

Every field mapping here was checked against a live response rather than against the docs,
because on all three boards the docs and the wire disagree in ways that fail *silently*:

  * Greenhouse omits the description entirely unless you ask for it, and still returns 200.
  * Lever puts the job title in `text`, and leaves two thirds of the job text out of the
    field called "description".
  * Ashby's primary key is undocumented, and neither Lever nor Ashby tells you the company.

Each of those produces a job that looks fine and scores zero. The mappings below carry the
evidence in their comments so a future edit does not quietly undo it.
"""

from __future__ import annotations

from .base import BOARDS, Connector, Posting, connector_for
from .ashby import AshbyConnector
from .greenhouse import GreenhouseConnector
from .lever import LeverConnector

__all__ = [
    "BOARDS",
    "AshbyConnector",
    "Connector",
    "GreenhouseConnector",
    "LeverConnector",
    "Posting",
    "connector_for",
]
