"""What a workspace needs to run its newsletter through Buttondown — declared for the core to offer.

Applying (from the integration's card) creates only what is missing: the incoming webhook Buttondown
posts subscriber events to, four workflows, and two optional private smart collections (confirmed
and unsubscribed readers). Webhooks and workflows arrive switched off; turning them on after a
review is the deliberate last step — and each can stay off on its own.

The loop (Marvin is the list of record; a signup entry's status mirrors Buttondown):
  a site signup (a `newsletter` form submission, not flagged as spam) → `subscribe` → the subscriber's
  API id stored on the entry as `buttondown_subscriber_id` (the entry waits in the inbox, pending)
  → the reader confirms → Buttondown posts `subscriber.confirmed` → `lookup_subscriber` turns the
  webhook's UUID into the API id → the entry with that id is published. `subscriber.unsubscribed`
  archives it the same way. A reader with no signup entry here (subscribed elsewhere) is skipped.
  One entry per reader: set the signup type's **Same person = same …** to its email field
  (`capabilities.submission.matchField: "email"`, Marvin's one-entry-per-person setting). A repeat signup
  then updates the reader's entry instead of adding a second one, and the signup workflow skips a repeat
  from a reader already confirmed (previous status `published`) — no second subscribe for someone on the
  list. A repeat that reopened an unsubscribed (archived) entry, or one still pending, subscribes again;
  Buttondown answers that idempotently.
  A signup Buttondown refuses (spam firewall, an earlier unsubscribe) or that fails otherwise goes to
  Needs review instead of waiting in the inbox like a pending one. That is the provider's error policy
  (see `ButtondownProvider.error_policy`), applied by Marvin, not workflow steps: Marvin records the
  failure on the entry as `integration_error.buttondown` and the message as its review reason, and
  retries a Buttondown outage or rate limit first.
  Publishing a `newsletter-issue` entry → `create_issue_email` (draft by default; see the connection's
  Issue delivery; the entry's page as its canonical URL) → the email's id stored on the entry as
  `buttondown_email_id`, so a republish never makes a second email.

Apply can't set the match field (a blueprint can add fields to an entry type, not change its submission
settings), so it is a suggestion: the signup-type parameter's help and the signup workflow say so.
Without it every signup is its own entry, as before, and the duplicate condition never matches.

Needs Marvin rc.177+ for `${site.url}`, and the release after it for `${entry.url}` and the entry
step's `if_none: skip` (on an older Marvin both are ignored: no canonical URL, and a reader with no
signup entry fails the step). The error policy needs a Marvin that reads SDK 0.5 policies; an older
one ignores it (the run fails and the entry stays in the inbox, as before).

The collections need no workflow: a signup's status already says where it stands (published =
confirmed, archived = unsubscribed), so each is a smart collection over the signup type and one
status, and membership follows the entry. Both are private (not "Visible to sites"), so changes to
them never rebuild a site and a site can't read the list. Apply never overwrites: a workspace that
already has a collection with either slug keeps it as it is (switch it to Smart by hand).

Parameters: `integration` (this integration's slug in the workspace, default `buttondown`),
`signup_type` (the signup form's entry type, default `newsletter`) and `issue_type` (the issue
entry type, default `newsletter-issue`, whose `body` and `preview` fields make the email).
"""

from marvin_integration_sdk import ContentBlueprint

CATEGORY = "Buttondown"
WEBHOOK_SLUG = "buttondown"
SECRET_REF = "BUTTONDOWN_SIGNING_KEY"

INTEGRATION_PARAM = {
    "key": "integration",
    "label": "Which Buttondown connection",
    "kind": "integration",
    "default": "buttondown",
    "help": "The workflows call this connection's actions.",
}
# The signup field that identifies a reader: suggested as the signup type's match field (one entry per reader).
SIGNUP_MATCH_FIELD = "email"

SIGNUP_TYPE_PARAM = {
    "key": "signup_type",
    "label": "Which entry type are newsletter signups?",
    "kind": "entry_type",
    "default": "newsletter",
    "help": (
        "The submittable type your site's signup form creates entries of. Suggested: set its Submission settings → "
        f"Same person = same … to `{SIGNUP_MATCH_FIELD}`, so a repeat signup updates the reader's entry instead of adding a second one."
    ),
}
ISSUE_TYPE_PARAM = {
    "key": "issue_type",
    "label": "Which entry type are newsletter issues?",
    "kind": "entry_type",
    "default": "newsletter-issue",
    "help": "Publishing one of these creates the Buttondown email (its body and preview fields).",
}


