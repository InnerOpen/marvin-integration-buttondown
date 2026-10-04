"""Buttondown provider — API calls through a stub http helper, no network."""

import hashlib
import hmac
import json
import logging

import pytest
from marvin_integration_sdk import IntegrationContext, Response

from marvin_integration_buttondown import ButtondownProvider

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
        for route_method, needle, status, payload in self.routes:
            if route_method == method and needle in url:
                return Response(status_code=status, content=json.dumps(payload).encode())
        return Response(status_code=599, content=b'{"detail":"no stub route"}')

    def get(self, url, *, headers=None, timeout=15):
        return self._answer("GET", url, headers=headers)

    def post(self, url, *, json=None, data=None, headers=None, timeout=15):
        return self._answer("POST", url, json, data, headers)

    def put(self, url, *, json=None, data=None, headers=None, timeout=15):
        return self._answer("PUT", url, json, data, headers)

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


def test_subscribe_blocked_by_the_spam_firewall_is_a_readable_error():
    http = _Http([("POST", "/v1/subscribers", 400, {"code": "subscriber_blocked", "detail": "This subscriber was blocked by the firewall."})])
    with pytest.raises(ValueError, match="spam firewall refused reader@example.com"):
        _run(http, "subscribe", email="reader@example.com")


def test_subscribe_a_suppressed_address_explains_the_earlier_unsubscribe():
    http = _Http([("POST", "/v1/subscribers", 400, {"code": "subscriber_suppressed", "detail": "Suppressed."})])
    with pytest.raises(ValueError, match="unsubscribed from this newsletter before"):
        _run(http, "subscribe", email="reader@example.com")


def test_subscribe_other_errors_carry_status_code_and_detail():
    http = _Http([("POST", "/v1/subscribers", 422, {"detail": [{"loc": ["body", "email_address"], "msg": "value is not a valid email"}]})])
    with pytest.raises(ValueError, match="HTTP 422: value is not a valid email"):
        _run(http, "subscribe", email="x@y")


def test_subscribe_needs_an_email():
    with pytest.raises(ValueError, match="email address"):
        _run(_Http([]), "subscribe", email="${event.submission_data.email}")


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
    with pytest.raises(ValueError, match="API key"):
        ButtondownProvider().run_action("subscribe", {"email": "a@b.c"}, _ctx(_Http([]), secret=None))


class _Offline(_Http):
    def get(self, url, *, headers=None, timeout=15):
        raise OSError("Network is unreachable")

    post = get


def test_a_network_error_fails_the_step_as_a_value_error():
    # Marvin's workflow engine turns only ValueError into a failed step; anything else escapes the run.
    with pytest.raises(ValueError, match="Buttondown lookup_subscriber failed: OSError: Network is unreachable"):
        _run(_Offline([]), "lookup_subscriber", subscriber=SUB_ID)


def test_declared_actions_match_the_handlers():
    assert {a.key for a in ButtondownProvider.actions} == {"subscribe", "lookup_subscriber", "create_issue_email"}


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
