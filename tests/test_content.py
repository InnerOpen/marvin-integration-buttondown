"""Declared content: the webhook, workflows and collections a workspace applies from the integration's card."""

import re

import pytest

from marvin_integration_buttondown import ButtondownProvider
from marvin_integration_buttondown.content import (
    CONFIRMED,
    CONFIRMED_SUBSCRIBERS,
    CONTENT,
    EVENTS_WEBHOOK,
    ISSUE_ON_PUBLISH,
    SUBSCRIBE_ON_SIGNUP,
    UNSUBSCRIBED,
    UNSUBSCRIBED_READERS,
)
from marvin_integration_buttondown.provider import CODE_BLOCKED, CODE_SUPPRESSED, WEBHOOK_EVENTS, ButtondownError

ACTION_KEYS = {a.key for a in ButtondownProvider.actions}
WORKFLOWS = [b for b in CONTENT if b.kind == "workflow"]
COLLECTION_OPS = {"add_to_collection", "remove_from_collection"}


def _definition(blueprint):
    return blueprint.payload["definition"]


def _steps(blueprint):
    return _definition(blueprint)["actions"]


def test_declares_the_webhook_workflows_and_collections():
    assert [(b.kind, b.slug) for b in CONTENT] == [
        ("incoming_webhook", "buttondown"),
        ("workflow", "buttondown-subscribe-on-signup"),
        ("workflow", "buttondown-subscriber-confirmed"),
        ("workflow", "buttondown-subscriber-unsubscribed"),
        ("workflow", "buttondown-issue-on-publish"),
        ("collection", "confirmed-subscribers"),
        ("collection", "unsubscribed"),
    ]
    assert ButtondownProvider.content == CONTENT


def test_the_collections_are_suggestions_and_the_loop_is_required():
    assert [b.slug for b in CONTENT if not b.required] == [CONFIRMED_SUBSCRIBERS.slug, UNSUBSCRIBED_READERS.slug]


def test_no_workflow_maintains_a_collection():
    # Membership follows the signup's status through smart rules, so no step adds or removes it by hand.
    ops = {step.get("op") for b in WORKFLOWS for step in _steps(b) + _definition(b).get("on_failure", [])}
    assert not ops & COLLECTION_OPS
    assert "collection" not in {p["key"] for b in CONTENT for p in b.parameters}


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
    for blueprint in WORKFLOWS:
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


# --- smart collections: membership follows the signup's status ---------------------------------------

_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _substitute(value, params):
    """Marvin's blueprint `{{key}}` substitution: a lone placeholder becomes the value itself."""
    if isinstance(value, str):
        whole = _PLACEHOLDER.fullmatch(value.strip())
        if whole and whole.group(1) in params:
            return params[whole.group(1)]
        return _PLACEHOLDER.sub(lambda m: str(params.get(m.group(1), m.group(0))), value)
    if isinstance(value, list):
        return [_substitute(v, params) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, params) for k, v in value.items()}
    return value


def test_every_placeholder_is_a_declared_parameter():
    for blueprint in CONTENT:
        used = set(_PLACEHOLDER.findall(repr((blueprint.slug, blueprint.name, blueprint.payload))))
        assert used <= {p["key"] for p in blueprint.parameters}, blueprint.slug


@pytest.mark.parametrize(
    "blueprint,slug,status",
    [(CONFIRMED_SUBSCRIBERS, "confirmed-subscribers", "published"), (UNSUBSCRIBED_READERS, "unsubscribed", "archived")],
)
def test_collection_is_a_private_smart_collection_of_signups_in_one_status(blueprint, slug, status):
    assert (blueprint.kind, blueprint.slug) == ("collection", slug)
    assert [p["key"] for p in blueprint.parameters] == ["signup_type"]
    payload = blueprint.payload
    # Private: confirms and unsubscribes never rebuild a site, and the publishing API never lists readers.
    assert (payload["is_smart"], payload["is_public"]) == (True, False)
    assert payload["smart_rules"] == {"entry_types": ["{{signup_type}}"], "statuses": [status], "match": "all"}


def test_signup_type_resolves_inside_the_smart_rules():
    for blueprint, status in ((CONFIRMED_SUBSCRIBERS, "published"), (UNSUBSCRIBED_READERS, "archived")):
        rules = _substitute(blueprint.payload, {"signup_type": "mailing-list"})["smart_rules"]
        assert rules == {"entry_types": ["mailing-list"], "statuses": [status], "match": "all"}


def test_the_collections_split_signups_by_the_statuses_the_workflows_set():
    # Confirmed publishes and unsubscribed archives; the collections must read exactly those statuses.
    status_after = {"publish": "published", "archive": "archived"}
    assert CONFIRMED_SUBSCRIBERS.payload["smart_rules"]["statuses"] == [status_after[_steps(CONFIRMED)[-1]["op"]]]
    assert UNSUBSCRIBED_READERS.payload["smart_rules"]["statuses"] == [status_after[_steps(UNSUBSCRIBED)[-1]["op"]]]


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
