"""Durable draft lifecycle (plan v2 §3/§7, `app/onboarding/drafts.py`).

The four properties worth a test: the payload is encrypted at rest, the snapshot
stays pinned, a *settled* application purges the answers, and a *rejected* one
does not — because that is what the operator corrects and resubmits.

"Settled" is the application's own outcome, not the 202 that accepted it: the
submit call confirms in milliseconds and the decision arrives days later, so
purging on confirmation would destroy every answer before the only event that
needs them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update

from app import forms, operations
from app.conduit.client import FieldError
from app.crypto import fernet
from app.models import Draft, Operation
from app.db import sessionmaker
from app.onboarding import drafts
from tests.conftest import settings_override

ACTOR = {"actor_id": "usr_1", "actor_email": "operator@example.com"}

# A minimal Dialect A snapshot. Small on purpose: the pinning test needs *two*
# versions of the same questionnaire, and the difference has to be legible.
SNAPSHOT_V1 = {
    "schemaVersion": "3",
    "context": "onboarding",
    "country": "USA",
    "fields": [
        {"pointer": "/businessInfo/legalName", "label": "Legal name", "type": "string",
         "required": True, "group": "businessInfo"},
    ],
}
# What discovery starts returning a week later: one more required field.
SNAPSHOT_V2 = {
    **SNAPSHOT_V1,
    "fields": [
        *SNAPSHOT_V1["fields"],
        {"pointer": "/businessInfo/taxId", "label": "Tax ID", "type": "string",
         "required": True, "group": "businessInfo"},
    ],
}

ANSWERS = {"root": {"businessInfo": {"legalName": "Acme Robotics LLC"}}, "persons": []}


def values(payload: dict) -> forms.FormValues:
    return forms.FormValues(root=payload["root"])


async def make_draft(session, **kwargs) -> Draft:
    return await drafts.create(
        session,
        kind="onboarding",
        actor_id=ACTOR["actor_id"],
        requirements_snapshot=SNAPSHOT_V1,
        payload=ANSWERS,
        country="USA",
        client_reference_id="acme-2026-08",
        **kwargs,
    )


async def submit(session, draft: Draft, body: dict) -> Operation:
    op, is_new = await operations.start(
        session,
        type="onboarding_submit",
        **ACTOR,
        path="/v2/onboarding",
        body=body,
        draft_id=draft.id,
    )
    assert is_new
    return op


async def reload_draft(session, draft_id) -> Draft:
    return (
        await session.execute(
            select(Draft).where(Draft.id == draft_id).execution_options(populate_existing=True)
        )
    ).scalar_one()


# --- create / update / list / discard ------------------------------------------------


async def test_a_draft_round_trips_its_answers_and_metadata(session):
    draft = await make_draft(session)

    loaded = await drafts.load(session, draft.id)
    assert loaded is not None
    assert loaded.payload == ANSWERS
    assert (loaded.country, loaded.client_reference_id) == ("USA", "acme-2026-08")
    assert loaded.requirements_snapshot == SNAPSHOT_V1
    assert (loaded.submitted_at, loaded.purged_at) == (None, None)


async def test_the_payload_is_encrypted_at_rest(session):
    draft = await make_draft(session)

    stored = await session.scalar(
        text("select payload from drafts where id = :id").bindparams(id=draft.id)
    )
    assert b"Acme Robotics" not in bytes(stored)  # not the operator's answer
    assert b"legalName" not in bytes(stored)  # …nor the shape of it
    assert fernet().decrypt(bytes(stored))  # …but we can still read it back

    # The snapshot is deliberately NOT encrypted: it is Conduit's own public
    # questionnaire, and list views query it.
    snapshot = await session.scalar(
        text("select requirements_snapshot::text from drafts where id = :id").bindparams(
            id=draft.id
        )
    )
    assert "legalName" in snapshot


async def test_list_for_actor_hides_other_actors_and_submitted_drafts(session):
    mine = await make_draft(session)
    await drafts.create(
        session, kind="onboarding", actor_id="usr_2", requirements_snapshot=SNAPSHOT_V1
    )
    submitted = await make_draft(session)
    await drafts.submitted(session, submitted.id)
    await session.commit()

    assert [d.id for d in await drafts.list_for_actor(session, "usr_1")] == [mine.id]
    assert {d.id for d in await drafts.list_for_actor(session, "usr_1", include_submitted=True)} == {
        mine.id,
        submitted.id,
    }


async def test_discard_removes_the_draft_but_not_its_operation(session):
    draft = await make_draft(session)
    op = await submit(session, draft, {"legalName": "Acme Robotics LLC"})

    assert await drafts.discard(session, draft.id) is True
    assert await drafts.load(session, draft.id) is None
    # ON DELETE SET NULL: the operation's own record survives the draft.
    surviving = (
        await session.execute(
            select(Operation).where(Operation.id == op.id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert surviving.draft_id is None


# --- the pinned snapshot -------------------------------------------------------------


async def test_a_draft_is_validated_against_its_own_snapshot_not_a_fresh_one(session):
    """Requirements changed under the operator's feet. The draft keeps answering
    the questionnaire it was rendered from — otherwise a half-finished draft
    would sprout errors for a question it was never shown."""
    draft = await make_draft(session)

    pinned = drafts.model(draft)
    assert [f.path for f in pinned.fields] == [("businessInfo", "legalName")]
    assert forms.validate(pinned, values(ANSWERS)).ok

    # The same answers against today's discovery response: now incomplete.
    fresh = forms.parse(SNAPSHOT_V2)
    assert not forms.validate(fresh, values(ANSWERS)).ok

    # …and re-loading from the database does not quietly re-fetch.
    assert drafts.model(await reload_draft(session, draft.id)).fields == pinned.fields


async def test_a_later_rejection_widens_a_learned_field_instead_of_dropping_it(session):
    """Two rejections, two people, one field the snapshot never advertised.

    Keyed by pointer alone the second descriptor was dropped as a duplicate, so
    the card only *it* named kept an optional copy: the operator answered
    nothing there and the resubmission met the identical 422.
    """
    snapshot = {
        **SNAPSHOT_V1,
        "individualRequirements": [
            {
                "role": "any",
                "minCount": 1,
                "fields": [
                    {"pointer": "/firstName", "label": "First name", "type": "string",
                     "required": True},
                ],
            }
        ],
    }
    draft = await drafts.create(
        session,
        kind="onboarding",
        actor_id=ACTOR["actor_id"],
        requirements_snapshot=snapshot,
        country="USA",
    )

    # Two cards on the one `any` row, which is what the 422s below name.
    cards = [forms.PersonValues(role="any"), forms.PersonValues(role="any")]

    def rejection(index: int) -> list[dict]:
        """What `web.onboarding` learns from a 422 naming person `index`."""
        return forms.learn(
            drafts.model(draft),
            [
                FieldError(
                    pointer=f"/ownership/persons/{index}/residencyPermitExpiryDate",
                    detail="Residency permit expiry is required",
                    category=None,
                    allowed_values=[],
                )
            ],
            cards,
        )

    assert await drafts.learn_fields(session, draft, rejection(0))
    assert await drafts.learn_fields(session, draft, rejection(1))

    stored = (await reload_draft(session, draft.id)).requirements_snapshot[forms.LEARNED_KEY]
    assert len(stored) == 1, "one field is one input"
    assert stored[0][forms.LEARNED_INDICES] == [0, 1], "both cards were named"
    # Discovery's own keys are untouched, and a third identical rejection is a
    # no-op rather than a fresh write.
    assert not await drafts.learn_fields(session, draft, rejection(1))
    assert (await reload_draft(session, draft.id)).requirements_snapshot["fields"] == snapshot[
        "fields"
    ]


async def test_updating_a_draft_never_re_pins_the_snapshot(session):
    draft = await make_draft(session)
    await drafts.update_payload(
        session, draft, {"root": {"businessInfo": {"legalName": "Acme Robotics Inc"}}}
    )

    reloaded = await reload_draft(session, draft.id)
    assert reloaded.requirements_snapshot == SNAPSHOT_V1
    assert reloaded.payload["root"]["businessInfo"]["legalName"] == "Acme Robotics Inc"


# --- submission purges the answers ----------------------------------------------------


async def observe(session, application_id: str, state: str, **extra) -> None:
    """What a webhook or a reconciler read leaves behind for this draft."""
    from app import projections

    await projections.apply_observation(
        session,
        resource_kind="applications",
        resource_id=application_id,
        observed={"id": application_id, "status": state, **extra},
    )


async def test_a_confirmed_submission_stamps_it_but_keeps_the_answers(session):
    """202 is not the outcome: the application can still be rejected, and its
    answers are the only copy of what would be corrected."""
    draft = await make_draft(session)
    op = await submit(session, draft, {"legalName": "Acme Robotics LLC"})

    await operations.transition(session, op.id, "in_flight", **ACTOR)
    await operations.transition(session, op.id, "confirmed", conduit_resource_id="app_1", **ACTOR)

    stamped = await reload_draft(session, draft.id)
    assert stamped.submitted_at is not None
    assert stamped.purged_at is None
    assert stamped.payload == ANSWERS


async def test_an_approved_application_purges_the_payload_and_keeps_the_metadata(session):
    draft = await make_draft(session)
    op = await submit(session, draft, {"legalName": "Acme Robotics LLC"})
    await operations.transition(session, op.id, "in_flight", **ACTOR)
    await operations.transition(session, op.id, "confirmed", conduit_resource_id="app_1", **ACTOR)
    await observe(session, "app_1", "approved", customerId="cus_1")

    assert await drafts.purge_settled(session) == 1
    purged = await reload_draft(session, draft.id)
    assert purged.payload is None
    assert purged.submitted_at is not None and purged.purged_at is not None
    # Everything the application detail view still needs is untouched.
    assert purged.requirements_snapshot == SNAPSHOT_V1
    assert (purged.country, purged.client_reference_id) == ("USA", "acme-2026-08")

    raw = await session.scalar(
        text("select payload from drafts where id = :id").bindparams(id=draft.id)
    )
    assert raw is None  # gone from the column, not merely hidden by the ORM


async def test_a_correctable_rejection_keeps_its_answers_and_a_final_one_does_not(session):
    correctable, final = await make_draft(session), await make_draft(session)
    for draft, application_id in ((correctable, "app_1"), (final, "app_2")):
        op = await submit(session, draft, {"legalName": f"Acme {application_id}"})
        await operations.transition(session, op.id, "in_flight", **ACTOR)
        await operations.transition(
            session, op.id, "confirmed", conduit_resource_id=application_id, **ACTOR
        )
    await observe(session, "app_1", "rejected", resubmittable=True)
    await observe(session, "app_2", "rejected", resubmittable=False)

    assert await drafts.purge_settled(session) == 1
    assert (await reload_draft(session, correctable.id)).payload is not None
    assert (await reload_draft(session, final.id)).payload is None


async def test_a_submission_whose_outcome_never_arrives_is_purged_by_the_backstop(session):
    draft = await make_draft(session)
    op = await submit(session, draft, {"legalName": "Acme Robotics LLC"})
    await operations.transition(session, op.id, "in_flight", **ACTOR)
    await operations.transition(session, op.id, "confirmed", conduit_resource_id="app_1", **ACTOR)

    assert await drafts.purge_settled(session) == 0  # nothing observed yet
    later = datetime.now(UTC) + timedelta(days=31)
    assert await drafts.purge_settled(session, now=later) == 1
    assert (await reload_draft(session, draft.id)).payload is None


async def test_purging_is_only_for_the_draft_the_operation_names(session):
    mine, other = await make_draft(session), await make_draft(session)
    op = await submit(session, mine, {"legalName": "Acme Robotics LLC"})

    await operations.transition(session, op.id, "in_flight", **ACTOR)
    await operations.transition(session, op.id, "confirmed", conduit_resource_id="app_1", **ACTOR)
    await observe(session, "app_1", "approved")
    await drafts.purge_settled(session)

    assert (await reload_draft(session, other.id)).payload == ANSWERS


async def test_a_purged_draft_refuses_further_edits(session):
    draft = await make_draft(session)
    await drafts.submitted(session, draft.id)
    await session.execute(
        update(Draft).where(Draft.id == draft.id).values(payload=None, purged_at=datetime.now(UTC))
    )
    await session.commit()

    with pytest.raises(drafts.DraftClosed):
        await drafts.update_payload(session, await reload_draft(session, draft.id), ANSWERS)


async def test_a_removal_cannot_restore_a_draft_purged_since_it_started(session):
    """The closed-draft check has to be taken under the lock, not before it.

    Checked against the caller's in-memory `Draft`, it reads whatever that object
    held when the route loaded it. A submission can purge the row in the window
    between that read and the lock, and the removal then writes its payload back
    onto a purged draft — restoring the identity answers retention had just
    dropped.
    """
    draft = await make_draft(session)

    # A second session, because the staleness is the point: within one session the
    # identity map refreshes the object and hides the window entirely.
    async with sessionmaker()() as caller:
        stale = await reload_draft(caller, draft.id)
        assert stale.purged_at is None, "the caller's view, taken before the purge"

        # Somebody else submits and retention purges, after the caller's read.
        await drafts.submitted(session, draft.id)
        await session.execute(
            update(Draft)
            .where(Draft.id == draft.id)
            .values(payload=None, purged_at=datetime.now(UTC))
        )
        await session.commit()
        assert stale.purged_at is None, "and still believes it is open"

        with pytest.raises(drafts.DraftClosed):
            await drafts.remove_person(caller, stale, {"root": {}, "persons": []}, 0)
        await caller.rollback()

    assert (await reload_draft(session, draft.id)).payload is None, "still purged"


async def test_a_stale_removal_does_not_undo_one_that_already_happened(session):
    """The payload write has to be inside the real-removal branch.

    Two tabs, three cards. The first removes card 0 and the draft holds two. The
    second was rendered before that and removes card 1 from its own stale three,
    arriving with two — so no card left the *stored* draft, `before == after`, and
    the renumber correctly declines to shift. Writing the payload anyway put the
    first tab's deleted card back.
    """
    three = {"root": {}, "persons": [{"role": "any", "values": {"firstName": n}} for n in "ABC"]}
    draft = await drafts.create(
        session,
        kind="onboarding",
        actor_id=ACTOR["actor_id"],
        requirements_snapshot=SNAPSHOT_V1,
        payload=three,
        country="USA",
    )

    # Tab one removes card 0 -> [B, C].
    first = {"root": {}, "persons": three["persons"][1:]}
    await drafts.remove_person(session, draft, first, 0)
    assert [p["values"]["firstName"] for p in (await reload_draft(session, draft.id)).payload["persons"]] == ["B", "C"]

    # Tab two still believes there are three, and removes card 1 -> [A, C].
    stale = {"root": {}, "persons": [three["persons"][0], three["persons"][2]]}
    await drafts.remove_person(session, draft, stale, 1)

    stored = (await reload_draft(session, draft.id)).payload["persons"]
    assert [p["values"]["firstName"] for p in stored] == ["B", "C"], "A must stay deleted"


async def test_a_removal_is_refused_when_another_tab_changed_the_card_list(session):
    """The stale-request guard is on the cards' shape, not on their count.

    Counting alone passes whenever the arithmetic happens to work out, and it can
    work out while the list has been rebuilt underneath. Here a second tab swaps
    one card for another: the total is unchanged, so a count check sees a clean
    "three became two" and writes a payload that resurrects the card that tab
    deleted and destroys the one it created.
    """
    draft = await drafts.create(
        session,
        kind="onboarding",
        actor_id=ACTOR["actor_id"],
        requirements_snapshot=SNAPSHOT_V1,
        payload={"root": {}, "persons": [
            {"role": "any", "values": {"firstName": "A"}},
            {"role": "BENEFICIAL_OWNER", "values": {"firstName": "B"}},
        ]},
        country="USA",
    )
    # Another tab drops the beneficial owner and adds a second `any`.
    await drafts.update_payload(session, draft, {"root": {}, "persons": [
        {"role": "any", "values": {"firstName": "A"}},
        {"role": "any", "values": {"firstName": "C"}},
    ]})

    # This request was rendered on [any, BENEFICIAL_OWNER] and removes card 0,
    # so it arrives with one card — and 1 == 2 - 1, which is all a count check
    # asks. Removing card 0 from the *stored* list would leave an `any`.
    stale = {"root": {}, "persons": [{"role": "BENEFICIAL_OWNER", "values": {"firstName": "B"}}]}
    returned = await drafts.remove_person(session, draft, stale, 0)

    assert [p["role"] for p in returned["persons"]] == ["any", "any"]
    stored = (await reload_draft(session, draft.id)).payload["persons"]
    assert [p["values"]["firstName"] for p in stored] == ["A", "C"], "the other tab's edit stands"


async def test_answers_typed_before_clicking_remove_are_not_a_conflict(session):
    """`hx-include` posts the whole wizard, so a Remove carries whatever the
    operator has typed since the last save. Those edits are theirs to make.

    A whole-payload compare cannot tell them from somebody else's concurrent edit
    and refuses both, which costs the operator the removal *and* the typing. The
    shape comparison ignores values, so this ordinary sequence works.
    """
    three = {"root": {}, "persons": [{"role": "any", "values": {"firstName": n}} for n in "ABC"]}
    draft = await drafts.create(
        session,
        kind="onboarding",
        actor_id=ACTOR["actor_id"],
        requirements_snapshot=SNAPSHOT_V1,
        payload=three,
        country="USA",
    )
    posted = {
        "root": {},
        "persons": [
            {"role": "any", "values": {"firstName": "B"}},
            {"role": "any", "values": {"firstName": "C-edited"}},
        ],
    }
    returned = await drafts.remove_person(session, draft, posted, 0)

    assert [p["values"]["firstName"] for p in returned["persons"]] == ["B", "C-edited"]
    stored = (await reload_draft(session, draft.id)).payload["persons"]
    assert [p["values"]["firstName"] for p in stored] == ["B", "C-edited"]


# --- rejection correction = re-open ---------------------------------------------------


async def test_a_rejected_submission_leaves_the_draft_editable_and_resubmittable(session):
    """The §1/§7.5 correction loop, from the draft's side: rejection is not
    success, so nothing is purged; the corrected body is a different hash, so it
    is a new operation with a new key — and the draft's own reference stays put,
    which is what says the two attempts are one application."""
    draft = await make_draft(session)
    first = await submit(session, draft, {"legalName": "Acme Robotics LLC"})
    await operations.transition(session, first.id, "in_flight", **ACTOR)
    await operations.transition(
        session, first.id, "rejected", error={"type": "VALIDATION_ERROR"}, **ACTOR
    )

    reopened = await reload_draft(session, draft.id)
    assert reopened.payload == ANSWERS  # still there — this is what gets corrected
    assert (reopened.submitted_at, reopened.purged_at) == (None, None)

    corrected = {"root": {"businessInfo": {"legalName": "Acme Robotics, LLC"}}, "persons": []}
    await drafts.update_payload(session, reopened, corrected)
    second = await submit(session, reopened, {"legalName": "Acme Robotics, LLC"})

    assert second.id != first.id
    assert second.idempotency_key != first.idempotency_key  # never a reused key
    assert second.draft_id == first.draft_id == draft.id
    assert (await reload_draft(session, draft.id)).client_reference_id == "acme-2026-08"
    # The rejected row is untouched by the correction (§7.5).
    assert (
        await session.scalar(select(Operation.state).where(Operation.id == first.id))
    ) == "rejected"

    # …and this one is accepted and then approved, which is when the answers go.
    await operations.transition(session, second.id, "in_flight", **ACTOR)
    await operations.transition(session, second.id, "confirmed", conduit_resource_id="app_9", **ACTOR)
    await observe(session, "app_9", "approved")
    await drafts.purge_settled(session)
    assert (await reload_draft(session, draft.id)).payload is None


# --- retention ------------------------------------------------------------------------


async def age_draft(session, draft_id: uuid.UUID, days: int) -> None:
    when = datetime.now(UTC) - timedelta(days=days)
    await session.execute(
        update(Draft).where(Draft.id == draft_id).values(updated_at=when, created_at=when)
    )
    await session.commit()


async def test_stale_unsubmitted_drafts_are_discarded_after_the_ttl(session):
    # Ids up front: a core UPDATE expires the ORM instances that match it.
    fresh = (await make_draft(session)).id
    stale = (await make_draft(session)).id
    submitted_long_ago = (await make_draft(session)).id
    await drafts.submitted(session, submitted_long_ago)
    await session.commit()
    for draft_id in (stale, submitted_long_ago):
        await age_draft(session, draft_id, days=45)
    await age_draft(session, fresh, days=3)

    assert await drafts.discard_stale(session) == 1
    assert await drafts.load(session, stale) is None
    assert await drafts.load(session, fresh) is not None
    # A submitted draft is provenance, not clutter: its payload is already gone.
    assert await drafts.load(session, submitted_long_ago) is not None


async def test_the_ttl_is_configurable(session):
    draft = await make_draft(session)
    await age_draft(session, draft.id, days=10)

    assert await drafts.discard_stale(session) == 0  # 10 < the 30-day default
    with settings_override(draft_ttl_days=7):
        assert await drafts.discard_stale(session) == 1


async def test_the_worker_tick_runs_the_draft_retention_job(session):
    """Wired into the periodic tick alongside the operation TTL and body purge."""
    import httpx

    from app import worker
    from app.conduit import ConduitClient

    draft = await make_draft(session)
    await age_draft(session, draft.id, days=45)

    client = ConduitClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with settings_override(draft_ttl_days=30):
        await worker.tick(session, client)
    await client.aclose()

    assert await drafts.load(session, draft.id) is None


async def test_discard_refuses_a_submitted_draft(session):
    """`discard` is the operator abandoning work in progress. A submitted draft
    is an application's provenance — what was answered, against which pinned
    questionnaire — and deleting it also orphans the operation's `draft_id`."""
    live, gone = await make_draft(session), await make_draft(session)
    op = await submit(session, live, {"legalName": "Acme Robotics LLC"})
    await operations.transition(session, op.id, "in_flight", **ACTOR)
    await operations.transition(session, op.id, "confirmed", conduit_resource_id="app_1", **ACTOR)

    assert await drafts.discard(session, live.id) is False
    assert await drafts.load(session, live.id) is not None
    assert (
        await session.scalar(select(Operation.draft_id).where(Operation.id == op.id))
    ) == live.id

    # An unsubmitted one still goes, which is what the function is for.
    assert await drafts.discard(session, gone.id) is True
    assert await drafts.load(session, gone.id) is None
