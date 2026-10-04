"""Buttondown provider — API calls through a stub http helper, no network."""

import hashlib
import hmac
import json
import logging

import pytest
from marvin_integration_sdk import Handle, IntegrationContext, IntegrationError, Response, Retry, register_provider, resolve_policy

from marvin_integration_buttondown import ButtondownProvider
from marvin_integration_buttondown.provider import (
    CODE_AUTH,
    CODE_BLOCKED,
    CODE_RATE_LIMITED,
    CODE_SPAMMY,
    CODE_SUPPRESSED,
    CODE_UNAVAILABLE,
    CODE_UNKNOWN,
    ButtondownError,
)

API = "https://api.buttondown.com/v1"
KEY = "bd-key"
_LOG = logging.getLogger("test")

SUB_ID = "sub_2xc0abc"
WEBHOOK_UUID = "ac79483b-cd28-49c1-982e-8a88e846d7e7"
SUBSCRIBER = {"id": SUB_ID, "email_address": "reader@example.com", "type": "unactivated", "secondary_id": 7}
EMAIL = {"id": "em_1", "status": "draft", "absolute_url": "https://buttondown.com/n/archive/hello/", "metadata": {"marvin_entry_id": "entry-1"}}


class _Http:
    def __init__(self, routes):
        self.routes = routes
        self.calls: list[dict] = []

    def _answer(self, method, url, body=None, data=None, headers=None):
        self.calls.append({"method": method, "url": url, "json": body, "data": data, "headers": headers})
        for route_method, needle, status, payload, *extra in self.routes:
            if route_method == method and needle in url:
                return Response(status_code=status, headers=extra[0] if extra else {}, content=json.dumps(payload).encode())
        return Response(status_code=599, content=b'{"detail":"no stub route"}')

    def get(self, url, *, headers=None, timeout=15):
        return self._answer("GET", url, headers=headers)

    def post(self, url, *, json=None, data=None, headers=None, timeout=15):
        return self._answer("POST", url, json, data, headers)

    def put(self, url, *, json=None, data=None, headers=None, timeout=15):
        return self._answer("PUT", url, json, data, headers)

    def patch(self, url, *, json=None, data=None, headers=None, timeout=15):
        return self._answer("PATCH", url, json, data, headers)

    def delete(self, url, *, headers=None, timeout=15):
        return self._answer("DELETE", url, headers=headers)

    def posted(self, path):
        return next(c for c in self.calls if c["method"] == "POST" and c["url"] == f"{API}{path}")


def _ctx(http, secret=KEY, **config):
    return IntegrationContext(config=config, secret=secret, logger=_LOG, http=http)


def _run(http, key, config=None, **args):
    return ButtondownProvider().run_action(key, args, _ctx(http, **(config or {})))


# --- check -----------------------------------------------------------------------------------------


def test_check_is_ok_when_ping_accepts_the_key():
    http = _Http([("GET", "/v1/ping", 200, {})])
    assert ButtondownProvider().check(_ctx(http)) == ("ok", None)
    assert http.calls[0]["headers"]["Authorization"] == f"Token {KEY}"


def test_check_reports_a_rejected_key():
    state, detail = ButtondownProvider().check(_ctx(_Http([("GET", "/v1/ping", 401, {"detail": "Invalid token."})])))
    assert state == "error" and "rejected the API key" in detail and "Invalid token." in detail


def test_check_without_a_key_is_unconfigured():
    assert ButtondownProvider().check(_ctx(_Http([]), secret=None))[0] == "unconfigured"


@pytest.mark.parametrize("config", [{"issue_delivery": "maybe"}, {"site_url": "example.com"}])
def test_check_reports_bad_config(config):
    state, _ = ButtondownProvider().check(_ctx(_Http([("GET", "/v1/ping", 200, {})]), **config))
    assert state == "error"


def test_check_never_leaks_the_key_in_its_message():
    _, detail = ButtondownProvider().check(_ctx(_Http([("GET", "/v1/ping", 500, {"detail": "boom"})])))
    assert KEY not in detail


# --- subscribe --------------------------------------------------------------------------------------


def test_subscribe_returns_the_api_subscriber_id():
    http = _Http([("POST", "/v1/subscribers", 201, SUBSCRIBER)])
    out = _run(http, "subscribe", email=" reader@example.com ", tags=["website"], ip_address="203.0.113.9", metadata={"source": "footer"})

    assert out == {"subscriber_id": SUB_ID, "email": "reader@example.com", "type": "unactivated", "already_subscribed": False}
    assert http.posted("/subscribers")["json"] == {
        "email_address": "reader@example.com",
        "tags": ["website"],
        "ip_address": "203.0.113.9",
        "metadata": {"source": "footer"},
    }


