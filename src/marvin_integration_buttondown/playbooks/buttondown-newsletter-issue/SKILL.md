---
name: buttondown-newsletter-issue
description: Use when drafting or revising a newsletter issue that Buttondown will email: the subject, preview and body fields, what Buttondown renders, and why a draft must be ready to send as it is. Write the issue yourself and save it as a draft; never publish it.
---

# A newsletter issue for Buttondown

Check whether the workspace has a voice or style Playbook too, and follow it for tone. This one covers what Buttondown
needs: the fields that make the email, how the body is read, and what happens when the issue is published.

## What publishing does

Publishing an issue runs the `buttondown-issue-on-publish` workflow, which creates the email in Buttondown. How far it goes
depends on the connection's **Issue delivery**: `draft` creates a draft for a person to review and send in Buttondown,
`send` emails your subscribers straight away, `off` does nothing. You usually can't tell which from here, so write every
issue as if it will be sent exactly as saved.

You never publish an issue. You save a draft and hand it back; a person reads it and publishes.

## Write it yourself

Write the subject, the preview and the body yourself, then save them with `create_draft` (a new issue) or `update_draft`
(a change to an existing one). Use `get_entry_type` first to see the issue type's fields. Do not use `compose_entry` or
`revise_entry`: those hand the writing to another AI that never loads Playbooks, so none of this would apply to it.

## The fields

| Field | What it does in the email | Rule |
|---|---|---|
| title | The subject line | Plain and specific, about 60 characters or fewer so most inboxes show all of it. No all-caps, no "Newsletter #12:" prefix unless the workspace already does that. |
| preview | The preview text some inboxes show beside the subject | One sentence, about 140 characters or fewer, that says what the reader will get. It adds to the subject; it does not repeat it. Never "In this issue". |
| body | The email itself | Markdown, see below. |

Look at the last two or three issues with `find_entries` (the issue entry type, newest first) and match their length,
shape and number format before you write.

## The body

Buttondown reads the body as Markdown and wraps it in the newsletter's own template, so the header, the footer and the
unsubscribe link come from Buttondown:

- Do not write a header, a logo, a greeting banner, a footer or an unsubscribe line.
- Headings (`##`), short paragraphs, lists, bold, italic, block quotes, links and images work.
- Tables, raw HTML and anything fancy may not survive every email client. Keep it plain.
- Links must be complete. A site path like `/notes/example/` is made absolute by the integration when the connection has
  a Site URL, but write the full `https://` address when you know it, and use the `url` the tools return for an entry
  rather than building one. If an entry has no url, say so in your reply instead of leaving an empty link.
- Images need a full public address and a short description in the alt text.
- Do not start the body with a line of three dashes (`---`).
- Open with the point, not "Hello everyone". End when you are done.

## Before you hand it back

Go through `references/checklist.md`. `references/example-issue.md` shows the shape of a finished issue.

In your reply give the review link, say that the issue is a draft that sends when someone publishes it (and that the
connection's Issue delivery decides whether Buttondown sends it or keeps a draft), and name anything you were not sure of.