SUBSCRIBER_ID_KEY = "buttondown_subscriber_id"
EMAIL_ID_KEY = "buttondown_email_id"
DELIVERY_KEY = "buttondown_issue_delivery"

EVENTS_WEBHOOK = ContentBlueprint(
    kind="incoming_webhook",
    slug=WEBHOOK_SLUG,
    name="Buttondown events",
    description=(
        f"Where Buttondown posts subscriber events. Mint its token, generate a key under Signing (stored as {SECRET_REF}), "
        "then run the connection's Connect Buttondown webhooks action with this webhook's URL — it creates the Buttondown side."
    ),
    required=True,
    category=CATEGORY,
    payload={
        "name": "Buttondown events",
        "description": "Buttondown webhook: subscriber.confirmed, subscriber.unsubscribed. Signed (X-Buttondown-Signature).",
        "signature_scheme": "buttondown",
        "signing_secret_ref": SECRET_REF,
    },
)

SUBSCRIBE_ON_SIGNUP = ContentBlueprint(
    kind="workflow",
    slug="buttondown-subscribe-on-signup",
    name="Buttondown: subscribe on signup",
    description=(
        "When the site's newsletter form is submitted (and not flagged as spam), add the address to Buttondown and remember its subscriber id "
        "on the entry. If Buttondown refuses it, the entry goes to Needs review with the reason. A repeat signup from a reader "
        f"already confirmed is skipped — set the signup type's Same person = same … to {SIGNUP_MATCH_FIELD} so a repeat updates their entry."
    ),
    required=True,
    category=CATEGORY,
    parameters=(SIGNUP_TYPE_PARAM, INTEGRATION_PARAM),
    payload={
        "definition": {
            "trigger": {"type": "event", "event": "form_submission_received"},
            "conditions": [
                {"field": "entry.entry_type", "op": "eq", "value": "{{signup_type}}"},
                # A flagged signup (disposable domain etc.) stays in Needs review and is never forwarded.
                {"field": "event.flagged", "op": "neq", "value": True},
                # One entry per reader (the signup type's match field): a repeat from someone already confirmed
                # (published) is skipped. A reopened (archived → inbox) or still-pending one subscribes again, which
                # Buttondown answers idempotently. Not a duplicate, or a Marvin without the field: no previous status.
                {"field": "event.previous_status", "op": "neq", "value": "published"},
            ],
            "actions": [
                {
                    "kind": "integration",
                    "id": "subscribe",
                    "integration": "{{integration}}",
                    "action": "subscribe",
                    "args": {
                        "email": "${event.submission_data.email}",
                        "tags": ["website"],
                        "ip_address": "${event.ip_address}",
                    },
                },
                {"kind": "entry", "op": "set_metadata", "metadata": {SUBSCRIBER_ID_KEY: "${steps.subscribe.output.subscriber_id}"}},
            ],
            # No on_failure: a refused or failed subscribe is handled by the provider's error policy (review,
            # after retries for an outage or rate limit). A copy applied before 0.4.0 keeps its on_failure
            # steps, which Marvin runs instead of the policy — an override, still correct.
        }
    },
)


def _subscriber_event(slug: str, name: str, description: str, event_type: str, step: dict) -> ContentBlueprint:
    """A Buttondown subscriber event → ``step`` on the signup entry with that subscriber's API id.

    Webhooks carry a UUID the API never returns, so the entry can't be matched on it directly:
    `lookup_subscriber` resolves it to the `sub_` id the signup workflow stored. No matching entry
    (a reader who subscribed outside the site) ends the step quietly; two (repeat signups from before the
    signup type had a match field) fail it."""
    return ContentBlueprint(
        kind="workflow",
        slug=slug,
        name=name,
        description=description,
        required=True,
        category=CATEGORY,
        parameters=(SIGNUP_TYPE_PARAM, INTEGRATION_PARAM),
        payload={
            "definition": {
                "trigger": {"type": "incoming_webhook", "webhook": WEBHOOK_SLUG},
                "conditions": [
                    {"field": "event.payload.event_type", "op": "eq", "value": event_type},
                    {"field": "event.payload.data.subscriber", "op": "exists"},
                ],
                "actions": [
                    {
                        "kind": "integration",
                        "id": "lookup",
                        "integration": "{{integration}}",
                        "action": "lookup_subscriber",
                        "args": {"subscriber": "${event.payload.data.subscriber}"},
                    },
                    {
                        **step,
                        "kind": "entry",
                        # A reader who joined some other way has no signup entry here: a quiet no-op, not a failed run.
                        "if_none": "skip",
                        "entity_query": {
                            "entry_type": "{{signup_type}}",
                            "metadata": {SUBSCRIBER_ID_KEY: "${steps.lookup.output.subscriber_id}"},
                        },
                    },
                ],
            }
        },
    )