def test_subscribe_skips_blank_and_unresolved_optional_args():
    http = _Http([("POST", "/v1/subscribers", 201, SUBSCRIBER)])
    _run(http, "subscribe", email="reader@example.com", tags=[""], ip_address="${event.ip_address}", metadata={})
    assert http.posted("/subscribers")["json"] == {"email_address": "reader@example.com"}


def test_subscribe_an_existing_address_returns_the_existing_subscriber():
    http = _Http(
        [
            ("POST", "/v1/subscribers", 400, {"code": "email_already_exists", "detail": "That email address is already subscribed."}),
            ("GET", "/v1/subscribers/reader@example.com", 200, {**SUBSCRIBER, "type": "regular"}),
        ]
    )
    out = _run(http, "subscribe", email="reader@example.com")
    assert out == {"subscriber_id": SUB_ID, "email": "reader@example.com", "type": "regular", "already_subscribed": True}
    assert not any("send-reminder" in c["url"] for c in http.calls)  # a confirmed reader gets no reminder


_ALREADY = ("POST", "/v1/subscribers", 400, {"code": "email_already_exists", "detail": "That email address is already subscribed."})
_UNACTIVATED = ("GET", "/v1/subscribers/reader@example.com", 200, SUBSCRIBER)
_REMINDER = f"/subscribers/{SUB_ID}/send-reminder"
SUBSCRIBED_OUT = {"subscriber_id": SUB_ID, "email": "reader@example.com", "type": "unactivated"}


def test_subscribe_an_unconfirmed_address_resends_the_confirmation():
    # The reminder route goes first: the stub matches by substring and /v1/subscribers would catch it.
    http = _Http([("POST", _REMINDER, 200, {}), _ALREADY, _UNACTIVATED])
    out = _run(http, "subscribe", email="reader@example.com")

    assert out == {**SUBSCRIBED_OUT, "already_subscribed": True, "confirmation_resent": True}
    assert http.posted(_REMINDER)["headers"]["Authorization"] == f"Token {KEY}"


def test_subscribe_a_refused_reminder_does_not_fail_the_signup():
    http = _Http([("POST", _REMINDER, 429, {"code": "rate_limited", "detail": "Slow down."}), _ALREADY, _UNACTIVATED])
    out = _run(http, "subscribe", email="reader@example.com")

    assert out["already_subscribed"] is True and out["confirmation_resent"] is False
    assert out["confirmation_reason"] == "HTTP 429 rate_limited: Slow down."


class _ReminderOffline(_Http):
    def post(self, url, *, json=None, data=None, headers=None, timeout=15):
        if "send-reminder" in url:
            raise OSError("Network is unreachable")
        return super().post(url, json=json, data=data, headers=headers, timeout=timeout)


def test_subscribe_a_reminder_network_error_does_not_fail_the_signup():
    out = _run(_ReminderOffline([_ALREADY, _UNACTIVATED]), "subscribe", email="reader@example.com")
    assert out["confirmation_resent"] is False and "Network is unreachable" in out["confirmation_reason"]


def _refused(code: str, detail: str = "Refused.") -> _Http:
    return _Http([("POST", "/v1/subscribers", 400, {"code": code, "detail": detail})])


def test_subscribe_blocked_by_the_spam_firewall_is_a_readable_error_coded_blocked():
    with pytest.raises(ButtondownError, match="spam firewall refused reader@example.com") as raised:
        _run(_refused("subscriber_blocked", "This subscriber was blocked by the firewall."), "subscribe", email="reader@example.com")
    assert raised.value.code == CODE_BLOCKED


@pytest.mark.parametrize(("buttondown_code", "code"), [("email_blocked", CODE_BLOCKED), ("ip_address_spammy", CODE_SPAMMY)])
def test_subscribe_other_firewall_refusals_carry_their_code(buttondown_code, code):
    with pytest.raises(ButtondownError, match=f"spam firewall refused reader@example.com \\({buttondown_code}\\)") as raised:
        _run(_refused(buttondown_code), "subscribe", email="reader@example.com")
    assert raised.value.code == code


def test_subscribe_a_suppressed_address_explains_the_earlier_unsubscribe_and_how_to_re_add_it():
    with pytest.raises(ButtondownError, match="unsubscribed from this newsletter before") as raised:
        _run(_refused("subscriber_suppressed", "Suppressed."), "subscribe", email="reader@example.com")
    assert raised.value.code == CODE_SUPPRESSED
    assert "re-add them in Buttondown" in str(raised.value)


def test_subscribe_other_errors_carry_status_code_and_detail_coded_unknown():
    http = _Http([("POST", "/v1/subscribers", 422, {"detail": [{"loc": ["body", "email_address"], "msg": "value is not a valid email"}]})])
    with pytest.raises(ButtondownError, match="HTTP 422: value is not a valid email") as raised:
        _run(http, "subscribe", email="x@y")
    assert raised.value.code == CODE_UNKNOWN


