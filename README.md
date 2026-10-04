# marvin-integration-buttondown

Buttondown integration for [Marvin](https://github.com/InnerOpen/marvin): run a site's newsletter through
[Buttondown](https://buttondown.com) while Marvin stays the list of record.

A signup on your site becomes a Buttondown subscriber. When the reader confirms, their signup entry is
published; when they unsubscribe, it's archived (inbox = pending, published = confirmed, archived =
unsubscribed). Publishing a newsletter issue creates its Buttondown email, as a **draft** by default,
so you review and send it in Buttondown. You can also have it sent straight away, or turn this off.

## Connection

| | |
|---|---|
| **API key** | Buttondown → Settings → API. Store it as a workspace secret (e.g. `BUTTONDOWN_API_KEY`) and enter `{{BUTTONDOWN_API_KEY}}`. The integration never logs it. |
| **Issue delivery** | **Off**: publishing an issue does nothing in Buttondown. **Draft** (default): creates a draft to review and send there. **Send**: sends it to your subscribers right away (`status: about_to_send`). |
| **Site URL** | Optional. Relative links in an issue (`[a work](/works/x)`, `href="/x"`) are made absolute against it, so they work in the email. Blank means the workspace's **Canonical URL** (Settings → General), which the workflow passes as `${site.url}`. Absolute links are left alone. |

The card's check calls `GET /v1/ping` with the key. It shows **unconfigured** without a key, and **error**
if the key is rejected or the config is invalid.

## Actions

| Action | What it does |
|---|---|
| `subscribe` | `email`, optional `tags`, `ip_address` (helps Buttondown's spam firewall), `metadata`, `notes`, `referrer_url`. Returns `subscriber_id` (the API's `sub_…`), `email`, `type`, `already_subscribed`. An address that's already subscribed returns the existing subscriber. You get a readable error when Buttondown's firewall refuses the address (`subscriber_blocked`, `email_blocked`, `ip_address_spammy`) or when it unsubscribed before (`subscriber_suppressed`). |
| `lookup_subscriber` | `subscriber`: a webhook's UUID, a `sub_` id or an email. Returns `subscriber_id`, `email`, `type`. Buttondown gives a subscriber two ids: the API returns `sub_…` and webhooks carry a UUID. This turns either one into the `sub_` id. |
| `create_issue_email` | `subject`, `body` (Markdown), `description` (preview text), `entry_id`, `email_id`, `site_url`. Follows **Issue delivery**. Runs once per entry: if `email_id` (the entry's stored `buttondown_email_id`) still exists in Buttondown, or an email carries `metadata.marvin_entry_id` for this entry, that email is returned with `skipped: true` and no second email is made. The body is sent with Buttondown's Markdown editor-mode marker. Every create names its status, because Buttondown's own default is `about_to_send` (send). |

Every failure, network errors included, raises a readable error, so the workflow step fails visibly.

It also contributes the **`buttondown`** webhook signature scheme: HMAC-SHA256 of the raw body, hex, in
`X-Buttondown-Signature: sha256=<hex>` (as Buttondown's docs describe). Buttondown's **Test webhook**
button sends *unsigned* requests, so Marvin rejects those with a 401. Real events are signed.

## What a workspace gets — declared, applied from the integration's card

Nothing is created on install. **Apply** creates only what is missing. The webhook and workflows arrive
switched off, and each can stay off on its own.

| Kind | Slug | What it does |
|---|---|---|
| incoming webhook | `buttondown` | Where Buttondown posts subscriber events (scheme `buttondown`, secret `BUTTONDOWN_SIGNING_KEY`). |
| workflow | `buttondown-subscribe-on-signup` | `form_submission_received` for the signup type, not flagged → `subscribe` (tag `website`, visitor IP) → `set_metadata buttondown_subscriber_id`. |
| workflow | `buttondown-subscriber-confirmed` | `subscriber.confirmed` → `lookup_subscriber` → the signup entry whose `metadata.buttondown_subscriber_id` matches → **publish**. |
| workflow | `buttondown-subscriber-unsubscribed` | `subscriber.unsubscribed` → `lookup_subscriber` → that entry → **archive**. |
| workflow | `buttondown-issue-on-publish` | `entry_published` for the issue type → `create_issue_email` (title, `body`, `preview`, `${site.url}`) → `set_metadata buttondown_email_id` + `buttondown_issue_delivery`. |
| workflow *(suggestion)* | `buttondown-confirmed-to-collection` | `subscriber.confirmed` → add the signup entry to a collection (default `confirmed-subscribers`). |
| workflow *(suggestion)* | `buttondown-unsubscribed-from-collection` | `subscriber.unsubscribed` → remove it from that collection. |

Parameters, asked when you apply:

- `integration`: the Buttondown connection. Default `buttondown`.
- `signup_type`: the signup form's submittable entry type. Default `newsletter`.
- `issue_type`: the issue entry type. Default `newsletter-issue`. Its `body` and `preview` fields make the email.
- `collection`: only for the two suggestions. It must exist.

## Setting it up

1. Store the API key as a workspace secret. Connect the integration with `{{BUTTONDOWN_API_KEY}}` and pick the **Issue delivery**. Set a **Site URL** only if the workspace has no Canonical URL.
2. **Apply** the content.
3. Open the `buttondown` incoming webhook and click **Mint token**. Copy the URL.
4. In Buttondown → Settings → Webhooks, add that URL for `subscriber.confirmed` and `subscriber.unsubscribed` and generate a signing key. Store the key in Marvin as the workspace secret `BUTTONDOWN_SIGNING_KEY`.
5. Switch on the webhook and the workflows you want.

## Known limits

- A confirm or unsubscribe can find no matching entry: the reader subscribed somewhere other than the site, or a repeat signup left two entries. Either way the entry step fails, and you see it as a failed run.
- An address that unsubscribed before can't rejoin from a signup (`subscriber_suppressed`). Buttondown needs a manual re-add or re-confirmation.
- `${site.url}` needs a Marvin that provides it. On an older Marvin it is blank, so set the connection's Site URL.

## Develop

```
uv run --extra dev pytest -q
uv run --extra dev ruff check .
```

Resolves the SDK from a sibling checkout (`../MarvinIntegrationSDK`).
