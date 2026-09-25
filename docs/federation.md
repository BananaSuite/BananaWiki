# Federation

Two BananaWiki installations can pair with each other and share individual
pages read only. It is off by default and does nothing until an administrator
on each side turns it on and enters the other's details.

Set `BW_FEDERATION_ENABLED=1` to enable it. While it is off every federation
route answers 404, including the endpoint peers would call, so a wiki that has
not opted in is indistinguishable from one that never had the feature.

## Pairing

`/admin/federation` shows this wiki's own identifier and the list of peers.
Pairing takes four things: the other wiki's identifier, a name for it, its
base URL, and a shared secret. The page generates a fresh 32 byte secret for
you; send it to the other administrator over a channel you trust, and they
enter the same value. Nothing synchronizes until both sides have done this.

A pairing carries an audience category. Pages received from that peer are
visible to whoever can read that category, plus administrators and owners.
Leaving it empty means administrators only.

At most 16 peers, and at most 32 shared pages per peer.

## Sharing pages out

Editors use `/federation/sharing` to grant a page to a named peer. A grant is
per page and per peer; there is no "share everything". Later edits to a shared
page are sent on the next synchronization until the grant is withdrawn, and
withdrawing it stops further sends. Editors see their own grants;
administrators and owners see all of them. A page above 128 KiB is refused.

## Receiving pages

`/federation` lists what this wiki has received and the reader is allowed to
see. Copies are read only. A copy that has not synchronized successfully for
48 hours stops being shown, so a peer that goes quiet does not leave stale
content on display indefinitely.

An administrator can fork a copy into an ordinary local page. The fork gets a
random slug, so a remote title cannot overwrite an existing page, and its first
lines record where it came from and at which revision. Forking is refused when
the pairing has no audience category, because the fork would otherwise land
somewhere broader than the copy was.

## What goes over the wire

A peer fetches `GET /federation/v1/snapshot`. There are no cookies and no
browser involved. Each request carries the caller's wiki identifier, a
timestamp, a random nonce and an HMAC-SHA256 signature over all of them
computed with the shared secret. Timestamps outside a two minute window are
refused, and a nonce is accepted once. The response body is signed the same
way and is capped at 8 MiB.

The background runtime wakes every 30 seconds and synchronizes the peers whose
next attempt is due. A successful synchronization schedules the next one five
minutes later. A failure doubles the wait, from 30 seconds up to an hour, and
the admin page shows the reason without repeating anything the remote sent.
An administrator can also synchronize one peer immediately from that page.

Synchronization is skipped entirely before setup is finished, while the wiki
is in maintenance mode or banana mode, and, on the hosting platform, when the
instance is at its storage quota.

Federation responses are sent with `Cache-Control: no-store` and
`Referrer-Policy: no-referrer`.

## Removing a pairing

Deleting a pairing removes the peer, the grants pointing at it, and the local
copies received from it. The remote wiki keeps whatever it had already
received; there is no way to reach into another installation and delete it.