def test_subscribe_needs_an_email_coded_unknown():
    with pytest.raises(ButtondownError, match="email address") as raised:
        _run(_Http([]), "subscribe", email="${event.submission_data.email}")
    assert raised.value.code == CODE_UNKNOWN


def test_a_coded_error_is_still_a_value_error_for_older_marvins():
    with pytest.raises(ValueError):
        _run(_refused("subscriber_blocked"), "subscribe", email="reader@example.com")


# --- lookup_subscriber ------------------------------------------------------------------------------


@pytest.mark.parametrize("key", [WEBHOOK_UUID, SUB_ID])
def test_lookup_subscriber_resolves_either_id_kind_to_the_api_id(key):
    http = _Http([("GET", f"/v1/subscribers/{key}", 200, {**SUBSCRIBER, "type": "regular"})])
    out = _run(http, "lookup_subscriber", subscriber=key)
    assert (out["subscriber_id"], out["email"], out["type"]) == (SUB_ID, "reader@example.com", "regular")


def test_lookup_subscriber_unknown_id_is_a_readable_error():
    http = _Http([("GET", "/v1/subscribers/", 404, {"detail": "Not found."})])
    with pytest.raises(ValueError, match="No Buttondown subscriber"):
        _run(http, "lookup_subscriber", subscriber=WEBHOOK_UUID)


def test_lookup_subscriber_needs_an_id():
    with pytest.raises(ValueError, match="needs a subscriber id"):
        _run(_Http([]), "lookup_subscriber", subscriber="${event.payload.data.subscriber}")


# --- create_issue_email -----------------------------------------------------------------------------

ISSUE = {"subject": "Notes from the studio", "body": "New: [Blue](/works/blue)", "description": "Recent works", "entry_id": "entry-1"}


def _emails(existing=None, list_results=None, create_status=201, create_body=None):
    return _Http(
        [
            ("GET", "/v1/emails/", 200 if existing else 404, existing or {"detail": "Not found."}),
            ("GET", "/v1/emails?", 200, {"results": list_results or [], "count": len(list_results or [])}),
            ("POST", "/v1/emails", create_status, create_body or {"id": "em_new", "status": "draft", "absolute_url": "https://bd/em_new"}),
        ]
    )


def test_create_issue_email_draft_is_the_default_delivery():
    http = _emails()
    out = _run(http, "create_issue_email", **ISSUE)

    assert out == {"email_id": "em_new", "status": "draft", "delivery": "draft", "skipped": False, "reason": "", "url": "https://bd/em_new"}
    assert http.posted("/emails")["json"]["status"] == "draft"


def test_create_issue_email_send_mode_sends_it():
    http = _emails(create_body={"id": "em_new", "status": "about_to_send"})
    out = _run(http, "create_issue_email", config={"issue_delivery": "send"}, **ISSUE)
    assert out["status"] == "about_to_send" and http.posted("/emails")["json"]["status"] == "about_to_send"


def test_create_issue_email_off_does_nothing_and_keeps_a_recorded_id():
    http = _emails()
    out = _run(http, "create_issue_email", config={"issue_delivery": "off"}, email_id="em_old", **ISSUE)
    assert out["skipped"] is True and out["email_id"] == "em_old" and out["reason"] == "Issue delivery is off."
    assert http.calls == []


def test_create_issue_email_payload_has_subject_preview_marker_and_entry_metadata():
    http = _emails()
    _run(http, "create_issue_email", config={"site_url": "https://example.com"}, **ISSUE)
    sent = http.posted("/emails")
    assert sent["json"] == {
        "subject": "Notes from the studio",
        "body": "<!-- buttondown-editor-mode: plaintext -->\nNew: [Blue](https://example.com/works/blue)",
        "description": "Recent works",
        "status": "draft",
        "metadata": {"marvin_entry_id": "entry-1"},
    }
    assert "X-Buttondown-Live-Dangerously" not in sent["headers"]


def test_create_issue_email_uses_the_workspace_site_url_from_the_workflow():
    http = _emails()
    _run(http, "create_issue_email", site_url="https://www.example.org", **ISSUE)
    assert "](https://www.example.org/works/blue)" in http.posted("/emails")["json"]["body"]


def test_create_issue_email_connection_site_url_wins_over_the_workspace_one():
    http = _emails()
    _run(http, "create_issue_email", config={"site_url": "https://example.com"}, site_url="https://www.example.org", **ISSUE)
    assert "](https://example.com/works/blue)" in http.posted("/emails")["json"]["body"]


def test_create_issue_email_unresolved_site_url_template_counts_as_none():
    http = _emails()
    _run(http, "create_issue_email", site_url="${site.url}", **ISSUE)
    assert "](/works/blue)" in http.posted("/emails")["json"]["body"]