CONFIRMED = _subscriber_event(
    "buttondown-subscriber-confirmed",
    "Buttondown: subscriber confirmed",
    "When a reader confirms their subscription in Buttondown, publish their signup entry (confirmed = published).",
    "subscriber.confirmed",
    {"op": "publish"},
)

UNSUBSCRIBED = _subscriber_event(
    "buttondown-subscriber-unsubscribed",
    "Buttondown: subscriber unsubscribed",
    "When a reader unsubscribes in Buttondown, archive their signup entry (unsubscribed = archived).",
    "subscriber.unsubscribed",
    {"op": "archive"},
)

ISSUE_ON_PUBLISH = ContentBlueprint(
    kind="workflow",
    slug="buttondown-issue-on-publish",
    name="Buttondown: email an issue when published",
    description=(
        "When a newsletter issue is published, create its Buttondown email — a draft, or sent, as the connection's Issue delivery says — "
        "and remember the email's id on the entry, so publishing it again never makes a second one."
    ),
    required=True,
    category=CATEGORY,
    parameters=(ISSUE_TYPE_PARAM, INTEGRATION_PARAM),
    payload={
        "definition": {
            "trigger": {"type": "event", "event": "entry_published"},
            "conditions": [{"field": "entry.entry_type", "op": "eq", "value": "{{issue_type}}"}],
            "actions": [
                {
                    "kind": "integration",
                    "id": "issue",
                    "integration": "{{integration}}",
                    "action": "create_issue_email",
                    "args": {
                        "subject": "${entry.title}",
                        "body": "${entry.data.body}",
                        "description": "${entry.data.preview}",
                        "canonical_url": "${entry.url}",
                        "entry_id": "${entry.id}",
                        "email_id": f"${{entry.metadata.{EMAIL_ID_KEY}}}",
                        # Blank when the workspace has no Canonical URL (or Marvin predates ${site.url}).
                        "site_url": "${site.url}",
                    },
                },
                # The delivery is never blank, so this step still succeeds when delivery is off and there is no email id
                # (set_metadata drops blank values and fails when nothing is left).
                {
                    "kind": "entry",
                    "op": "set_metadata",
                    "metadata": {EMAIL_ID_KEY: "${steps.issue.output.email_id}", DELIVERY_KEY: "${steps.issue.output.delivery}"},
                },
            ],
        }
    },
)


def _signups_with_status(slug: str, name: str, description: str, status: str, icon: str) -> ContentBlueprint:
    """A private smart collection of the signup entries in one status.

    Optional (a suggestion): the loop works without it. Private so membership changes — every
    confirm and unsubscribe — never request a site rebuild, and the publishing API never lists readers."""
    return ContentBlueprint(
        kind="collection",
        slug=slug,
        name=name,
        description=description,
        category=CATEGORY,
        parameters=(SIGNUP_TYPE_PARAM,),
        payload={
            "description": description,
            "icon": icon,
            "is_smart": True,
            "is_public": False,
            "smart_rules": {"entry_types": ["{{signup_type}}"], "statuses": [status], "match": "all"},
        },
    )


CONFIRMED_SUBSCRIBERS = _signups_with_status(
    "confirmed-subscribers",
    "Confirmed subscribers",
    "Signups confirmed in Buttondown (published). Fills itself as readers confirm; private, so sites never see it.",
    "published",
    "📬",
)

UNSUBSCRIBED_READERS = _signups_with_status(
    "unsubscribed",
    "Unsubscribed",
    "Signups that unsubscribed in Buttondown (archived). Fills itself as readers leave; private, so sites never see it.",
    "archived",
    "📭",
)

CONTENT = (EVENTS_WEBHOOK, SUBSCRIBE_ON_SIGNUP, CONFIRMED, UNSUBSCRIBED, ISSUE_ON_PUBLISH, CONFIRMED_SUBSCRIBERS, UNSUBSCRIBED_READERS)
