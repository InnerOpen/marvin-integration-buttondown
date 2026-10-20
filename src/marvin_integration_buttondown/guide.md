# Buttondown

Runs a newsletter through [Buttondown](https://buttondown.com) while Marvin stays the list of record. A signup on the
site becomes a Buttondown subscriber, Buttondown's confirmations and unsubscribes come back and update the signup
entry, and a published newsletter issue becomes a Buttondown email (a draft to review, or sent straight away).

## Connect it

1. In Buttondown, copy your **API key** (Settings → API). In Marvin, save it as the workspace secret
   `BUTTONDOWN_API_KEY`.
2. **Settings → Integrations → Buttondown → Connect**: set the credential to `{{BUTTONDOWN_API_KEY}}` and choose:
   - **Issue delivery**: `off` (publishing an issue does nothing in Buttondown), `draft` (the default: a draft to
     review and send in Buttondown) or `send` (sent to your subscribers as soon as the issue is published).
   - **Site URL** (optional): your public site, e.g. `https://example.com`. Relative links in an issue (`/works/x`)
     are made absolute against it so they work in the email. Blank means the workspace's Canonical URL
     (Settings -> General).
3. Apply the integration's content from its card: the signup form settings (the signup type becomes a signup form, listed
   under Audience -> Subscribers), the `buttondown` incoming webhook and the workflows below. The webhook and workflows
   arrive switched off; review them, then turn them on. On a Marvin older than the one with Subscribers, the signup form
   settings can't be applied: set the signup type's **Signup** setting in the entry type editor instead.
4. To hear back from Buttondown: mint the `buttondown` incoming webhook's token, generate a key under Signing (stored
   as `BUTTONDOWN_SIGNING_KEY`), then run the **Connect Buttondown webhooks** action with the webhook's URL. It is safe
   to run again. Buttondown's dashboard "Test webhook" button sends unsigned, so Marvin rejects it; real events are
   signed.

## The loop

A signup entry's status mirrors Buttondown: inbox = waiting for the reader to confirm, published = confirmed,
archived = unsubscribed, needs review = a problem (Buttondown refused it, or the spam protection held it).

Signups are internal to Marvin: the signup type is a signup form (`submission.kind: signup`, destination Audience), so its
entries stay out of All entries, the status lists, search and the AI index, and are read under **Audience -> Subscribers**
(counts per status, search, a detail with the history). Applying the integration's content sets this on the signup type.
Subscribers is read-only for Buttondown's part: subscribing and unsubscribing happen here, in the workflows and webhook below.

- A submission of the signup entry type (not flagged as spam) runs `subscribe`; the subscriber's id is stored on the
  entry as `buttondown_subscriber_id`.
- `subscriber.confirmed` publishes that entry; `subscriber.unsubscribed` archives it. A reader with no signup entry
  here (subscribed another way, or through another workspace on the same Buttondown account) is skipped quietly.
- Suggested: set the signup type's Submission settings -> "Same person = same ..." to its email field, so a repeat
  signup updates the reader's entry instead of adding a second one, and a repeat from someone already confirmed is
  skipped.
- Publishing an entry of the issue type runs `create_issue_email`; the email's id is stored on the entry as
  `buttondown_email_id`.
- Nothing else is needed to list readers by status: Audience -> Subscribers does it. (Earlier versions offered two smart
  collections, "Confirmed subscribers" and "Unsubscribed"; they are gone, and Marvin removes the ones this integration made.)

## Actions