def test_create_issue_email_without_a_site_url_keeps_links_and_warns(caplog):
    http = _emails()
    with caplog.at_level(logging.WARNING, logger="test"):
        _run(http, "create_issue_email", **ISSUE)
    assert "](/works/blue)" in http.posted("/emails")["json"]["body"] and "relative links" in caplog.text


def test_create_issue_email_sends_an_absolute_canonical_url():
    http = _emails()
    _run(http, "create_issue_email", canonical_url="https://example.com/notes/hello", **ISSUE)
    assert http.posted("/emails")["json"]["canonical_url"] == "https://example.com/notes/hello"


def test_create_issue_email_makes_a_site_path_canonical_url_absolute():
    http = _emails()
    _run(http, "create_issue_email", config={"site_url": "https://example.com"}, canonical_url="/notes/hello", **ISSUE)
    assert http.posted("/emails")["json"]["canonical_url"] == "https://example.com/notes/hello"


@pytest.mark.parametrize("canonical", ["/notes/hello", "", "${entry.url}"])
def test_create_issue_email_omits_a_canonical_url_it_cannot_make_absolute(canonical):
    http = _emails()
    _run(http, "create_issue_email", canonical_url=canonical, **ISSUE)
    assert "canonical_url" not in http.posted("/emails")["json"]


def test_create_issue_email_returns_the_recorded_email_instead_of_a_second_one():
    http = _emails(existing=EMAIL)
    out = _run(http, "create_issue_email", email_id="em_1", **ISSUE)
    assert (out["email_id"], out["skipped"], out["status"]) == ("em_1", True, "draft")
    assert not any(c["method"] == "POST" for c in http.calls)


def test_create_issue_email_finds_its_email_by_entry_id_when_the_id_was_not_recorded():
    http = _emails(list_results=[{"id": "em_other", "status": "sent", "metadata": {}}, EMAIL])
    out = _run(http, "create_issue_email", email_id="${entry.metadata.buttondown_email_id}", **ISSUE)
    assert (out["email_id"], out["skipped"]) == ("em_1", True)
    assert not any(c["method"] == "POST" for c in http.calls)


def test_create_issue_email_recreates_when_the_recorded_email_was_deleted():
    http = _emails()
    out = _run(http, "create_issue_email", email_id="em_gone", **ISSUE)
    assert out["email_id"] == "em_new" and out["skipped"] is False


def test_create_issue_email_ignores_deleted_emails_in_buttondown():
    http = _emails(existing={**EMAIL, "status": "deleted"}, list_results=[{**EMAIL, "status": "deleted"}])
    assert _run(http, "create_issue_email", email_id="em_1", **ISSUE)["email_id"] == "em_new"


def test_create_issue_email_a_frontmatter_like_body_sends_the_override_header():
    http = _emails()
    _run(http, "create_issue_email", **{**ISSUE, "body": "---\nA rule, not frontmatter"})
    assert http.posted("/emails")["headers"]["X-Buttondown-Live-Dangerously"] == "true"


def test_create_issue_email_keeps_an_explicit_editor_mode():
    http = _emails()
    body = "<!-- buttondown-editor-mode: fancy -->\n<p>Hi</p>"
    _run(http, "create_issue_email", **{**ISSUE, "body": body})
    assert http.posted("/emails")["json"]["body"] == body


def test_create_issue_email_errors_are_readable():
    http = _emails(create_status=400, create_body={"code": "sending_requires_confirmation", "detail": "Confirm first."})
    with pytest.raises(ValueError, match="sending confirmed"):
        _run(http, "create_issue_email", config={"issue_delivery": "send"}, **ISSUE)
    http = _emails(create_status=400, create_body={"code": "subject_invalid", "detail": "Bad subject."})
    with pytest.raises(ValueError, match="HTTP 400 subject_invalid: Bad subject."):
        _run(http, "create_issue_email", **ISSUE)


def test_create_issue_email_rejects_a_bad_delivery_setting():
    with pytest.raises(ValueError, match="off, draft or send"):
        _run(_emails(), "create_issue_email", config={"issue_delivery": "later"}, **ISSUE)


# --- plumbing ---------------------------------------------------------------------------------------


def test_unknown_action_and_missing_key():
    with pytest.raises(NotImplementedError):
        _run(_Http([]), "nope")
    with pytest.raises(ValueError, match="API key") as raised:
        ButtondownProvider().run_action("subscribe", {"email": "a@b.c"}, _ctx(_Http([]), secret=None))
    assert raised.value.code == CODE_AUTH


class _Offline(_Http):
    error: Exception = OSError("Network is unreachable")

    def _answer(self, *args, **kwargs):
        raise self.error


class _TimingOut(_Offline):
    error = TimeoutError("timed out")


