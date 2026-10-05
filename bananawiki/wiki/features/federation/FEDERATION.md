# Federation

Two BananaWiki installations can pair with each other and share individual
pages read only. It is off by default and does nothing until the operator
turns it on and an administrator on each side enters the other's details.

Set `BW_FEDERATION_ENABLED=1` to enable it. While it is off every federation
URL answers 404, including the endpoint peers call, so a wiki that has not
opted in looks like one that never had the feature. There is no
administrator switch.

The wire protocol is unchanged from 1.4, so 1.4 and 1.6 wikis pair with each
other.

## Pairing

`/admin/federation` shows this wiki's identifier and the paired wikis.
Pairing takes the other wiki's identifier, a name, its HTTPS origin (no
credentials, path or query) and a shared 64-character key. The page proposes
a fresh key; send it to the other administrator over a channel you trust.
Nothing synchronises until both sides have added each other.

A pairing has an audience category: received pages are visible to whoever
can read that category and holds `page.view_all`, plus administrators.
Without one, only administrators see them.

The key is needed in clear to sign requests, so it is stored encrypted with
the instance secret key rather than hashed. Keys stored in plain text by 1.4
keep working and are encrypted by the next poll. After moving the wiki to a
different secret key the key can no longer be read; the admin page says so,
and the pairing has to be made again.

A wiki is only contacted at public internet addresses. For a peer on the
same private network, tick *The other wiki is on a private network* when
pairing; loopback and private addresses are then allowed for that peer only.
Under managed hosting (`BW_MANAGED_HOSTING=1`) the local network is the
host's, so the option is not offered and a peer marked that way earlier is
contacted at public addresses only.
Link-local (cloud metadata), multicast and reserved addresses are always
refused, the address is resolved once and the connection goes to the checked
address, redirects are not followed, and answers are capped at 8 MiB and 25
seconds (`bananawiki/core/http.py`).

At most 16 peers, and at most 32 shared pages per peer.

## Sharing pages

Editors use `/federation/sharing` to share one page with one paired wiki.
Only an active Markdown page the editor may edit can be shared (not hidden,
pending deletion or page-builder pages), at most 128 KiB with a title of at
most 300 characters. Later edits are sent on each synchronisation. A share is
withdrawn automatically when the page stops qualifying, when the person who
shared it may no longer edit it, or when the page moves to another category:
sharing again is a deliberate act. Editors see and withdraw their own shares;
administrators see all of them.

## Receiving pages

`/federation` lists received copies the reader may see; each copy is shown
as plain text, and nothing from the other wiki is executed or loaded. A copy
that has not synchronised for 48 hours stops being shown.

An administrator can fork a copy into an ordinary local page in the
pairing's audience category (refused without one). The fork gets a random
slug, records its source and revision in its first lines, and is created
through the pages service, so it has history, is searchable and other
features hear about it.

## What goes over the wire

A peer fetches `GET /federation/v1/snapshot` without cookies. The request
carries the caller's wiki id (`BW-Wiki`), a Unix timestamp (`BW-Time`), a
random 128-bit nonce (`BW-Nonce`) and `BW-Signature`: HMAC-SHA256 with the
pairing key over `BW-FED-1`, method, path, both wiki ids, timestamp and nonce.
Timestamps outside two minutes are refused and each nonce is accepted once
(`federation_nonces`). A peer is served at most once every 30 seconds (429).
The JSON answer is signed the same way (`BW-FED-1-RESPONSE`, bound to the
request nonce and the SHA-256 of the body) and validated before anything is
stored.

The `federation.poll` job runs every 30 seconds and synchronises the peers
whose next attempt is due; a lease in `federation_peers` makes sure only one
worker polls a peer at a time. A success schedules the next poll five minutes
later; a failure doubles the wait from 30 seconds up to an hour and shows the
reason on the admin page, without repeating anything the remote sent. An
administrator can synchronise a peer immediately. Nothing is fetched before
setup is finished, in maintenance mode, or when the hosting storage quota is
reached.

Federation responses carry `Cache-Control: no-store` and
`Referrer-Policy: no-referrer`.

## Removing a pairing

Disconnecting deletes the peer, the shares pointing at it and the copies
received from it. The other wiki keeps what it already received.

## Tables

`federation_identity` (this wiki's id and sequence), `federation_peers`
(pairings, poll state; 1.6 adds `allow_private_network`),
`federation_page_ids` (stable public id per shared page),
`federation_shares`, `federation_copies`, `federation_nonces`. Times in these
tables are Unix seconds, as in 1.4.