**Subscribe** (`subscribe`)
- `email` (required); optional `tags` (created in Buttondown if missing), `ip_address` (the visitor's IP, which helps
  Buttondown's spam firewall), `metadata`, `notes`, `referrer_url`.
- Output: `subscriber_id` (`sub_...`), `email`, `type`, `already_subscribed`.
- An address already on the list returns its existing subscriber with `already_subscribed: true`. If that subscriber
  never confirmed (`type` is `unactivated`), Buttondown is asked to re-send the confirmation email:
  `confirmation_resent` says whether it worked and `confirmation_reason` why not. A failed reminder never fails the
  signup.

**Look up subscriber** (`lookup_subscriber`)
- `subscriber`: a webhook's `data.subscriber` UUID, a `sub_` id or an email address. Output: `subscriber_id`, `email`,
  `type`. Buttondown's webhooks carry a UUID the API never returns, so this is how a confirm or unsubscribe event is
  matched to the entry that stored the `sub_` id. An unknown subscriber fails the step.

**Create issue email** (`create_issue_email`)
- `subject` and `body` (Markdown) are required. Optional: `description` (preview text), `canonical_url` (the issue's
  page, `${entry.url}`; a site path is made absolute), `entry_id` (`${entry.id}`), `email_id` (the entry's stored
  `buttondown_email_id`) and `site_url` (`${site.url}`; the connection's Site URL wins when set).
- Output: `email_id`, `status`, `delivery`, `skipped`, `reason`, `url`.
- It follows the connection's Issue delivery, so the same workflow drafts or sends depending on the connection.
  With delivery `off` it returns `skipped: true` and no email id. It creates one email per entry: if the entry already
  has one (found by its stored id, or by the entry id recorded on the email in Buttondown) it returns that email with
  `skipped: true`, so republishing never sends twice.
- Draft or send: a `draft` is created for you to review and send in Buttondown. `send` is created as
  `about_to_send`, which means it goes out to subscribers. Choose `send` only when a published issue is final.
- Keep the step that stores `buttondown_email_id` (and `buttondown_issue_delivery`) after it; the
  **Newsletter delivery** recipe in Marvin's Workflow Library is exactly this.

**Connect Buttondown webhooks** (`connect_webhooks`)
- `webhook_url` (this workspace's `buttondown` incoming webhook URL) and `signing_key` (`{{BUTTONDOWN_SIGNING_KEY}}`)
  are required; optional `label` and `remove_legacy_url` (an old Marvin hook URL to retire).
- Creates or updates the one Buttondown webhook that sends `subscriber.confirmed` and `subscriber.unsubscribed` to
  Marvin. Webhooks pointing elsewhere are left alone. Output: `webhook_id`, `result` (`created`, `updated`, `replaced`
  or `unchanged`), `removed`, `legacy_removed`. It is meant to be run by an admin; it is not retried.

## In workflows

- An integration step is `{"kind": "integration", "integration": "<connection slug>", "action": "<key>", "args": {...}}`.
  The connection's slug (from its card) is the `integration` value, not `buttondown`, if it was named differently.
- Later steps read an action's output as `${steps.<id>.output.<key>}` (give the step an `id`).
- To subscribe someone from a form other than the signup form (for example a contact form with an opt-in checkbox),
  use the **Subscribe form submitters who opt in** recipe. Note that confirmations and unsubscribes only update
  signup-type entries, so those readers are not tracked back on the form entry. A refused subscribe fails that
  workflow run (its alert fires) and leaves the form entry where it is, rather than sending it to Needs review.
- A signup entry is published only when Buttondown reports the reader's confirmation, so a new reader stays in the
  inbox until then.

## What it can't do

- Send a newsletter on a schedule, edit or delete an email after it is created, or read opens and clicks.
- Re-add an address that unsubscribed before: Buttondown refuses it (`suppressed`); re-add them in Buttondown.
- Unsubscribe or remove a subscriber from Marvin, or change a subscriber's tags afterwards.
- Send the first email of a newsletter whose sending Buttondown has not confirmed yet: the API refuses
  (`sending_requires_confirmation`) until that is done in Buttondown's dashboard.

## When it fails

A failure carries a code that workflow on-failure steps read as `${error.code}`. Marvin applies the handling; the
integration never retries or alerts on its own.

- **`blocked`**: Buttondown's spam firewall refused the address. The signup goes to Needs review with the reason.
- **`spammy`**: the firewall refused the visitor's IP. Needs review.
- **`suppressed`**: the address unsubscribed before; Buttondown will not re-add it from a signup. Needs review;
  re-add them in Buttondown by hand.
- **`auth`**: no API key, or Buttondown rejected it (HTTP 401/403). Admins are alerted and it is retried once when the
  connection is healthy again; fix the key.
- **`rate_limited`**: HTTP 429. Retried after 1 minute, 5 minutes, then 15 minutes (Buttondown's `Retry-After` wins),
  then Needs review.
- **`unavailable`**: Buttondown unreachable, timed out or answering 5xx. Retried after 2 minutes, 10 minutes, then
  1 hour, then Needs review.
- **`unknown`**: anything else (another HTTP error, a bad argument, a bad connection setting, an unknown subscriber).
  The step fails with the message; for `subscribe` the signup goes to Needs review.

`lookup_subscriber` (it runs before there is a signup entry) and `create_issue_email` (it runs on a published issue,
which review would take off the site) are never sent to Needs review: transient failures retry, then an admin is told,
and anything else just fails the step. `connect_webhooks` is not retried; the admin sees the error where they ran it.