def test_a_network_error_fails_the_step_as_a_value_error_coded_unavailable():
    # Marvin's workflow engine turns only ValueError into a failed step; anything else escapes the run.
    with pytest.raises(ValueError, match="Buttondown lookup_subscriber failed: OSError: Network is unreachable") as raised:
        _run(_Offline([]), "lookup_subscriber", subscriber=SUB_ID)
    assert raised.value.code == CODE_UNAVAILABLE


def test_a_timeout_is_coded_unavailable():
    with pytest.raises(ButtondownError, match="TimeoutError: timed out") as raised:
        _run(_TimingOut([]), "subscribe", email="reader@example.com")
    assert raised.value.code == CODE_UNAVAILABLE and raised.value.retry_after is None


def test_any_other_exception_still_fails_the_step_coded_unknown():
    class _Broken(_Offline):
        error = RuntimeError("boom")

    with pytest.raises(ButtondownError, match="RuntimeError: boom") as raised:
        _run(_Broken([]), "subscribe", email="reader@example.com")
    assert raised.value.code == CODE_UNKNOWN


# --- error codes from HTTP statuses -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, CODE_AUTH),
        (403, CODE_AUTH),
        (429, CODE_RATE_LIMITED),
        (500, CODE_UNAVAILABLE),
        (502, CODE_UNAVAILABLE),
        (503, CODE_UNAVAILABLE),
        (404, CODE_UNKNOWN),
        (422, CODE_UNKNOWN),
    ],
)
def test_http_failures_are_coded_by_status(status, code):
    http = _Http([("POST", "/v1/subscribers", status, {"detail": "Nope."})])
    with pytest.raises(ButtondownError, match=f"HTTP {status}: Nope.") as raised:
        _run(http, "subscribe", email="reader@example.com")
    assert raised.value.code == code


@pytest.mark.parametrize(
    ("key", "args", "route"),
    [
        ("lookup_subscriber", {"subscriber": SUB_ID}, ("GET", "/v1/subscribers/", 503, {"detail": "Down."})),
        ("create_issue_email", {"subject": "Hi", "body": "x", "email_id": "em_1"}, ("GET", "/v1/emails/", 401, {"detail": "Invalid token."})),
        (
            "connect_webhooks",
            {"webhook_url": "https://api.example.com/api/hooks/tok", "signing_key": "k"},
            ("GET", "/v1/webhooks", 429, {"detail": "Slow."}),
        ),
    ],
)
def test_every_action_codes_its_http_failures(key, args, route):
    with pytest.raises(ButtondownError) as raised:
        _run(_Http([route]), key, **args)
    assert raised.value.code == {503: CODE_UNAVAILABLE, 401: CODE_AUTH, 429: CODE_RATE_LIMITED}[route[2]]


@pytest.mark.parametrize(("header", "expected"), [("Retry-After", 30.0), ("retry-after", 30.0), ("RETRY-AFTER", 30.0)])
def test_rate_limited_carries_retry_after_seconds(header, expected):
    http = _Http([("POST", "/v1/subscribers", 429, {"code": "rate_limited", "detail": "Slow down."}, {header: "30"})])
    with pytest.raises(ButtondownError, match="HTTP 429 rate_limited: Slow down.") as raised:
        _run(http, "subscribe", email="reader@example.com")
    assert raised.value.code == CODE_RATE_LIMITED and raised.value.retry_after == expected


def test_rate_limited_reads_an_http_date_retry_after():
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    when = format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)
    http = _Http([("POST", "/v1/subscribers", 429, {"detail": "Slow down."}, {"Retry-After": when})])
    with pytest.raises(ButtondownError) as raised:
        _run(http, "subscribe", email="reader@example.com")
    assert 100 <= raised.value.retry_after <= 120


@pytest.mark.parametrize("value", [None, "", "soon", "Mon, 01 Jan 2001 00:00:00 GMT", "-5"])
def test_rate_limited_without_a_usable_retry_after(value):
    headers = {} if value is None else {"Retry-After": value}
    http = _Http([("POST", "/v1/subscribers", 429, {"detail": "Slow down."}, headers)])
    with pytest.raises(ButtondownError) as raised:
        _run(http, "subscribe", email="reader@example.com")
    # A missing or garbled hint leaves the policy's backoff in charge; a past date or negative means "now".
    assert raised.value.code == CODE_RATE_LIMITED
    assert raised.value.retry_after == {None: None, "": None, "soon": None}.get(value, 0.0)


def test_retry_after_is_only_read_for_a_rate_limit():
    http = _Http([("POST", "/v1/subscribers", 503, {"detail": "Down."}, {"Retry-After": "30"})])
    with pytest.raises(ButtondownError) as raised:
        _run(http, "subscribe", email="reader@example.com")
    assert raised.value.code == CODE_UNAVAILABLE and raised.value.retry_after is None


