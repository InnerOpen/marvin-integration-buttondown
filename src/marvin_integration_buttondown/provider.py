"""Buttondown provider: newsletter subscribers and issue emails.

The provider is pure with respect to Marvin: it gets ``config`` (issue delivery, site URL), the
resolved ``secret`` (a Buttondown API key), a ``logger`` and a safe ``http`` client via ``ctx``, and
returns dicts. Receiving Buttondown's webhooks (and verifying their signature) is the core's job —
this provider contributes the `buttondown` signature scheme and declares the webhook + workflows.
A provider never sees the workspace, so the site URL for an issue's links arrives as an action
argument (the declared workflow passes Marvin's ``${site.url}``) unless the connection sets its own.

Buttondown quirks this hides:
- A subscriber has two ids. The API returns ``sub_…``; webhooks carry a UUID. ``GET /subscribers/{x}``
  resolves either (and an email address), so ``lookup_subscriber`` turns a webhook UUID into the API id.
- ``POST /emails`` defaults to ``status: about_to_send`` — an email created without a status is sent.
  Every create here names its status explicitly.
- Signups can be refused by Buttondown's spam firewall (``400 subscriber_blocked``) or because the
  address unsubscribed before (``subscriber_suppressed``); both come back as readable errors.
- Re-subscribing an address that never confirmed (``unactivated``) is refused as "already exists" and
  sends nothing, so ``subscribe`` asks for a confirmation reminder (``POST /subscribers/{id}/send-reminder``).
- Webhooks are account-wide, and several Marvin workspaces can share one Buttondown account. Each
  workspace owns only the webhook pointing at its own hook URL; ``connect_webhooks`` never touches others.
"""

from __future__ import annotations

from typing import ClassVar
from urllib.parse import quote

from marvin_integration_sdk import (
    CATEGORY_DESTINATION,
    CredentialField,
    IntegrationContext,
    IntegrationProvider,
    ProviderAction,
    Response,
    register_provider,
)

from .content import CONTENT
from .links import absolutize_links, absolutize_url, has_relative_links, is_relative, normalise_base_url

API = "https://api.buttondown.com/v1"
ERROR_TEXT_LIMIT = 300

DELIVERY_OFF, DELIVERY_DRAFT, DELIVERY_SEND = "off", "draft", "send"
DELIVERY_STATUS = {DELIVERY_DRAFT: "draft", DELIVERY_SEND: "about_to_send"}
DEFAULT_DELIVERY = DELIVERY_DRAFT

# Marvin bodies are Markdown. Saying so stops Buttondown guessing "fancy" (HTML) from an inline tag.
EDITOR_MODE_MARKER = "<!-- buttondown-editor-mode: plaintext -->"
# The key the created email carries in Buttondown, so a lost `buttondown_email_id` can still be found.
ENTRY_METADATA_KEY = "marvin_entry_id"

ALREADY_SUBSCRIBED = {"email_already_exists", "subscriber_already_exists"}
UNACTIVATED = "unactivated"
FIREWALLED = {"subscriber_blocked", "email_blocked", "ip_address_spammy"}

_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_SUBSCRIBER_OUT = {
    "type": "object",
    "properties": {"subscriber_id": _STR, "email": _STR, "type": _STR, "already_subscribed": _BOOL},
}

# What the declared workflows react to — nothing else is worth a delivery.
WEBHOOK_EVENTS = ("subscriber.confirmed", "subscriber.unsubscribed")
WEBHOOK_DESCRIPTION = "Marvin: subscriber confirmations and unsubscribes"
WEBHOOK_ENABLED = "enabled"
# Marvin's incoming webhook URLs are https://<api>/api/hooks/<token>.
HOOK_PATH = "/api/hooks/"
# A cap on following `next` pages, so a misbehaving API can't loop the action forever.
MAX_WEBHOOK_PAGES = 20


def _error(resp: Response) -> tuple[str, str]:
    """(code, detail) from a Buttondown error body: ``{"code": …, "detail": …}`` or a validation list."""
    try:
        body = resp.json()
    except ValueError:
        return "", resp.text[:ERROR_TEXT_LIMIT]
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, list):  # 422: [{"loc": [...], "msg": "..."}]
            detail = "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
        return str(body.get("code") or ""), str(detail or resp.text[:ERROR_TEXT_LIMIT])
    return "", resp.text[:ERROR_TEXT_LIMIT]


