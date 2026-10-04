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
FIREWALLED = {"subscriber_blocked", "email_blocked", "ip_address_spammy"}

_STR = {"type": "string"}
_SUBSCRIBER_OUT = {
    "type": "object",
    "properties": {"subscriber_id": _STR, "email": _STR, "type": _STR, "already_subscribed": {"type": "boolean"}},
}


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
            description="Add an email address to the newsletter. An address that is already subscribed returns its existing subscriber.",
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
            output_schema=_SUBSCRIBER_OUT,
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
                return self._subscriber(existing, already=True)
        if code in FIREWALLED:
            raise ValueError(f"Buttondown's spam firewall refused {email} ({code}): {detail}")
        if code == "subscriber_suppressed":
            raise ValueError(f"{email} unsubscribed from this newsletter before, so Buttondown won't re-add it from a signup ({code}): {detail}")
        raise _fail(resp, "subscribe")

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