def test_a_coded_error_is_an_integration_error_that_survives_pickling():
    import pickle

    error = ButtondownError("Slow down.", CODE_RATE_LIMITED, retry_after=30.0)
    assert isinstance(error, IntegrationError) and isinstance(error, ValueError)
    copy = pickle.loads(pickle.dumps(error))
    assert (type(copy), str(copy), copy.code, copy.retry_after) == (ButtondownError, "Slow down.", CODE_RATE_LIMITED, 30.0)


# --- error policy -----------------------------------------------------------------------------------

REVIEW = Handle(review=True)
NOTIFY = Handle(notify=True)


def test_register_provider_accepts_the_error_policy():
    assert register_provider(ButtondownProvider) is ButtondownProvider


def test_info_shows_how_each_error_is_handled():
    policy = ButtondownProvider().info()["error_policy"]
    assert set(policy["provider"]) == {CODE_BLOCKED, CODE_SPAMMY, CODE_SUPPRESSED, CODE_UNAVAILABLE, CODE_RATE_LIMITED, CODE_AUTH, "*"}
    assert policy["provider"][CODE_UNAVAILABLE]["summary"] == "retry 3× (2m, 10m, 1h), then send to review"
    assert policy["provider"][CODE_RATE_LIMITED]["summary"] == "retry 3× (1m, 5m, 15m), then send to review"
    assert policy["provider"][CODE_AUTH]["summary"] == "notify admins, wait for the connection to recover, then retry once, then send to review"
    assert policy["provider"][CODE_BLOCKED]["summary"] == policy["provider"]["*"]["summary"] == "send to review"
    assert set(policy["actions"]) == {"subscribe", "lookup_subscriber", "create_issue_email", "connect_webhooks"}
    assert policy["actions"]["subscribe"] == {}  # the signup follows the provider's policy


@pytest.mark.parametrize(
    ("code", "handle"),
    [
        (CODE_BLOCKED, REVIEW),
        (CODE_SPAMMY, REVIEW),
        (CODE_SUPPRESSED, REVIEW),
        (CODE_UNAVAILABLE, Handle(retry=Retry(backoff=(120, 600, 3600)), then=REVIEW)),
        (CODE_RATE_LIMITED, Handle(retry=Retry(backoff=(60, 300, 900)), then=REVIEW)),
        (CODE_AUTH, Handle(notify=True, retry=Retry(backoff=(), on_recovery=True), then=REVIEW)),
        (CODE_UNKNOWN, REVIEW),
        ("something_new", REVIEW),
    ],
)
def test_a_failed_signup_is_handled_by_the_provider_policy(code, handle):
    assert resolve_policy(ButtondownProvider, "subscribe", code) == handle


@pytest.mark.parametrize("action", ["lookup_subscriber", "create_issue_email"])
@pytest.mark.parametrize(
    ("code", "handle"),
    [
        (CODE_UNAVAILABLE, Handle(retry=Retry(backoff=(120, 600, 3600)), then=NOTIFY)),
        (CODE_RATE_LIMITED, Handle(retry=Retry(backoff=(60, 300, 900)), then=NOTIFY)),
        (CODE_AUTH, Handle(notify=True, retry=Retry(backoff=(), on_recovery=True))),
        (CODE_UNKNOWN, Handle()),
    ],
)
def test_lookups_and_issue_emails_retry_but_never_send_an_entry_to_review(action, code, handle):
    # lookup_subscriber runs before there is a signup entry; create_issue_email's entry is a published issue.
    resolved = resolve_policy(ButtondownProvider, action, code)
    assert resolved == handle
    assert not resolved.review and not (resolved.then and resolved.then.review)


@pytest.mark.parametrize("code", [CODE_UNAVAILABLE, CODE_RATE_LIMITED, CODE_AUTH, CODE_UNKNOWN])
def test_connect_webhooks_just_fails_for_the_admin_who_ran_it(code):
    assert resolve_policy(ButtondownProvider, "connect_webhooks", code) == Handle()


def test_declared_actions_match_the_handlers():
    assert {a.key for a in ButtondownProvider.actions} == {"subscribe", "lookup_subscriber", "create_issue_email", "connect_webhooks"}


# --- connect_webhooks -------------------------------------------------------------------------------

HOOK = "https://api.example.com/api/hooks/tok_new"
LEGACY = "https://api.example.com/api/hooks/tok_old"
SIGNING = "whsec-123"
EVENTS = ["subscriber.confirmed", "subscriber.unsubscribed"]
DESCRIPTION = "Marvin: subscriber confirmations and unsubscribes"
# Another workspace on the same Buttondown account and the same Marvin host — never ours to touch.
FOREIGN = {"id": "wh_other", "url": "https://api.example.com/api/hooks/tok_other", "status": "enabled", "event_types": EVENTS}
DESIRED = {"url": HOOK, "event_types": EVENTS, "status": "enabled", "description": DESCRIPTION, "signing_key": SIGNING}


