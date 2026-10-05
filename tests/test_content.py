"""Declared content: the webhook, workflows and collections a workspace applies from the integration's card."""

import re

import pytest
from marvin_integration_sdk import Handle, resolve_policy

from marvin_integration_buttondown import ButtondownProvider
from marvin_integration_buttondown.content import (
    CONFIRMED,
    CONFIRMED_SUBSCRIBERS,
    CONTENT,
    EVENTS_WEBHOOK,
    ISSUE_ON_PUBLISH,
    SIGNUP_MATCH_FIELD,
    SIGNUP_TYPE_PARAM,
    SUBSCRIBE_ON_SIGNUP,
    UNSUBSCRIBED,
    UNSUBSCRIBED_READERS,
)
from marvin_integration_buttondown.provider import (
    CODE_AUTH,
    CODE_RATE_LIMITED,
    CODE_UNAVAILABLE,
    CODE_UNKNOWN,
    WEBHOOK_EVENTS,
    ButtondownError,
)

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
# The provider's error policy does it now (Marvin applies it and records `integration_error.buttondown`
# on the entry); the workflow declares no on_failure steps of its own.

# What any action can fail with; the refusals (blocked / spammy / suppressed) come only from subscribe.
CALL_CODES = (CODE_AUTH, CODE_RATE_LIMITED, CODE_UNAVAILABLE, CODE_UNKNOWN)


def _refusal(buttondown_code: str) -> ButtondownError:
    from test_provider import _Http, _run

    http = _Http([("POST", "/v1/subscribers", 400, {"code": buttondown_code, "detail": "Refused."})])
    with pytest.raises(ButtondownError) as raised:
        _run(http, "subscribe", email="reader@example.com")
    return raised.value


def _integration_steps(blueprint):
    return [step for step in _steps(blueprint) if step["kind"] == "integration"]


def test_no_workflow_declares_on_failure_steps():
    assert [b.slug for b in WORKFLOWS if "on_failure" in _definition(b)] == []


@pytest.mark.parametrize("buttondown_code", ["subscriber_blocked", "email_blocked", "ip_address_spammy", "subscriber_suppressed"])
def test_a_refused_signup_goes_to_needs_review_through_the_provider_policy(buttondown_code):
    (subscribe,) = _integration_steps(SUBSCRIBE_ON_SIGNUP)
    error = _refusal(buttondown_code)
    assert resolve_policy(ButtondownProvider, subscribe["action"], error.code) == Handle(review=True)


@pytest.mark.parametrize("blueprint", [CONFIRMED, UNSUBSCRIBED, ISSUE_ON_PUBLISH], ids=lambda b: b.slug)
def test_the_other_workflows_never_send_their_entry_to_review(blueprint):
    # A subscriber event has no signup entry yet when its lookup fails; an issue is published — review would unpublish it.
    for step in _integration_steps(blueprint):
        for code in CALL_CODES:
            handle = resolve_policy(ButtondownProvider, step["action"], code)
            assert handle is not None and not handle.review and not (handle.then and handle.then.review), (step["action"], code)


# ── One entry per reader (Marvin's match field on the signup type) ───────────────────────────────


def _passes(conditions, event: dict) -> bool:
    """The signup workflow's conditions against an event, with Marvin's eq/neq (a missing field is None)."""

    def resolve(path):
        node = {"event": event, "entry": {"entry_type": "{{signup_type}}"}}
        for part in path.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        return node

    ops = {"eq": lambda a, b: a == b, "neq": lambda a, b: a != b}
    return all(ops[c["op"]](resolve(c["field"]), c["value"]) for c in conditions)


def test_signup_skips_a_repeat_from_a_reader_already_confirmed():
    conditions = _definition(SUBSCRIBE_ON_SIGNUP)["conditions"]
    assert {"field": "event.previous_status", "op": "neq", "value": "published"} in conditions


@pytest.mark.parametrize(
    ("event", "subscribes"),
    [
        ({"flagged": False}, True),  # a first signup, or a Marvin without the duplicate fields
        ({"flagged": False, "duplicate": True, "previous_status": "inbox"}, True),  # still pending: subscribe again
        ({"flagged": False, "duplicate": True, "previous_status": "archived"}, True),  # unsubscribed, signed up again
        ({"flagged": False, "duplicate": True, "previous_status": "published"}, False),  # already confirmed
        ({"flagged": True, "duplicate": False, "previous_status": "published"}, False),  # flagged: never forwarded
    ],
)
def test_which_signups_subscribe(event, subscribes):
    assert _passes(_definition(SUBSCRIBE_ON_SIGNUP)["conditions"], event) is subscribes


def test_the_signup_type_is_told_to_match_readers_by_the_subscribed_email_field():
    assert SIGNUP_MATCH_FIELD == "email"
    # The field suggested as the match field is the one the workflow subscribes.
    (subscribe,) = _integration_steps(SUBSCRIBE_ON_SIGNUP)
    assert subscribe["args"]["email"] == f"${{event.submission_data.{SIGNUP_MATCH_FIELD}}}"
    # Suggested wherever the content describes the signup type: the parameter and the signup workflow.
    assert "Same person = same" in SIGNUP_TYPE_PARAM["help"] and f"`{SIGNUP_MATCH_FIELD}`" in SIGNUP_TYPE_PARAM["help"]
    assert "Same person = same" in SUBSCRIBE_ON_SIGNUP.description and SIGNUP_MATCH_FIELD in SUBSCRIBE_ON_SIGNUP.description
    assert all(p is SIGNUP_TYPE_PARAM for b in CONTENT for p in b.parameters if p["key"] == "signup_type")


def test_version_is_0_6_0_everywhere():
    import tomllib
    from pathlib import Path

    import marvin_integration_buttondown

    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert marvin_integration_buttondown.__version__ == pyproject["project"]["version"] == "0.6.0"
