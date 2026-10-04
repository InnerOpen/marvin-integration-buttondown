"""Declared content: the webhook and workflows a workspace applies from the integration's card."""

from marvin_integration_buttondown import ButtondownProvider
from marvin_integration_buttondown.content import (
    CONFIRMED,
    CONFIRMED_COLLECTION,
    CONTENT,
    EVENTS_WEBHOOK,
    ISSUE_ON_PUBLISH,
    SUBSCRIBE_ON_SIGNUP,
    UNSUBSCRIBED,
    UNSUBSCRIBED_COLLECTION,
)

ACTION_KEYS = {a.key for a in ButtondownProvider.actions}


def _definition(blueprint):
    return blueprint.payload["definition"]


def _steps(blueprint):
    return _definition(blueprint)["actions"]


def test_declares_the_webhook_and_workflows():
    assert [(b.kind, b.slug) for b in CONTENT] == [
        ("incoming_webhook", "buttondown"),
        ("workflow", "buttondown-subscribe-on-signup"),
        ("workflow", "buttondown-subscriber-confirmed"),
        ("workflow", "buttondown-subscriber-unsubscribed"),
        ("workflow", "buttondown-issue-on-publish"),
        ("workflow", "buttondown-confirmed-to-collection"),
        ("workflow", "buttondown-unsubscribed-from-collection"),
    ]
    assert ButtondownProvider.content == CONTENT


def test_the_collection_workflows_are_suggestions_and_the_loop_is_required():
    assert [b.slug for b in CONTENT if not b.required] == [CONFIRMED_COLLECTION.slug, UNSUBSCRIBED_COLLECTION.slug]


def test_the_webhook_requires_the_buttondown_signature():
    assert EVENTS_WEBHOOK.payload["signature_scheme"] in ButtondownProvider.signature_schemes
    assert EVENTS_WEBHOOK.payload["signing_secret_ref"] == "BUTTONDOWN_SIGNING_KEY"
    assert "token" not in EVENTS_WEBHOOK.payload  # minted in the workspace, never declared


def test_every_integration_step_calls_a_declared_action_of_the_parameterised_connection():
    for blueprint in CONTENT[1:]:
        for step in _steps(blueprint):
            if step["kind"] == "integration":
                assert step["action"] in ACTION_KEYS and step["integration"] == "{{integration}}"
                assert "integration" in {p["key"] for p in blueprint.parameters}


def test_entry_types_are_parameters_with_the_documented_defaults():
    defaults = {p["key"]: p["default"] for b in CONTENT for p in b.parameters if p["kind"] == "entry_type"}
    assert defaults == {"signup_type": "newsletter", "issue_type": "newsletter-issue"}


def test_signup_subscribes_unflagged_newsletter_submissions_and_records_the_id():
    d = _definition(SUBSCRIBE_ON_SIGNUP)
    assert d["trigger"] == {"type": "event", "event": "form_submission_received"}
    assert {"field": "entry.entry_type", "op": "eq", "value": "{{signup_type}}"} in d["conditions"]
    assert {"field": "event.flagged", "op": "neq", "value": True} in d["conditions"]
    subscribe, record = d["actions"]
    assert subscribe["action"] == "subscribe" and subscribe["args"]["email"] == "${event.submission_data.email}"
    assert subscribe["args"]["ip_address"] == "${event.ip_address}"
    assert record == {"kind": "entry", "op": "set_metadata", "metadata": {"buttondown_subscriber_id": "${steps.subscribe.output.subscriber_id}"}}


def _subscriber_event_shape(blueprint, event_type):
    d = _definition(blueprint)
    assert d["trigger"] == {"type": "incoming_webhook", "webhook": "buttondown"}
    assert {"field": "event.payload.event_type", "op": "eq", "value": event_type} in d["conditions"]
    lookup, act = d["actions"]
    assert lookup["action"] == "lookup_subscriber" and lookup["args"] == {"subscriber": "${event.payload.data.subscriber}"}
    # The webhook's UUID is resolved to the stored sub_ id before the entry is matched.
    assert act["entity_query"] == {"entry_type": "{{signup_type}}", "metadata": {"buttondown_subscriber_id": "${steps.lookup.output.subscriber_id}"}}
    return act


def test_confirmed_publishes_and_unsubscribed_archives_the_signup_entry():
    assert _subscriber_event_shape(CONFIRMED, "subscriber.confirmed")["op"] == "publish"
    assert _subscriber_event_shape(UNSUBSCRIBED, "subscriber.unsubscribed")["op"] == "archive"


def test_the_collection_workflows_add_on_confirm_and_remove_on_unsubscribe():
    add = _subscriber_event_shape(CONFIRMED_COLLECTION, "subscriber.confirmed")
    remove = _subscriber_event_shape(UNSUBSCRIBED_COLLECTION, "subscriber.unsubscribed")
    assert (add["op"], add["collection_slug"]) == ("add_to_collection", "{{collection}}")
    assert (remove["op"], remove["collection_slug"]) == ("remove_from_collection", "{{collection}}")


def test_issue_workflow_creates_the_email_once_and_records_its_id():
    d = _definition(ISSUE_ON_PUBLISH)
    assert d["trigger"] == {"type": "event", "event": "entry_published"}
    assert d["conditions"] == [{"field": "entry.entry_type", "op": "eq", "value": "{{issue_type}}"}]
    create, record = d["actions"]
    assert create["action"] == "create_issue_email" and create["id"] == "issue"
    assert create["args"]["entry_id"] == "${entry.id}"
    assert create["args"]["email_id"] == "${entry.metadata.buttondown_email_id}"
    assert create["args"]["site_url"] == "${site.url}"
    assert record["op"] == "set_metadata"
    assert record["metadata"]["buttondown_email_id"] == "${steps.issue.output.email_id}"
    # Never blank, so the step succeeds when delivery is off and there is no email id.
    assert record["metadata"]["buttondown_issue_delivery"] == "${steps.issue.output.delivery}"