def _webhooks(*results, next_url=None):
    return ("GET", "/v1/webhooks", 200, {"results": list(results), "next": next_url, "count": len(results)})


def _connect(http, **args):
    return _run(http, "connect_webhooks", webhook_url=HOOK, signing_key=SIGNING, **args)


def _writes(http):
    return [(c["method"], c["url"].removeprefix(API)) for c in http.calls if c["method"] != "GET"]


def test_connect_webhooks_creates_one_enabled_signed_webhook_for_our_url():
    http = _Http([_webhooks(FOREIGN), ("POST", "/v1/webhooks", 201, {"id": "wh_new", **DESIRED})])
    out = _connect(http)

    assert out == {"webhook_id": "wh_new", "result": "created", "created": True, "event_types": EVENTS, "removed": [], "legacy_removed": False}
    assert http.posted("/webhooks")["json"] == DESIRED
    assert _writes(http) == [("POST", "/webhooks")]  # the other workspace's webhook is untouched


def test_connect_webhooks_updates_the_webhook_already_on_our_url_in_place():
    stale = {"id": "wh_ours", "url": HOOK + "/", "status": "disabled", "event_types": ["subscriber.created"], "description": "", "signing_key": ""}
    http = _Http([_webhooks(FOREIGN, stale), ("PATCH", "/v1/webhooks/wh_ours", 200, {**stale, **DESIRED})])
    out = _connect(http)

    assert out["result"] == "updated" and out["webhook_id"] == "wh_ours" and out["created"] is False
    assert _writes(http) == [("PATCH", "/webhooks/wh_ours")]
    assert http.calls[-1]["json"] == DESIRED


def test_connect_webhooks_leaves_a_matching_webhook_alone():
    http = _Http([_webhooks(FOREIGN, {"id": "wh_ours", **DESIRED})])
    assert _connect(http)["result"] == "unchanged"
    assert _writes(http) == []


def test_connect_webhooks_labels_the_description():
    http = _Http([_webhooks(), ("POST", "/v1/webhooks", 201, {"id": "wh_new"})])
    _connect(http, label="Mash & Burn")
    assert http.posted("/webhooks")["json"]["description"] == f"{DESCRIPTION} (Mash & Burn)"


class _NoPatchHttp(_Http):
    patch = None  # a Marvin whose http helper predates PATCH


def test_connect_webhooks_without_patch_replaces_the_webhook_new_one_first():
    stale = {"id": "wh_ours", "url": HOOK, "status": "disabled", "event_types": EVENTS}
    http = _NoPatchHttp([_webhooks(stale), ("POST", "/v1/webhooks", 201, {"id": "wh_new", **DESIRED}), ("DELETE", "/v1/webhooks/wh_ours", 204, {})])
    out = _connect(http)

    assert out["result"] == "replaced" and out["webhook_id"] == "wh_new"
    assert _writes(http) == [("POST", "/webhooks"), ("DELETE", "/webhooks/wh_ours")]


def test_connect_webhooks_removes_duplicates_on_our_own_url():
    http = _Http([_webhooks({"id": "wh_a", **DESIRED}, {"id": "wh_b", **DESIRED}), ("DELETE", "/v1/webhooks/wh_b", 204, {})])
    out = _connect(http)
    assert out["result"] == "unchanged" and out["removed"] == ["wh_b"]
    assert _writes(http) == [("DELETE", "/webhooks/wh_b")]


def test_connect_webhooks_retires_the_legacy_url_on_an_exact_match_only():
    legacy = {"id": "wh_legacy", "url": LEGACY, "status": "enabled", "event_types": EVENTS}
    near_miss = {"id": "wh_near", "url": LEGACY + "2", "status": "enabled", "event_types": EVENTS}
    http = _Http(
        [
            _webhooks(FOREIGN, legacy, near_miss, {"id": "wh_ours", **DESIRED}),
            ("DELETE", "/v1/webhooks/wh_legacy", 204, {}),
        ]
    )
    out = _connect(http, remove_legacy_url=LEGACY)

    assert out["legacy_removed"] is True and out["removed"] == ["wh_legacy"]
    assert _writes(http) == [("DELETE", "/webhooks/wh_legacy")]


def test_connect_webhooks_a_legacy_url_buttondown_does_not_have_is_not_an_error():
    http = _Http([_webhooks(FOREIGN, {"id": "wh_ours", **DESIRED})])
    out = _connect(http, remove_legacy_url=LEGACY)
    assert out["legacy_removed"] is False and _writes(http) == []


def test_connect_webhooks_follows_pagination():
    page2 = f"{API}/webhooks?page=2"
    http = _Http(
        [
            ("GET", "/v1/webhooks?page=2", 200, {"results": [{"id": "wh_ours", **DESIRED}], "next": None}),
            _webhooks(FOREIGN, next_url=page2),
        ]
    )
    assert _connect(http)["result"] == "unchanged"
    assert [c["url"] for c in http.calls] == [f"{API}/webhooks", page2]