def _fail(resp: Response, what: str) -> ValueError:
    code, detail = _error(resp)
    return ValueError(f"Buttondown {what} failed: HTTP {resp.status_code}{f' {code}' if code else ''}: {detail}")


def _same_url(a: str, b: str) -> bool:
    """The same hook URL, give or take surrounding blanks and a trailing slash — never a looser match."""
    return bool(a) and a.strip().rstrip("/") == b.strip().rstrip("/")


def _text(value) -> str:
    """A workflow argument as a string — an unresolved ``${…}`` template counts as blank."""
    text = str(value if value is not None else "").strip()
    return "" if text.startswith("${") or text in ("None", "null") else text


@register_provider
class ButtondownProvider(IntegrationProvider):
    slug = "buttondown"
    name = "Buttondown"
    description = (
        "Newsletter signups become Buttondown subscribers, confirmations and unsubscribes sync back to Marvin, "
        "and a published issue becomes a Buttondown draft — or is sent, if you choose."
    )
    category = CATEGORY_DESTINATION
    icon = "📮"

    content = CONTENT

    # Buttondown signs with HMAC-SHA256 over the raw body: `X-Buttondown-Signature: sha256=<hex>`.
    # Its dashboard's *Test webhook* button sends unsigned, so a test delivery is rejected — real events are signed.
    signature_schemes: ClassVar[dict[str, dict]] = {
        "buttondown": {
            "algorithm": "sha256",
            "encoding": "hex",
            "message": "{body}",
            "header": "X-Buttondown-Signature",
            "prefix": "sha256=",
            "notes": "Buttondown webhooks: HMAC-SHA256 of the raw body, hex, in X-Buttondown-Signature as sha256=<hex>",
        },
    }

    credentials = (
        CredentialField(
            key="api_key",
            label="API key",
            help="Your Buttondown API key (Settings → API). Store it as a workspace secret and reference it as {{BUTTONDOWN_API_KEY}}.",
        ),
    )
    config_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "issue_delivery": {
                "type": "string",
                "title": "Issue delivery",
                "enum": [DELIVERY_OFF, DELIVERY_DRAFT, DELIVERY_SEND],
                "default": DEFAULT_DELIVERY,
                "description": "What publishing a newsletter issue does in Buttondown: off (nothing), draft (create a draft to review and send there), "
                "or send (send it to your subscribers straight away).",
            },
            "site_url": {
                "type": "string",
                "title": "Site URL",
                "description": "Your public site's address, e.g. https://example.com. Relative links in an issue (/works/x) are made absolute "
                "against it so they work in the email. Blank = the workspace's Canonical URL (Settings → General).",
            },
        },
        "additionalProperties": False,
    }

    actions = (
        ProviderAction(
            key="subscribe",
            label="Subscribe",
            description=(
                "Add an email address to the newsletter. An address that is already subscribed returns its existing subscriber; "
                "one that never confirmed (unactivated) is sent a fresh confirmation email."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "email": _STR,
                    "tags": {"type": "array", "items": _STR, "description": "Buttondown tags, created if missing."},
                    "ip_address": {"type": "string", "description": "The visitor's IP — helps Buttondown's spam firewall judge the signup."},
                    "metadata": {"type": "object", "description": "Stored on the Buttondown subscriber."},
                    "notes": _STR,
                    "referrer_url": _STR,
                },
                "required": ["email"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {**_SUBSCRIBER_OUT["properties"], "confirmation_resent": _BOOL, "confirmation_reason": _STR},
            },
        ),
        ProviderAction(
            key="lookup_subscriber",
            label="Look up subscriber",
            description="Find a subscriber by its webhook UUID, its sub_ id or its email address: returns the sub_ id, email and type.",
            input_schema={
                "type": "object",
                "properties": {"subscriber": {"type": "string", "description": "A webhook's data.subscriber UUID, a sub_ id, or an email."}},
                "required": ["subscriber"],
                "additionalProperties": False,
            },
            output_schema=_SUBSCRIBER_OUT,
            cost_hint="free",
        ),
        ProviderAction(
            key="create_issue_email",
            label="Create issue email",
            description=(
                "Turn a newsletter issue into a Buttondown email, as the connection's Issue delivery says: off (skip), draft, or send. "
                "Once per entry: an email already created for it is returned, not duplicated."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "subject": _STR,
                    "body": {"type": "string", "description": "The issue in Markdown. Relative links are made absolute against the site URL."},
                    "description": {"type": "string", "description": "The preview text / archive description."},
                    "canonical_url": {
                        "type": "string",
                        "description": "The issue's page on your site (the workflow passes ${entry.url}); a site path is made absolute against the site URL.",
                    },
                    "entry_id": {"type": "string", "description": "The Marvin entry — recorded on the email so it is created only once."},
                    "email_id": {
                        "type": "string",
                        "description": "The email already created for this entry, if any (pass the entry's buttondown_email_id).",
                    },
                    "site_url": {
                        "type": "string",
                        "description": "The workspace's site address (the workflow passes ${site.url}). The connection's Site URL, when set, wins.",
                    },
                },
                "required": ["subject", "body"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"email_id": _STR, "status": _STR, "delivery": _STR, "skipped": {"type": "boolean"}, "reason": _STR, "url": _STR},
            },
        ),
        ProviderAction(
            key="connect_webhooks",
            label="Connect Buttondown webhooks",
            description=(
                "Create (or update) the one Buttondown webhook that posts this workspace's subscriber confirmations and unsubscribes "
                "to Marvin, signed with the given key. Webhooks pointing anywhere else are left alone; optionally deletes the webhook "
                "for an old Marvin hook URL you are retiring."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "webhook_url": {
                        "type": "string",
                        "title": "Webhook URL",
                        "description": "This workspace's buttondown incoming webhook URL (https://…/api/hooks/<token>).",
                    },
                    "signing_key": {
                        "type": "string",
                        "title": "Signing key",
                        "description": "The key Buttondown signs with — pass {{BUTTONDOWN_SIGNING_KEY}} (the webhook's signing secret).",
                    },
                    "remove_legacy_url": {
                        "type": "string",
                        "title": "Old hook URL to retire",
                        "description": "Optional: an old Marvin hook URL. A Buttondown webhook pointing at exactly this URL is deleted.",
                    },
                    "label": {
                        "type": "string",
                        "title": "Label",
                        "description": "Optional: shown in Buttondown's webhook description, e.g. the workspace name.",
                    },
                },
                "required": ["webhook_url", "signing_key"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "webhook_id": _STR,
                    "result": {"type": "string", "enum": ["created", "updated", "replaced", "unchanged"]},
                    "created": _BOOL,
                    "event_types": {"type": "array", "items": _STR},
                    "removed": {"type": "array", "items": _STR},
                    "legacy_removed": _BOOL,
                },
            },
        ),
    )

    # ---- plumbing ---------------------------------------------------------------------------

    @staticmethod
    def _headers(ctx: IntegrationContext, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {"Authorization": f"Token {ctx.secret}", "Content-Type": "application/json", **(extra or {})}

    def _get(self, ctx: IntegrationContext, path: str) -> Response:
        return ctx.http.get(f"{API}{path}", headers=self._headers(ctx))

    @staticmethod
    def _delivery(ctx: IntegrationContext) -> str:
        value = str((ctx.config or {}).get("issue_delivery") or DEFAULT_DELIVERY).strip().lower()
        if value not in (DELIVERY_OFF, DELIVERY_DRAFT, DELIVERY_SEND):
            raise ValueError(f"Issue delivery must be off, draft or send, not {value!r}.")
        return value

    @staticmethod
    def _subscriber(data: dict, already: bool = False) -> dict:
        return {
            "subscriber_id": data.get("id") or "",
            "email": data.get("email_address") or "",
            "type": data.get("type") or "",
            "already_subscribed": already,
        }

    def _fetch_subscriber(self, ctx: IntegrationContext, key: str) -> dict | None:
        resp = self._get(ctx, f"/subscribers/{quote(key, safe='@')}")
        if resp.status_code == 404:
            return None
        if not resp.ok:
            raise _fail(resp, "subscriber lookup")
        return resp.json() or {}

    # ---- lifecycle --------------------------------------------------------------------------

    def check(self, ctx: IntegrationContext) -> tuple[str, str | None]:
        if not ctx.secret:
            return ("unconfigured", "Missing API key.")
        try:
            self._delivery(ctx)
            normalise_base_url((ctx.config or {}).get("site_url"))
            resp = self._get(ctx, "/ping")
        except Exception as e:  # noqa: BLE001 — reported on the card, never raised
            return ("error", str(e))
        if resp.status_code in (401, 403):
            return ("error", f"Buttondown rejected the API key (HTTP {resp.status_code}): {_error(resp)[1]}")
        if not resp.ok:
            return ("error", str(_fail(resp, "API check")))
        return ("ok", None)

    def run_action(self, key: str, args: dict, ctx: IntegrationContext) -> dict:
        handler = {
            "subscribe": self._subscribe,
            "lookup_subscriber": self._lookup_subscriber,
            "create_issue_email": self._create_issue_email,
            "connect_webhooks": self._connect_webhooks,
        }.get(key)
        if handler is None:
            raise NotImplementedError(f"buttondown has no action '{key}'")
        if not ctx.secret:
            raise ValueError("No Buttondown API key configured.")
        try:
            return handler(args or {}, ctx)
        except (ValueError, NotImplementedError):
            raise
        except Exception as e:  # a network error must fail the step, not escape the workflow engine
            raise ValueError(f"Buttondown {key} failed: {type(e).__name__}: {e}") from e

    # ---- actions ----------------------------------------------------------------------------

    def _subscribe(self, args: dict, ctx: IntegrationContext) -> dict:
        email = _text(args.get("email"))
        if "@" not in email:
            raise ValueError(f"subscribe needs an email address, got {email!r}.")
        body: dict = {"email_address": email}
        tags = [t for t in (_text(t) for t in (args.get("tags") or [])) if t]
        if tags:
            body["tags"] = tags
        for field in ("ip_address", "notes", "referrer_url"):
            if value := _text(args.get(field)):
                body[field] = value
        if isinstance(args.get("metadata"), dict) and args["metadata"]:
            body["metadata"] = args["metadata"]

        resp = ctx.http.post(f"{API}/subscribers", json=body, headers=self._headers(ctx))
        if resp.ok:
            return self._subscriber(resp.json() or {})
        code, detail = _error(resp)
        if code in ALREADY_SUBSCRIBED:
            existing = self._fetch_subscriber(ctx, email)
            if existing:
                out = self._subscriber(existing, already=True)
                if out["type"] == UNACTIVATED:
                    out.update(self._resend_confirmation(ctx, out["subscriber_id"] or email))
                return out
        if code in FIREWALLED:
            raise ValueError(f"Buttondown's spam firewall refused {email} ({code}): {detail}")
        if code == "subscriber_suppressed":
            raise ValueError(f"{email} unsubscribed from this newsletter before, so Buttondown won't re-add it from a signup ({code}): {detail}")
        raise _fail(resp, "subscribe")

    def _resend_confirmation(self, ctx: IntegrationContext, key: str) -> dict:
        """Ask Buttondown to re-send the confirmation email. Never fails the signup: a refusal (rate limit, error) is reported."""
        try:
            resp = ctx.http.post(f"{API}/subscribers/{quote(key, safe='@')}/send-reminder", json={}, headers=self._headers(ctx))
        except Exception as e:  # noqa: BLE001 — the signup itself succeeded; a missed reminder is not worth failing it
            reason = f"{type(e).__name__}: {e}"
        else:
            if resp.ok:
                return {"confirmation_resent": True}
            code, detail = _error(resp)
            reason = f"HTTP {resp.status_code}{f' {code}' if code else ''}: {detail}"
        ctx.logger.warning("buttondown: could not re-send the confirmation email to %s — %s", key, reason)
        return {"confirmation_resent": False, "confirmation_reason": reason}

    def _lookup_subscriber(self, args: dict, ctx: IntegrationContext) -> dict:
        key = _text(args.get("subscriber"))
        if not key:
            raise ValueError("lookup_subscriber needs a subscriber id (the webhook's data.subscriber), a sub_ id or an email.")
        data = self._fetch_subscriber(ctx, key)
        if data is None:
            raise ValueError(f"No Buttondown subscriber {key!r}.")
        return self._subscriber(data)

    def _existing_email(self, ctx: IntegrationContext, email_id: str, entry_id: str) -> dict | None:
        """The email already created for this entry: by its recorded id, else by the entry id it carries."""
        if email_id:
            resp = self._get(ctx, f"/emails/{quote(email_id, safe='')}")
            if resp.ok:
                found = resp.json() or {}
                if found.get("status") != "deleted":
                    return found
            elif resp.status_code != 404:
                raise _fail(resp, "email lookup")
        if entry_id:
            resp = self._get(ctx, "/emails?ordering=-creation_date&excluded_fields=body")
            if not resp.ok:
                raise _fail(resp, "list emails")
            for found in (resp.json() or {}).get("results") or []:
                if str((found.get("metadata") or {}).get(ENTRY_METADATA_KEY) or "") == entry_id and found.get("status") != "deleted":
                    return found
        return None

    @staticmethod
    def _base_url(args: dict, ctx: IntegrationContext) -> str:
        """The connection's Site URL if set (a deliberate choice), else the workspace's, as the workflow passed it."""
        return normalise_base_url(_text((ctx.config or {}).get("site_url")) or _text(args.get("site_url")))

    @staticmethod
    def _canonical_url(args: dict, base: str) -> str:
        """The issue's own page as an absolute URL, or "" — Buttondown refuses a relative canonical_url."""
        url = _text(args.get("canonical_url"))
        if url and is_relative(url):
            url = absolutize_url(url, base) if base else ""
        return url if url.startswith(("https://", "http://")) else ""

    def _create_issue_email(self, args: dict, ctx: IntegrationContext) -> dict:
        delivery = self._delivery(ctx)
        email_id, entry_id = _text(args.get("email_id")), _text(args.get("entry_id"))
        if delivery == DELIVERY_OFF:
            return {"email_id": email_id, "status": "", "delivery": delivery, "skipped": True, "reason": "Issue delivery is off.", "url": ""}

        existing = self._existing_email(ctx, email_id, entry_id)
        if existing:
            return {
                "email_id": existing.get("id") or email_id,
                "status": existing.get("status") or "",
                "delivery": delivery,
                "skipped": True,
                "reason": "This issue already has a Buttondown email.",
                "url": existing.get("absolute_url") or "",
            }

        subject, body = _text(args.get("subject")), str(args.get("body") or "")
        if not subject:
            raise ValueError("create_issue_email needs a subject.")
        base = self._base_url(args, ctx)
        if base:
            body = absolutize_links(body, base)
        elif has_relative_links(body):
            ctx.logger.warning(
                "buttondown: issue %s has relative links and no site URL is set — they will not work in the email", entry_id or subject
            )

        payload: dict = {
            "subject": subject,
            "body": body if "buttondown-editor-mode" in body else f"{EDITOR_MODE_MARKER}\n{body}",
            "status": DELIVERY_STATUS[delivery],
        }
        if description := _text(args.get("description")):
            payload["description"] = description
        if canonical := self._canonical_url(args, base):
            payload["canonical_url"] = canonical
        if entry_id:
            payload["metadata"] = {ENTRY_METADATA_KEY: entry_id}

        # Buttondown refuses a body that opens with `---` as leaked frontmatter unless told it's meant.
        extra = {"X-Buttondown-Live-Dangerously": "true"} if body.lstrip().startswith("---") else None
        resp = ctx.http.post(f"{API}/emails", json=payload, headers=self._headers(ctx, extra))
        if not resp.ok:
            code, detail = _error(resp)
            if code == "sending_requires_confirmation":
                raise ValueError(f"Buttondown needs this newsletter's sending confirmed before the API can send ({code}): {detail}")
            raise _fail(resp, "create email")
        created = resp.json() or {}
        return {
            "email_id": created.get("id") or "",
            "status": created.get("status") or payload["status"],
            "delivery": delivery,
            "skipped": False,
            "reason": "",
            "url": created.get("absolute_url") or "",
        }

    # ---- webhooks ---------------------------------------------------------------------------

    @staticmethod
    def _hook_url(value, arg: str) -> str:
        url = _text(value)
        if not url.startswith("https://") or HOOK_PATH not in url or not url.split(HOOK_PATH, 1)[1].strip("/"):
            raise ValueError(f"{arg} must be a Marvin incoming webhook URL (https://…{HOOK_PATH}<token>), got {url!r}.")
        return url

    def _list_webhooks(self, ctx: IntegrationContext) -> list[dict]:
        webhooks: list[dict] = []
        url = f"{API}/webhooks"
        for _ in range(MAX_WEBHOOK_PAGES):
            resp = ctx.http.get(url, headers=self._headers(ctx))
            if not resp.ok:
                raise _fail(resp, "list webhooks")
            page = resp.json() or {}
            webhooks.extend(page.get("results") or [])
            url = str(page.get("next") or "")
            if not url.startswith(API):  # no next page (or one somewhere we never send the key)
                break
        return webhooks

    def _send_webhook(self, ctx: IntegrationContext, method: str, path: str, body: dict, what: str) -> dict:
        resp = getattr(ctx.http, method)(f"{API}{path}", json=body, headers=self._headers(ctx))
        if not resp.ok:
            raise _fail(resp, what)
        return resp.json() or {}

    def _delete_webhook(self, ctx: IntegrationContext, webhook_id: str) -> str:
        resp = ctx.http.delete(f"{API}/webhooks/{quote(webhook_id, safe='')}", headers=self._headers(ctx))
        if not resp.ok and resp.status_code != 404:  # already gone is as good as deleted
            raise _fail(resp, "delete webhook")
        return webhook_id

    @staticmethod
    def _webhook_matches(existing: dict, desired: dict) -> bool:
        return (
            existing.get("status") == desired["status"]
            and sorted(existing.get("event_types") or []) == sorted(desired["event_types"])
            and (existing.get("description") or "") == desired["description"]
            and (existing.get("signing_key") or "") == desired["signing_key"]  # a masked key never matches, so it is re-set
        )

    def _upsert_webhook(self, ctx: IntegrationContext, existing: dict | None, desired: dict) -> dict:
        if existing is None:
            created = self._send_webhook(ctx, "post", "/webhooks", desired, "create webhook")
            return {"webhook_id": created.get("id") or "", "result": "created"}
        webhook_id = str(existing.get("id") or "")
        if self._webhook_matches(existing, desired):
            return {"webhook_id": webhook_id, "result": "unchanged"}
        if callable(getattr(ctx.http, "patch", None)):
            self._send_webhook(ctx, "patch", f"/webhooks/{quote(webhook_id, safe='')}", desired, "update webhook")
            return {"webhook_id": webhook_id, "result": "updated"}
        # Buttondown updates only by PATCH, which this Marvin's http helper lacks: replace it, new one first so no event is missed.
        created = self._send_webhook(ctx, "post", "/webhooks", desired, "create webhook")
        self._delete_webhook(ctx, webhook_id)
        return {"webhook_id": created.get("id") or "", "result": "replaced"}

    def _connect_webhooks(self, args: dict, ctx: IntegrationContext) -> dict:
        url = self._hook_url(args.get("webhook_url"), "webhook_url")
        signing_key = _text(args.get("signing_key"))
        if not signing_key or signing_key.startswith("{{"):
            raise ValueError("signing_key is required — pass {{BUTTONDOWN_SIGNING_KEY}} (the buttondown webhook's signing secret).")
        legacy = self._hook_url(args.get("remove_legacy_url"), "remove_legacy_url") if _text(args.get("remove_legacy_url")) else ""
        if legacy and _same_url(legacy, url):
            raise ValueError("remove_legacy_url is the webhook URL being connected — pass the old hook's URL, or leave it blank.")
        label = _text(args.get("label"))
        desired = {
            "url": url,
            "event_types": list(WEBHOOK_EVENTS),
            "status": WEBHOOK_ENABLED,
            "description": f"{WEBHOOK_DESCRIPTION} ({label})" if label else WEBHOOK_DESCRIPTION,
            "signing_key": signing_key,
        }

        webhooks = [w for w in self._list_webhooks(ctx) if w.get("id")]
        ours = [w for w in webhooks if _same_url(str(w.get("url") or ""), url)]
        result = self._upsert_webhook(ctx, ours[0] if ours else None, desired)
        # Extra webhooks on our own URL would deliver every event twice.
        removed = [self._delete_webhook(ctx, str(w.get("id") or "")) for w in ours[1:]]
        retired = [self._delete_webhook(ctx, str(w.get("id") or "")) for w in webhooks if legacy and _same_url(str(w.get("url") or ""), legacy)]
        return {
            **result,
            "created": result["result"] == "created",
            "event_types": list(WEBHOOK_EVENTS),
            "removed": removed + retired,
            "legacy_removed": bool(retired),
        }
