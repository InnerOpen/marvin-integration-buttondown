"""Workflow Library recipes Buttondown ships (SDK 0.9.0): offered in Marvin once a Buttondown connection exists.

Marvin's own library already has ``newsletter-delivery`` (a published issue becomes a Buttondown email), and the
integration's content blueprints cover the signup form itself. The gap is the other forms: a contact or event form with
an "add me to the newsletter" box.
"""

from marvin_integration_sdk import WorkflowRecipe

SUBSCRIBE_ON_OPT_IN = WorkflowRecipe(
    id="subscribe-on-opt-in",
    title="Subscribe form submitters who opt in",
    outcome=(
        "When a form other than the newsletter signup is submitted with its opt-in box ticked, the sender's address is "
        "added to Buttondown, which emails them to confirm."
    ),
    category="Newsletter",
    tags=("newsletter", "forms"),
    definition={
        # A refused subscribe fails the run (and its alert fires) instead of the provider's default of sending the contact
        # message itself to Needs review: the message is fine, only the newsletter signup didn't happen.
        "integration_errors": "fail",
        "trigger": {"type": "event", "event": "form_submission_received"},
        "conditions": [
            {"field": "entry.entry_type", "op": "eq", "value": "{{form_entry_type}}"},
            # A flagged submission (disposable domain etc.) stays in Needs review and is never forwarded.
            {"field": "event.flagged", "op": "neq", "value": True},
            {"field": "event.submission_data.{{optin_field}}", "op": "eq", "value": True},
        ],
        "actions": [
            {
                "kind": "integration",
                "id": "subscribe",
                "integration": "{{buttondown_integration}}",
                "action": "subscribe",
                "args": {
                    "email": "${event.submission_data.{{email_field}}}",
                    "tags": ["{{tag}}"],
                    "ip_address": "${event.ip_address}",
                },
            }
        ],
    },
    variables={
        "form_entry_type": {
            "type": "entry_type_slug",
            "description": "The form's entry type (not the newsletter signup type: the Buttondown content already handles that one).",
            "example": "contact",
        },
        "optin_field": {
            "type": "field_key",
            "description": "The checkbox field a sender ticks to join the newsletter.",
            "example": "subscribe",
        },
        "email_field": {"type": "field_key", "description": "The field holding the sender's email address.", "example": "email"},
        "tag": {
            "type": "text",
            "description": "A Buttondown tag put on these subscribers, so you can tell where they signed up (created if missing).",
            "example": "contact-form",
        },
        "buttondown_integration": {
            "type": "integration_slug",
            "description": "The Buttondown connection to subscribe through.",
            "example": "buttondown",
        },
    },
    side_effects=({"kind": "external_post", "description": "Adds the address to Buttondown, which sends the reader a confirmation email."},),
)

RECIPES = (SUBSCRIBE_ON_OPT_IN,)
