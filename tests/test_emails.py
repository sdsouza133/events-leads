"""The text of the emails the service sends."""

import uuid

from app.emails import results_email, run_url

RUN_ID = uuid.UUID("00000000-0000-4000-8000-000000000007")


def test_the_link_is_the_run_page_whatever_the_base_url_ends_with() -> None:
    assert run_url("https://leads.example/", RUN_ID) == f"https://leads.example/runs/{RUN_ID}"
    assert run_url("https://leads.example", RUN_ID) == f"https://leads.example/runs/{RUN_ID}"


def test_the_results_email_names_the_events_and_counts_the_people() -> None:
    email = results_email(
        to="jo@acme.example",
        base_url="https://leads.example",
        run_id=RUN_ID,
        leads=1,
        events=["SaaStr Annual", "INBOUND", "Pipeline Summit"],
        days=30,
    )
    assert email.key == f"results:{RUN_ID}"
    assert "We found 1 person who went to, or are going to, SaaStr Annual, INBOUND and Pipeline Summit," in email.text


def test_a_run_without_events_on_record_still_reads_well() -> None:
    email = results_email(
        to="jo@acme.example", base_url="https://x.example", run_id=RUN_ID, leads=0, events=[], days=30
    )
    assert "the LinkedIn posts about your events" in email.text
