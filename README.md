# marvin-integration-buttondown

Buttondown integration for [Marvin](https://github.com/InnerOpen/marvin): run a site's newsletter through
[Buttondown](https://buttondown.com) while Marvin stays the list of record.

A signup on your site becomes a Buttondown subscriber. When the reader confirms, their signup entry is
published; when they unsubscribe, it's archived (inbox = pending, published = confirmed, archived =
unsubscribed). A signup Buttondown refuses goes to **Needs review** with the reason, so it never waits in
the inbox looking like a pending one. Signups are internal to Marvin: they stay out of your content and are
read under **Audience › Subscribers** (counts per status, search, a detail with the status history), which
replaces the two smart collections earlier versions offered. Publishing a newsletter issue creates its Buttondown email, as a
**draft** by default, so you review and send it in Buttondown. You can also have it sent straight away,
or turn this off.

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
| `subscribe` | `email`, optional `tags`, `ip_address` (helps Buttondown's spam firewall), `metadata`, `notes`, `referrer_url`. Returns `subscriber_id` (the API's `sub_…`), `email`, `type`, `already_subscribed`. An address that's already subscribed returns the existing subscriber. If that subscriber never confirmed (`type: unactivated`), Buttondown would send nothing, so `subscribe` asks it to re-send the confirmation email (`POST /v1/subscribers/{id}/send-reminder`) and adds `confirmation_resent: true`. If Buttondown refuses (rate limit, error), the signup still succeeds with `confirmation_resent: false` and a `confirmation_reason`. A confirmed (`regular`) subscriber gets nothing extra. You get a readable error when Buttondown's firewall refuses the address (`subscriber_blocked`, `email_blocked`, `ip_address_spammy`) or when it unsubscribed before (`subscriber_suppressed`, with the hint to re-add them in Buttondown). |
| `lookup_subscriber` | `subscriber`: a webhook's UUID, a `sub_` id or an email. Returns `subscriber_id`, `email`, `type`. Buttondown gives a subscriber two ids: the API returns `sub_…` and webhooks carry a UUID. This turns either one into the `sub_` id. |
| `create_issue_email` | `subject`, `body` (Markdown), `description` (preview text), `canonical_url`, `entry_id`, `email_id`, `site_url`. Follows **Issue delivery**. `canonical_url` (the workflow passes `${entry.url}`, the issue's page on your site) is sent to Buttondown when it is, or can be made, absolute; otherwise it is left out. Runs once per entry: if `email_id` (the entry's stored `buttondown_email_id`) still exists in Buttondown, or an email carries `metadata.marvin_entry_id` for this entry, that email is returned with `skipped: true` and no second email is made. The body is sent with Buttondown's Markdown editor-mode marker. Every create names its status, because Buttondown's own default is `about_to_send` (send). Returns `sent` (0.9): true only when this call created the email and asked Buttondown to send it. |
| `resolve_email_id` | `email`: the id a webhook carries (a UUID) or an `em_` id. Returns `email_id`, the `em_…` id the API returns (a TypeID is its prefix plus the same UUID in base32, so this makes no call). It is how an `email.sent` event is matched to the send Marvin recorded. |
| `connect_webhooks` | **Connect Buttondown webhooks.** Creates or updates the Buttondown webhook that posts this workspace's `subscriber.confirmed`, `subscriber.unsubscribed` and (0.9) `email.sent` events to Marvin: enabled, signed with `signing_key`. Args: `webhook_url` (this workspace's `buttondown` incoming webhook URL), `signing_key: {{BUTTONDOWN_SIGNING_KEY}}`, optional `remove_legacy_url` (an old Marvin hook URL to retire), optional `label` (e.g. the workspace name, shown in Buttondown's description). Safe to run again. The webhook already pointing at `webhook_url` is updated, not duplicated, and extra copies on that URL are removed. Webhooks pointing anywhere else are never touched. `remove_legacy_url` deletes only a webhook whose URL matches it exactly (a trailing slash aside), and it must be a Marvin hook URL. Returns `webhook_id`, `result` (`created` / `updated` / `replaced` / `unchanged`), `removed`, `legacy_removed`. |

It also contributes the **`buttondown`** webhook signature scheme: HMAC-SHA256 of the raw body, hex, in
`X-Buttondown-Signature: sha256=<hex>` (as Buttondown's docs describe). Buttondown's **Test webhook**
button sends *unsigned* requests, so Marvin rejects those with a 401. Real events are signed.

## Errors and how they're handled

Every failure, network errors included, raises a readable `ButtondownError` (an SDK `IntegrationError`)
with a stable `code`. The provider declares an **error policy** saying what Marvin does about each code;
Marvin applies it (retries, Needs review, admin alerts) and shows it on the integration's card under
"How errors are handled". The provider itself never sleeps, retries or alerts. A workflow's own
on-failure steps still see the code as `${error.code}`.

| `code` | When | `subscribe` (provider policy) |
|---|---|---|
| `blocked` | The spam firewall refused the address (`subscriber_blocked`, `email_blocked`). | Send to review. |
| `spammy` | The spam firewall refused the visitor's IP (`ip_address_spammy`). | Send to review. |
| `suppressed` | The address unsubscribed before (`subscriber_suppressed`); only Buttondown can re-add it. | Send to review. |
| `unavailable` | Buttondown couldn't be reached or timed out, or answered 5xx. | Retry 3× (2m, 10m, 1h), then send to review. |
| `rate_limited` | Buttondown answered 429. Its `Retry-After` (seconds or an HTTP date) becomes the error's `retry_after`, which Marvin honours over the backoff. | Retry 3× (1m, 5m, 15m), then send to review. |
| `auth` | No API key on the connection, or Buttondown rejected it (401/403). | Notify admins, wait until the connection is healthy again, retry once, then send to review. |
| `unknown` | Anything else: another HTTP error, a bad argument or setting. | Send to review (the `*` fallback). |

"Send to review" moves the signup entry to **Needs review** with the error message as its reason, and
Marvin records the failure on the entry as `integration_error.buttondown`, so the workflow needs no
on-failure steps of its own.

Some actions override the provider policy, because sending their entry to review would be wrong:

| Action | Policy | Why |
|---|---|---|
| `lookup_subscriber` | `unavailable` / `rate_limited`: the same retries, then notify admins. `auth`: notify, retry once on recovery, then fail. Anything else: fail. | It runs on Buttondown's webhook before there is a signup entry to review. Retrying a read is free and keeps a confirm or unsubscribe from being lost; a subscriber that doesn't exist is just a failed run. |
| `create_issue_email` | The same as `lookup_subscriber`. | Its entry is a *published* issue, and review would take it off the site. Retries are safe: the email is created once per entry, so a retry after a timeout finds the email instead of making (or sending) a second one. |
| `connect_webhooks` | Every code: fail. | An admin runs it by hand from the card and sees the error right there. Nothing to retry or review. |

## What a workspace gets — declared, applied from the integration's card

Nothing is created on install. **Apply** creates only what is missing. The webhook and workflows arrive
switched off, and each can stay off on its own.

| Kind | Slug | What it does |
|---|---|---|
| entry fields | `buttondown-signup-form` | **Signup form.** Makes the signup type a signup form: `submission.kind: signup` (its entries are newsletter signups, listed under Audience › Subscribers, not as messages) and `submission.destination: audience` (internal: out of All entries, the status lists, search and the AI index; the entry type editor's *Show in content lists* is off). Adds the `email` field the signup workflow reads, if the type lacks it. Only settings the type doesn't already store are set: a `kind` or `destination` you chose stays. Needs a Marvin with Subscribers (see [Marvin versions](#known-limits)); on an older one the settings block is ignored and you set the type's **Signup** setting by hand. |
| incoming webhook | `buttondown` | Where Buttondown posts subscriber events (scheme `buttondown`, secret `BUTTONDOWN_SIGNING_KEY`). |
| workflow | `buttondown-subscribe-on-signup` | `form_submission_received` for the signup type, not flagged, and not a repeat from a reader already confirmed (`event.previous_status` ≠ `published`, see [One entry per reader](#one-entry-per-reader)) → `subscribe` (tag `website`, visitor IP) → `set_metadata buttondown_subscriber_id`. **If it fails**, the error policy above applies: a refusal goes straight to **Needs review** with the reason; an outage or rate limit is retried first. |
| workflow | `buttondown-subscriber-confirmed` | `subscriber.confirmed` → `lookup_subscriber` → the signup entry whose `metadata.buttondown_subscriber_id` matches → **publish**. No matching entry → the step is skipped (`if_none: skip`) and the run stays green. |
| workflow | `buttondown-subscriber-unsubscribed` | `subscriber.unsubscribed` → `lookup_subscriber` → that entry → **archive** (skipped quietly when there is none). |
| workflow | `buttondown-issue-on-publish` | `entry_published` for the issue type → `create_issue_email` (title, `body`, `preview`, `${entry.url}` as canonical URL, `${site.url}`) → `set_metadata buttondown_email_id` + `buttondown_issue_delivery`. Unchanged since 0.8.0, so a workspace that edited its copy has nothing to overwrite. |
| workflow | `buttondown-email-sent` | (0.9) `email.sent` → `resolve_email_id` → `automation_result` (Succeeded): settles the send recorded under Results. |

## Results (0.9)

With **Issue delivery: send**, `create_issue_email` is a *hand-off*: Marvin records it under **Settings › Automation › Results**
as *In flight*, with the email's id and its link. Buttondown then posts `email.sent` when it has finished sending, and
`buttondown-email-sent` marks the result *Succeeded*. A **draft**, a skip (the issue already has an email) and Issue delivery **off**
are not hand-offs and record nothing. Buttondown has no event for a send that fails, so a send that never completes shows as
*No reply* after a day; a send Buttondown refuses fails the step as it always did.

An email you send from Buttondown's own dashboard is listed too (a result is kept for any `email.sent` that matches nothing).

To start receiving `email.sent`, run **Connect Buttondown webhooks** again after upgrading (it updates the webhook already there),
then click **Apply** and switch on `buttondown-email-sent`. This release changes none of the existing workflows (`buttondown-issue-on-publish` included),
so no card offers an Update.

## Subscribers replaces the collections

Versions 0.3 to 0.7 offered two private smart collections, **Confirmed subscribers** and **Unsubscribed**. A signup's status already says where it stands (published = confirmed, archived = unsubscribed), and Marvin's **Audience › Subscribers** now shows every signup with its status (**Pending**, **Confirmed**, **Unsubscribed**, **Needs a look**), counts at the top, search, filters by form, status and date, and a detail for each. The collections are no longer shipped, and Marvin's own upgrade removes the ones this integration made (only those still exactly as it created them: a collection you renamed, made public or changed is left alone).

What Subscribers does and doesn't do: it lists and opens signups, and can move one to the Trash. It never subscribes or unsubscribes anyone: that stays here (the subscribe workflow, the confirm and unsubscribe webhook workflows). When this integration is connected the page says so; without a mailing-list integration it still lists signups, with a hint to connect one.

Parameters, asked when you apply:

- `integration`: the Buttondown connection. Default `buttondown`.
- `signup_type`: the signup form's submittable entry type. Default `newsletter`. Suggested: match readers by `email` on it (see [One entry per reader](#one-entry-per-reader)).
- `issue_type`: the issue entry type. Default `newsletter-issue`. Its `body` and `preview` fields make the email.

## Setting it up

1. Store the API key as a workspace secret. Connect the integration with `{{BUTTONDOWN_API_KEY}}` and pick the **Issue delivery**. Set a **Site URL** only if the workspace has no Canonical URL.
2. **Apply** the content. This makes the signup type a signup form, so it appears under **Audience › Subscribers**.
3. Open the `buttondown` incoming webhook (Automation → Incoming webhooks). Click **Mint token** and copy the URL. Under **Signing**, click **Change**, keep the secret `BUTTONDOWN_SIGNING_KEY` and click **Generate key**. You can skip this if the workspace already has that secret. You don't paste the key anywhere: the next step hands it to Buttondown.
4. On the integration's card, run **Connect Buttondown webhooks** with:
   - **webhook_url**: the URL from step 3
   - **signing_key**: `{{BUTTONDOWN_SIGNING_KEY}}`
   - **remove_legacy_url** (optional): the URL of an older hand-built Buttondown hook you're retiring, e.g. a `buttondown-incoming-webhook`
   - **label** (optional): the workspace name
5. Switch on the `buttondown` incoming webhook and the workflows you want. A provider can't switch Marvin's webhooks on, so this one is a click.
6. If you had an older hand-built Buttondown hook in Marvin, delete it (Automation → Incoming webhooks).
7. Open the signup entry type (e.g. `newsletter`) and, under **Submission settings**, set **Same person = same …** to `email`. See [One entry per reader](#one-entry-per-reader).
8. **Check Buttondown's side.** In Buttondown, open **Settings → Webhooks**: the Marvin webhook must list all three events, `subscriber.confirmed`, `subscriber.unsubscribed` and **`email.sent`**. Without `email.sent` every send stays *In flight* under Settings › Automation › Results and turns into *No reply* a day later. If one is missing, run **Connect Buttondown webhooks** again (it updates the existing webhook), or add the event by hand in Buttondown. Also check that the `buttondown-email-sent` workflow is switched on (step 5). Seen on Mash & Burn after the 0.9 upgrade: `email.sent` was missing and the workflow was off.

Repeat steps 3–8 in **every workspace that shares the Buttondown account**.

### One entry per reader

Marvin can keep one entry per person for a submittable type: its match field (`capabilities.submission.matchField`, **Same person = same …** in the editor). Set it to `email` on the signup type and a repeat signup updates the reader's entry instead of adding a second one. Apply can't set it for you (a blueprint may set a form's `destination` and `kind`, nothing else), so it's step 7 above. Or set it through the API: `PATCH` the entry type with `capabilitiesJson.submission.matchField: "email"`, keeping its other capabilities.

With it set, the signup workflow (0.6.0+) handles a repeat by what the reader's entry was before:

| Previous status | What happens |
|---|---|
| none (a first signup) | Subscribe, as always. |
| `inbox` (still pending) | Subscribe again. Buttondown answers it idempotently. |
| `archived` (unsubscribed) | Marvin reopens the entry to the inbox and the workflow subscribes again. If Buttondown suppresses the address, the entry goes to Needs review (see [Known limits](#known-limits)). |
| `published` (confirmed) | Skipped. They're already on the list; no second subscribe. |

A flagged repeat is never forwarded: Marvin files it as its own `needs_review` entry and leaves the reader's entry alone. Without the match field every signup is its own entry, as before, and the new condition never matches.

### Several workspaces, one Buttondown account

Buttondown webhooks belong to the account, so every workspace on the account gets its own webhook, each pointing at its own Marvin hook URL and signed with its own key. `connect_webhooks` only ever changes the webhook on the URL you give it (plus the exact legacy URL, if you name one). Running it in workspace B leaves workspace A's webhook alone.

Each workspace then receives every confirm and unsubscribe on the account, including readers who signed up through another workspace's site. Those readers have no signup entry in this workspace, so the entry step skips them (`if_none: skip`) and the run stays green.

If a hook token is rotated, run **Connect Buttondown webhooks** again with the new URL and the old one as `remove_legacy_url`.

## Upgrading to 0.8: Subscribers replaces the collections

- **Core does the clean-up.** On a Marvin with Subscribers, its upgrade removes this integration's `confirmed-subscribers` and `unsubscribed` collections where they are still exactly what 0.3–0.7 created (smart, private, one signup type, one status), and marks the signup type a signup form (`submission.kind: signup`) when it can tell which type Buttondown was applied with. Nothing else is touched; no entry changes.
- **Hand-made or changed collections stay.** A collection with one of those slugs that you edited, or built yourself (including one converted from 0.2's collection workflows), is yours: delete it from Collections if you no longer want it.
- **A workspace the upgrade couldn't mark** (the subscribe workflow was deleted, or the signup type has another name and no workflow names it): apply `buttondown-signup-form` from the card, or open the signup entry type and set **Signup** under Submission settings. One click either way.
- **0.2's `buttondown-confirmed-to-collection` / `buttondown-unsubscribed-from-collection` workflows** were dropped in 0.3 and still run in a workspace that applied them, if they are on. Delete them (Automation › Workflows); Subscribers makes them unnecessary.
- **On an older Marvin** (without Subscribers) nothing breaks: the signup form blueprint still applies (it only adds `email` if missing, its settings block is ignored), the collections are no longer offered, and the ones already there keep working. Set the **Signup** setting by hand when you upgrade Marvin, or apply the blueprint then.

## Known limits

- A confirm or unsubscribe for a reader with no signup entry here (they subscribed some other way) is a quiet no-op: the entry step's output says `skipped: true, reason: "no matching entry"`. If a repeat signup left **two** entries with the same subscriber id (from before the signup type had a match field), the step fails rather than guess. Merge or delete the extra one; with [one entry per reader](#one-entry-per-reader) set it doesn't happen again.
- **A refused signup goes to Needs review; a refusal is never retried.** When Buttondown refuses an address, the signup workflow's run fails (it shows under **Runs**) and the error policy moves the entry to **Needs review** with the reason, which the Review Queue's card shows. Look at the entry: a firewall refusal (`blocked` / `spammy`) is usually spam you can archive; a real reader can be added by hand in Buttondown. Only an outage, a rate limit or a rejected key is retried (see the table above); an `unknown` failure goes to review straight away.
- **Returning readers are manual for now.** An address that unsubscribed before can't rejoin from a signup: Buttondown answers `subscriber_suppressed`, the run fails and the entry goes to Needs review with code `suppressed`. To let them back in, re-add or re-confirm them in Buttondown (Subscribers → the address → change its type, or send a new confirmation). Their next `subscriber.confirmed` then publishes the entry as usual. With [one entry per reader](#one-entry-per-reader) the rejoin attempt reopens their archived entry instead of adding a new one, so it's that entry that goes to Needs review.
- Updating a Buttondown webhook in place needs a Marvin whose integration HTTP helper has PATCH. On an older Marvin, `connect_webhooks` replaces the webhook instead: it creates the new one, then deletes the old one (`result: replaced`). The outcome is the same, but the webhook gets a new id.
- Marvin versions: the signup form settings and Audience › Subscribers need the Marvin release after 1.1.0-rc.14 (a blueprint's `submission` block is ignored before that). `${site.url}` needs 1.0.0-rc.177+. `${entry.url}` and the entry step's `if_none: skip` need the release after it. On an older Marvin they are ignored: no canonical URL is sent (set the connection's Site URL for links), and a reader with no signup entry fails the step. The error policy needs a Marvin that reads SDK 0.5 error policies; an older Marvin ignores it, so a failed signup just fails its run and the entry stays in the inbox. One entry per reader needs a Marvin with the submission match field; on an older one every signup is its own entry and the `previous_status` condition always passes.
- **Upgrading to 0.6.0:** the signup workflow gained the `previous_status` condition. The card marks an applied copy with **↑**; click **Update** to pick it up (whether it's switched on is kept), then do step 7.
- **Already applied the content?** Apply never overwrites, so a workspace that applied the signup workflow from 0.3.x keeps its on-failure steps (`set_metadata buttondown_subscribe_error` → `request_review`). Marvin runs a workflow's own on-failure steps instead of the provider policy, so that copy still sends refusals to review, but without the retries. The card marks it with **↑** and an **Update** button; click it to drop the on-failure steps and use the policy (whether it's switched on is kept).

## What it teaches Marvin (0.8.0, SDK 0.9.0)

- **A guide** (`src/marvin_integration_buttondown/guide.md`): connecting it, what each action takes and returns, how
  Issue delivery (draft or send) fits a workflow, what it can't do, and what each error code means. Marvin's agents
  read it when building workflows where Buttondown is connected, and Marvin's manual search finds it.
- **A recipe** in the Workflow Library, *Subscribe form submitters who opt in*: when a form other than the newsletter
  signup (a contact or event form) is submitted with its opt-in checkbox ticked, the sender is subscribed through
  `subscribe`. Shown as "From the Buttondown plugin 0.8.0", offered once Buttondown is connected. Marvin's own
  *Newsletter delivery* recipe already covers the issue side, so it isn't repeated here. Confirmations and
  unsubscribes only update signup-type entries, so a reader subscribed this way isn't tracked back on the form entry.

## Logo

`logo.svg` is Buttondown's logo, unmodified, from the press kit at [buttondown.com/brand](https://buttondown.com/brand). Buttondown asks that it not be altered or recoloured, not be used to imply a business relationship, and be shown only on a white or black background.

## Develop

```
uv run --extra dev pytest -q
uv run --extra dev ruff check .
```

Resolves the SDK from a sibling checkout (`../MarvinIntegrationSDK`).