class _FakeButtondown(_Http):
    """Just enough of Buttondown's webhook store to run the action twice against the same state."""

    def __init__(self, webhooks):
        super().__init__([])
        self.store = {w["id"]: dict(w) for w in webhooks}

    def get(self, url, *, headers=None, timeout=15):
        self.calls.append({"method": "GET", "url": url})
        return Response(status_code=200, content=json.dumps({"results": list(self.store.values()), "next": None}).encode())

    def post(self, url, **kwargs):  # kwargs, not json=: the parameter would shadow the json module
        self.calls.append({"method": "POST", "url": url})
        created = {"id": f"wh_{len(self.store) + 1}", **kwargs["json"]}
        self.store[created["id"]] = created
        return Response(status_code=201, content=json.dumps(created).encode())


def test_connect_webhooks_twice_makes_exactly_one_webhook():
    fake = _FakeButtondown([FOREIGN])
    first, second = _connect(fake), _connect(fake)

    assert (first["result"], second["result"]) == ("created", "unchanged")
    assert sorted(w["url"] for w in fake.store.values()) == sorted([FOREIGN["url"], HOOK])


@pytest.mark.parametrize(
    "args, message",
    [
        ({"webhook_url": "https://api.example.com/settings/webhooks", "signing_key": SIGNING}, "Marvin incoming webhook URL"),
        ({"webhook_url": "http://api.example.com/api/hooks/tok", "signing_key": SIGNING}, "Marvin incoming webhook URL"),
        ({"webhook_url": "https://api.example.com/api/hooks/", "signing_key": SIGNING}, "Marvin incoming webhook URL"),
        ({"webhook_url": HOOK, "signing_key": "{{BUTTONDOWN_SIGNING_KEY}}"}, "signing_key is required"),
        ({"webhook_url": HOOK, "signing_key": ""}, "signing_key is required"),
        ({"webhook_url": HOOK, "signing_key": SIGNING, "remove_legacy_url": HOOK + "/"}, "being connected"),
        ({"webhook_url": HOOK, "signing_key": SIGNING, "remove_legacy_url": "https://example.com/other"}, "remove_legacy_url must be"),
    ],
)
def test_connect_webhooks_rejects_bad_arguments_before_calling_buttondown(args, message):
    http = _Http([])
    with pytest.raises(ValueError, match=message):
        _run(http, "connect_webhooks", **args)
    assert http.calls == []


def test_connect_webhooks_list_failure_is_readable():
    http = _Http([("GET", "/v1/webhooks", 403, {"code": "forbidden", "detail": "Your plan lacks webhooks."})])
    with pytest.raises(ValueError, match="list webhooks failed: HTTP 403 forbidden: Your plan lacks webhooks."):
        _connect(http)


# --- signature scheme -------------------------------------------------------------------------------


def _verify(scheme: dict, body: bytes, headers: dict, key: str) -> bool:
    """Marvin's HMAC check as the SDK documents a scheme: digest(key, message) == header minus prefix."""
    message = scheme["message"].replace("{body}", body.decode())
    digest = hmac.new(key.encode(), message.encode(), getattr(hashlib, scheme["algorithm"])).hexdigest()
    assert scheme["encoding"] == "hex"
    sent = {k.lower(): v for k, v in headers.items()}.get(scheme["header"].lower(), "")
    return sent.startswith(scheme["prefix"]) and hmac.compare_digest(sent[len(scheme["prefix"]) :], digest)


def _buttondown_sign(body: bytes, key: str) -> str:
    """How Buttondown signs, from its docs' verification snippet."""
    return "sha256=" + hmac.new(key.encode("utf-8"), msg=body, digestmod=hashlib.sha256).hexdigest()


WEBHOOK_BODY = json.dumps({"event_type": "subscriber.confirmed", "data": {"subscriber": WEBHOOK_UUID}}).encode()


def test_buttondown_scheme_accepts_a_real_signature():
    scheme = ButtondownProvider.signature_schemes["buttondown"]
    headers = {"X-Buttondown-Signature": _buttondown_sign(WEBHOOK_BODY, "signing-key")}
    assert _verify(scheme, WEBHOOK_BODY, headers, "signing-key")


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Buttondown-Signature": _buttondown_sign(WEBHOOK_BODY, "wrong-key")},
        {"X-Buttondown-Signature": _buttondown_sign(WEBHOOK_BODY + b" ", "signing-key")},
        {},  # Buttondown's Test webhook button sends unsigned
    ],
)
def test_buttondown_scheme_rejects_a_bad_or_missing_signature(headers):
    assert not _verify(ButtondownProvider.signature_schemes["buttondown"], WEBHOOK_BODY, headers, "signing-key")
