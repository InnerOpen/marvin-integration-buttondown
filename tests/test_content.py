"""Declared content: the webhook and workflows a workspace applies from the integration's card."""

import re

import pytest

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
from marvin_integration_buttondown.provider import CODE_BLOCKED, CODE_SUPPRESSED, WEBHOOK_EVENTS, ButtondownError

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


def test_connect_webhooks_subscribes_to_exactly_the_events_the_workflows_handle():
    handled = {
        cond["value"]
        for b in CONTENT
        if b.kind == "workflow" and _definition(b)["trigger"]["type"] == "incoming_webhook"
        for cond in _definition(b)["conditions"]
        if cond["field"] == "event.payload.event_type"
    }
    assert handled == set(WEBHOOK_EVENTS)


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
    # A reader who subscribed outside the site has no signup entry: skipped quietly, not a failed run.
    assert act["if_none"] == "skip"
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
    assert create["args"]["canonical_url"] == "${entry.url}"
    assert record["op"] == "set_metadata"
    assert record["metadata"]["buttondown_email_id"] == "${steps.issue.output.email_id}"
    # Never blank, so the step succeeds when delivery is off and there is no email id.
    assert record["metadata"]["buttondown_issue_delivery"] == "${steps.issue.output.delivery}"


# --- a refused signup goes to Needs review ------------------------------------------------------------
# Marvin runs a workflow's `on_failure` steps when a step fails, with the failure as `${error.*}`. These
# tests resolve the declared steps the way Marvin's templates do, against the error the provider raises
# for a stubbed Buttondown refusal (Marvin prefixes the step: "buttondown.subscribe failed: …").

_TEMPLATE = re.compile(r"\$\{([^}]+)\}")


def _resolve(value, context):
    """Marvin's `${path}` templates: a whole-string template keeps the value's type, embedded ones become text."""
    if isinstance(value, dict):
        return {k: _resolve(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, context) for v in value]
    if not isinstance(value, str):
        return value

    def lookup(path):
        node = context
        for part in path.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        return node

    whole = _TEMPLATE.fullmatch(value)
    return lookup(whole.group(1)) if whole else _TEMPLATE.sub(lambda m: str(lookup(m.group(1)) or ""), value)


def _refusal(buttondown_code: str) -> ButtondownError:
    from test_provider import _Http, _run

    http = _Http([("POST", "/v1/subscribers", 400, {"code": buttondown_code, "detail": "Refused."})])
    with pytest.raises(ButtondownError) as raised:
        _run(http, "subscribe", email="reader@example.com")
    return raised.value


def _on_failure_for(error: ButtondownError):
    """The signup workflow's on-failure steps, resolved for a failed subscribe step."""
    context = {
        "error": {"message": f"buttondown.subscribe failed: {error}", "code": error.code, "step": "subscribe", "at": "2026-10-04T10:00:00+00:00"}
    }
    return _resolve(_definition(SUBSCRIBE_ON_SIGNUP)["on_failure"], context)


def test_a_blocked_signup_records_the_error_and_goes_to_needs_review_with_the_reason():
    record, review = _on_failure_for(_refusal("subscriber_blocked"))

    assert record["op"] == "set_metadata"
    error = record["metadata"]["buttondown_subscribe_error"]
    assert (error["code"], error["at"]) == (CODE_BLOCKED, "2026-10-04T10:00:00+00:00")
    assert "spam firewall refused reader@example.com" in error["message"]
    assert review["op"] == "request_review" and "spam firewall refused reader@example.com" in review["reason"]


def test_a_suppressed_signup_goes_to_needs_review_saying_to_re_add_them_in_buttondown():
    record, review = _on_failure_for(_refusal("subscriber_suppressed"))

    assert record["metadata"]["buttondown_subscribe_error"]["code"] == CODE_SUPPRESSED
    assert review["op"] == "request_review"
    assert "unsubscribed from this newsletter before" in review["reason"] and "re-add them in Buttondown" in review["reason"]


def test_the_failure_steps_act_on_the_signup_entry_itself():
    # No entity_* target: they act on the triggering entry, the signup the subscribe step failed for.
    for step in _definition(SUBSCRIBE_ON_SIGNUP)["on_failure"]:
        assert step["kind"] == "entry" and not {"entity_id", "entity_slug", "entity_query"} & step.keys()


def test_only_the_signup_workflow_sends_failures_to_review():
    assert [b.slug for b in CONTENT if b.kind == "workflow" and "on_failure" in _definition(b)] == [SUBSCRIBE_ON_SIGNUP.slug]
