"""What a workspace needs to run its newsletter through Buttondown — declared for the core to offer.

Applying (from the integration's card) creates only what is missing: the incoming webhook Buttondown
posts subscriber events to, and four workflows. Webhooks and workflows arrive switched off; turning
them on after a review is the deliberate last step — and each can stay off on its own.

The loop (Marvin is the list of record; a signup entry's status mirrors Buttondown):
  a site signup (a `newsletter` form submission, not flagged as spam) → `subscribe` → the subscriber's
  API id stored on the entry as `buttondown_subscriber_id` (the entry waits in the inbox, pending)
  → the reader confirms → Buttondown posts `subscriber.confirmed` → `lookup_subscriber` turns the
  webhook's UUID into the API id → the entry with that id is published. `subscriber.unsubscribed`
  archives it the same way.
  Publishing a `newsletter-issue` entry → `create_issue_email` (draft by default; see the connection's
  Issue delivery) → the email's id stored on the entry as `buttondown_email_id`, so a republish
  never makes a second email.

Three parameters: `integration` (this integration's slug in the workspace, default `buttondown`),
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
SIGNUP_TYPE_PARAM = {
    "key": "signup_type",
    "label": "Which entry type are newsletter signups?",
    "kind": "entry_type",
    "default": "newsletter",
    "help": "The submittable type your site's signup form creates entries of.",
}
ISSUE_TYPE_PARAM = {
    "key": "issue_type",
    "label": "Which entry type are newsletter issues?",
    "kind": "entry_type",
    "default": "newsletter-issue",
    "help": "Publishing one of these creates the Buttondown email (its body and preview fields).",
}

COLLECTION_PARAM = {
    "key": "collection",
    "label": "Which collection holds confirmed readers?",
    "kind": "collection",
    "default": "confirmed-subscribers",
    "help": "Create it first; confirmed readers are added, unsubscribed ones removed.",
}

SUBSCRIBER_ID_KEY = "buttondown_subscriber_id"
EMAIL_ID_KEY = "buttondown_email_id"
DELIVERY_KEY = "buttondown_issue_delivery"

EVENTS_WEBHOOK = ContentBlueprint(
    kind="incoming_webhook",
    slug=WEBHOOK_SLUG,
    name="Buttondown events",
    description=(
        "Where Buttondown posts subscriber events. Mint its token, paste the URL into Buttondown → Settings → Webhooks "
        f"(subscriber.confirmed and subscriber.unsubscribed), and store that webhook's signing key here as {SECRET_REF}."
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
    description="When the site's newsletter form is submitted (and not flagged as spam), add the address to Buttondown and remember its subscriber id on the entry.",
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
        }
    },
)


def _subscriber_event(
    slug: str, name: str, description: str, event_type: str, step: dict, *, required: bool = True, extra_params: tuple = ()
) -> ContentBlueprint:
    """A Buttondown subscriber event → ``step`` on the signup entry with that subscriber's API id.

    Webhooks carry a UUID the API never returns, so the entry can't be matched on it directly:
    `lookup_subscriber` resolves it to the `sub_` id the signup workflow stored."""
    return ContentBlueprint(
        kind="workflow",
        slug=slug,
        name=name,
        description=description,
        required=required,
        category=CATEGORY,
        parameters=(SIGNUP_TYPE_PARAM, INTEGRATION_PARAM, *extra_params),
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

# Optional: keep a collection of confirmed readers in step with Buttondown. Separate workflows, so the
# core loop needs no collection and a workspace without one simply doesn't apply these.
CONFIRMED_COLLECTION = _subscriber_event(
    "buttondown-confirmed-to-collection",
    "Buttondown: add confirmed readers to a collection",
    "When a reader confirms, add their signup entry to a collection (e.g. confirmed-subscribers).",
    "subscriber.confirmed",
    {"op": "add_to_collection", "collection_slug": "{{collection}}"},
    required=False,
    extra_params=(COLLECTION_PARAM,),
)

UNSUBSCRIBED_COLLECTION = _subscriber_event(
    "buttondown-unsubscribed-from-collection",
    "Buttondown: remove unsubscribed readers from the collection",
    "When a reader unsubscribes, take their signup entry out of the confirmed readers' collection.",
    "subscriber.unsubscribed",
    {"op": "remove_from_collection", "collection_slug": "{{collection}}"},
    required=False,
    extra_params=(COLLECTION_PARAM,),
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

CONTENT = (EVENTS_WEBHOOK, SUBSCRIBE_ON_SIGNUP, CONFIRMED, UNSUBSCRIBED, ISSUE_ON_PUBLISH, CONFIRMED_COLLECTION, UNSUBSCRIBED_COLLECTION)
