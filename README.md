# marvin-integration-buttondown

Buttondown integration for [Marvin](https://github.com/InnerOpen/marvin): run a site's newsletter through
[Buttondown](https://buttondown.com) while Marvin stays the list of record.

A signup on your site becomes a Buttondown subscriber. When the reader confirms, their signup entry is
published; when they unsubscribe, it's archived (inbox = pending, published = confirmed, archived =
unsubscribed). A signup Buttondown refuses goes to **Needs review** with the reason, so it never waits in
the inbox looking like a pending one. Publishing a newsletter issue creates its Buttondown email, as a
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
| `create_issue_email` | `subject`, `body` (Markdown), `description` (preview text), `canonical_url`, `entry_id`, `email_id`, `site_url`. Follows **Issue delivery**. `canonical_url` (the workflow passes `${entry.url}`, the issue's page on your site) is sent to Buttondown when it is, or can be made, absolute; otherwise it is left out. Runs once per entry: if `email_id` (the entry's stored `buttondown_email_id`) still exists in Buttondown, or an email carries `metadata.marvin_entry_id` for this entry, that email is returned with `skipped: true` and no second email is made. The body is sent with Buttondown's Markdown editor-mode marker. Every create names its status, because Buttondown's own default is `about_to_send` (send). |
| `connect_webhooks` | **Connect Buttondown webhooks.** Creates or updates the Buttondown webhook that posts this workspace's `subscriber.confirmed` and `subscriber.unsubscribed` events to Marvin: enabled, signed with `signing_key`. Args: `webhook_url` (this workspace's `buttondown` incoming webhook URL), `signing_key: {{BUTTONDOWN_SIGNING_KEY}}`, optional `remove_legacy_url` (an old Marvin hook URL to retire), optional `label` (e.g. the workspace name, shown in Buttondown's description). Safe to run again. The webhook already pointing at `webhook_url` is updated, not duplicated, and extra copies on that URL are removed. Webhooks pointing anywhere else are never touched. `remove_legacy_url` deletes only a webhook whose URL matches it exactly (a trailing slash aside), and it must be a Marvin hook URL. Returns `webhook_id`, `result` (`created` / `updated` / `replaced` / `unchanged`), `removed`, `legacy_removed`. |

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
switched off, and each can stay off on its own. The two collections are optional.

| Kind | Slug | What it does |
|---|---|---|
| incoming webhook | `buttondown` | Where Buttondown posts subscriber events (scheme `buttondown`, secret `BUTTONDOWN_SIGNING_KEY`). |
| workflow | `buttondown-subscribe-on-signup` | `form_submission_received` for the signup type, not flagged → `subscribe` (tag `website`, visitor IP) → `set_metadata buttondown_subscriber_id`. **If it fails**, the error policy above applies: a refusal goes straight to **Needs review** with the reason; an outage or rate limit is retried first. |
| workflow | `buttondown-subscriber-confirmed` | `subscriber.confirmed` → `lookup_subscriber` → the signup entry whose `metadata.buttondown_subscriber_id` matches → **publish**. No matching entry → the step is skipped (`if_none: skip`) and the run stays green. |
| workflow | `buttondown-subscriber-unsubscribed` | `subscriber.unsubscribed` → `lookup_subscriber` → that entry → **archive** (skipped quietly when there is none). |
| workflow | `buttondown-issue-on-publish` | `entry_published` for the issue type → `create_issue_email` (title, `body`, `preview`, `${entry.url}` as canonical URL, `${site.url}`) → `set_metadata buttondown_email_id` + `buttondown_issue_delivery`. |
| collection *(suggestion)* | `confirmed-subscribers` | **Confirmed subscribers.** Smart, private: signup-type entries that are `published`. Rules: `{"entry_types": ["<signup_type>"], "statuses": ["published"], "match": "all"}`. |
| collection *(suggestion)* | `unsubscribed` | **Unsubscribed.** Smart, private: signup-type entries that are `archived`. Rules: `{"entry_types": ["<signup_type>"], "statuses": ["archived"], "match": "all"}`. |

The collections need no workflow. The confirm and unsubscribe workflows already set the signup's status,
and a smart collection's membership follows the status, so a reader moves from one to the other on their
own. Both are private (**Visible to sites** off), so a confirm or unsubscribe never rebuilds a site and
the publishing API never lists your readers. Applying a collection fills it straight away from the
signups you already have.

Parameters, asked when you apply:

- `integration`: the Buttondown connection. Default `buttondown`.
- `signup_type`: the signup form's submittable entry type. Default `newsletter`.
- `issue_type`: the issue entry type. Default `newsletter-issue`. Its `body` and `preview` fields make the email.

## Setting it up

1. Store the API key as a workspace secret. Connect the integration with `{{BUTTONDOWN_API_KEY}}` and pick the **Issue delivery**. Set a **Site URL** only if the workspace has no Canonical URL.
2. **Apply** the content. Skip the two collections if you don't want them. If the workspace already has a
   collection called `confirmed-subscribers` or `unsubscribed`, Apply leaves it alone; see
   [Upgrading from 0.2](#upgrading-from-02-collection-workflows--smart-collections).
3. Open the `buttondown` incoming webhook (Automation → Incoming webhooks). Click **Mint token** and copy the URL. Under **Signing**, click **Change**, keep the secret `BUTTONDOWN_SIGNING_KEY` and click **Generate key**. You can skip this if the workspace already has that secret. You don't paste the key anywhere: the next step hands it to Buttondown.
4. On the integration's card, run **Connect Buttondown webhooks** with:
   - **webhook_url**: the URL from step 3
   - **signing_key**: `{{BUTTONDOWN_SIGNING_KEY}}`
   - **remove_legacy_url** (optional): the URL of an older hand-built Buttondown hook you're retiring, e.g. a `buttondown-incoming-webhook`
   - **label** (optional): the workspace name
5. Switch on the `buttondown` incoming webhook and the workflows you want. A provider can't switch Marvin's webhooks on, so this one is a click.
6. If you had an older hand-built Buttondown hook in Marvin, delete it (Automation → Incoming webhooks).

Repeat steps 3–6 in **every workspace that shares the Buttondown account**.

### Several workspaces, one Buttondown account

Buttondown webhooks belong to the account, so every workspace on the account gets its own webhook, each pointing at its own Marvin hook URL and signed with its own key. `connect_webhooks` only ever changes the webhook on the URL you give it (plus the exact legacy URL, if you name one). Running it in workspace B leaves workspace A's webhook alone.

Each workspace then receives every confirm and unsubscribe on the account, including readers who signed up through another workspace's site. Those readers have no signup entry in this workspace, so the entry step skips them (`if_none: skip`) and the run stays green.

If a hook token is rotated, run **Connect Buttondown webhooks** again with the new URL and the old one as `remove_legacy_url`.

## Upgrading from 0.2: collection workflows → smart collections

0.2 offered two optional workflows, `buttondown-confirmed-to-collection` and
`buttondown-unsubscribed-from-collection`, that added a signup to a collection on confirm and removed it
on unsubscribe. 0.3 drops them for the two smart collections above. The integration's card no longer
lists the old workflows, but a workspace that applied them keeps them (and they keep running if they
are on) until you delete them.

Apply never overwrites, and a collection can't be updated from its blueprint (only workflows can). So
in a workspace that already has a collection with either slug, the card shows it as already applied,
and applying reports `a collection with slug '…' already exists — left as it is`. Nothing is
converted. Switch it by hand:

1. Automation → Workflows: delete `buttondown-confirmed-to-collection` and
   `buttondown-unsubscribed-from-collection` (or whatever you called the hand-built ones).
2. Collections → `confirmed-subscribers` → **Edit**:
   - tick **Smart Collection**, keep **Collect: Entries**
   - on the **Builder** tab, pick your signup entry type (e.g. `newsletter`) and the status
     **published**, matching **all**. Or paste on the **JSON** tab:
     `{"entry_types": ["newsletter"], "statuses": ["published"], "match": "all"}`. The builder leaves
     out `"match": "all"` because it's the default; the rules are the same.
   - untick **Visible to sites**
   - save. Membership is recomputed from the rules on save. Anything added by hand that doesn't match
     is dropped, and every published signup is added.
3. Do the same for `unsubscribed` with the status **archived**:
   `{"entry_types": ["newsletter"], "statuses": ["archived"], "match": "all"}`.

Or delete the old collections and **Apply** the two from the card instead, if nothing else uses them.
A workspace without either collection just applies them.

## Known limits

- A confirm or unsubscribe for a reader with no signup entry here (they subscribed some other way) is a quiet no-op: the entry step's output says `skipped: true, reason: "no matching entry"`. If a repeat signup left **two** entries with the same subscriber id, the step fails rather than guess.
- **A refused signup goes to Needs review; a refusal is never retried.** When Buttondown refuses an address, the signup workflow's run fails (it shows under **Runs**) and the error policy moves the entry to **Needs review** with the reason, which the Review Queue's card shows. Look at the entry: a firewall refusal (`blocked` / `spammy`) is usually spam you can archive; a real reader can be added by hand in Buttondown. Only an outage, a rate limit or a rejected key is retried (see the table above); an `unknown` failure goes to review straight away.
- **Returning readers are manual for now.** An address that unsubscribed before can't rejoin from a signup: Buttondown answers `subscriber_suppressed`, the run fails and the entry goes to Needs review with code `suppressed`. To let them back in, re-add or re-confirm them in Buttondown (Subscribers → the address → change its type, or send a new confirmation). Their next `subscriber.confirmed` then publishes the entry as usual.
- Updating a Buttondown webhook in place needs a Marvin whose integration HTTP helper has PATCH. On an older Marvin, `connect_webhooks` replaces the webhook instead: it creates the new one, then deletes the old one (`result: replaced`). The outcome is the same, but the webhook gets a new id.
- Marvin versions: `${site.url}` needs 1.0.0-rc.177+. `${entry.url}` and the entry step's `if_none: skip` need the release after it. On an older Marvin they are ignored: no canonical URL is sent (set the connection's Site URL for links), and a reader with no signup entry fails the step. The error policy needs a Marvin that reads SDK 0.5 error policies; an older Marvin ignores it, so a failed signup just fails its run and the entry stays in the inbox.
- **Already applied the content?** Apply never overwrites, so a workspace that applied the signup workflow from 0.3.x keeps its on-failure steps (`set_metadata buttondown_subscribe_error` → `request_review`). Marvin runs a workflow's own on-failure steps instead of the provider policy, so that copy still sends refusals to review, but without the retries. The card marks it with **↑** and an **Update** button; click it to drop the on-failure steps and use the policy (whether it's switched on is kept).

## Develop

```
uv run --extra dev pytest -q
uv run --extra dev ruff check .
```

Resolves the SDK from a sibling checkout (`../MarvinIntegrationSDK`).
