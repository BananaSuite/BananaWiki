# BananaWiki security and stability audit

Branch: `security-audit`. Audit started 2026-10-27.

Each finding lists severity, how it was reproduced, the fix and the test that
guards it. Severities: **Critical** (remote takeover or mass data exposure
without privileges), **High** (privilege escalation, other users' data,
easy site-wide denial of service), **Medium** (exploitable with conditions or
limited impact), **Low** (hardening, defence in depth, minor leaks).

## Method

* Read the documentation (README, ARCHITECTURE, SECURITY, CHANGELOG, docs/).
* Parallel code review per subsystem against the threat model (anonymous,
  user, editor, unapproved/suspended/deleted user, API token or share-link
  holder, federation peer, plugin, hosting tenant, worker PC, sandboxed code).
* Every finding reproduced against the application (test client or a local
  server on 127.0.0.1) before the fix and re-run after it.
* Static analysis: ruff, bandit, pip-audit.

## Findings

### W3-1 Plain administrator re-enables a third-party plugin through the onboarding wizard
- **Severity:** Medium. **Attacker:** administrator who is neither owner nor superuser.
- **Repro:** with a loaded plugin switched off by the owner, `POST /onboarding`
  (`mode=advanced&features=<plugin>`) as a plain admin switched the plugin on
  (its code ran at once, and after every restart), although the plugin page
  refuses the same admin.
- **Fix:** `registry.switch_refusal` refuses switching on a loaded
  third-party plugin to anyone who fails `plugin_manager.service.can_trust_code`,
  so every caller of `set_enabled` (wizard, plugin page, future ones) applies
  the owner/superuser rule; the wizard no longer lists such plugins for them.
- **Test:** `tests/security/test_audit_regressions.py::test_w3_1_*`.

### W3-2 / W1-2 Anonymous requests could send 500 MiB bodies that CSRF spooled to disk, uncounted by the global limit
- **Severity:** Medium (denial of service). **Attacker:** anonymous visitor.
- **Repro:** `MAX_CONTENT_LENGTH` was the largest upload limit (the 500 MiB site
  import), applied to every route; the CSRF check parses the whole multipart
  body before authentication, and it ran before the global 300/min limit.
  `GET /login`, then `POST /login` with the CSRF token and a 200 MiB file: the
  body was spooled to a temporary file before the 401; 400 such POSTs of 8 MiB
  were all read and none answered 429.
- **Fix:** a new first `before_request` applies the global per-client limit
  (before CSRF) and sets `request.max_content_length` to
  `BW_MAX_CONTENT_LENGTH_BYTES` (16 MiB) for every unsafe request, except views
  marked `@auth.large_body` (attachments, canvas and kanban uploads and imports,
  chat sends, custom page files, page-builder images, Markdown imports, plugin
  and site imports, API attachment uploads) called by a live session or a
  bearer token. Each of those views still enforces its own, smaller limit.
- **Test:** `tests/security/test_audit_regressions.py::test_w3_2_*`, `::test_w1_2_*`.

### W6-1 A hard link inside data/ made every backup and rollback package unrestorable
- **Severity:** Medium (a failed update could not roll back). **Attacker:** code running as the service account, or an operator by accident (`cp -al`, sync tools).
- **Repro:** `data/a.bin` and its hard link `data/b.bin`; `write_package` (via
  `tarfile.gettarinfo`) stored `b.bin` as a link member and `read_package`
  refused it ("Archive links and special files are not supported."), so the
  package, including a before-update rollback package, could not be restored.
  Found while fixing the same flaw in BananaChat's shared lifecycle code (C3-10).
- **Fix:** `ops/files.write_package` builds each member's `TarInfo` from the
  open descriptor (every name a regular member); `extract_archive(hard_links=True)`,
  used by `read_package`, restores link members of packages written by earlier
  releases as copies of a file already extracted from the same archive.
- **Test:** `tests/security/test_ops_packages.py::test_w6_1_*`.

### W1-1 Whitespace-padded user names bypass the per-account sign-in limit
- **Severity:** Medium. **Attacker:** anonymous (password guessing against one account from many addresses).
- **Repro:** after 100 failures for `victim` from 100 addresses, `victim` was refused
  with 429, but `"victim" + " " * 44 + "X"` (stripped, cut to 50, stripped again by the
  lookup) kept getting 401 for 30 more guesses, and `"victim" + "　" * 44 + "X"` with
  the right password signed in (302). Each whitespace padding was a fresh
  `login:account` / `login:account-ip` budget. Expected: one budget per account.
- **Fix:** `features/auth/signin.py` `verify()` (shared by `/login`, `/admin/sign-in`,
  `/session-conflict/force` and the portal-merge sign-in) refuses names that no account
  can have (`[A-Za-z0-9_-]{1,50}`: the username rule without the 3-character minimum,
  since 1.4 accepted shorter names) without a lookup, and keys the per-account counters
  on the account found (`id:<user id>`) or, for unknown names, the ASCII-lower-cased name
  (what SQLite NOCASE compares). Two tests in `tests/test_core_limits.py` that seeded the
  old name-based keys now seed the id-based ones. Documented in docs/security.md.
- **Test:** `tests/security/test_wauth.py::test_w1_1_padded_or_recased_names_share_the_account_budget`,
  `tests/security/test_wauth.py::test_w1_1_a_valid_name_still_signs_in`

### W1-3 An administrator's export of another account is missing from the audit log
- **Severity:** Low. **Attacker:** plain administrator (accountability).
- **Repro:** as a plain admin, `GET /admin/users/<alice>/export` returned a ZIP with
  alice's direct messages and IP addresses, and the `audit_log` row count did not change
  (only an application log line), although docs/security.md says exporting private
  conversations is audited.
- **Fix:** `features/user_data_export/routes.py` records `user.data_exported` (target the
  account, details its user name) when the requester exports someone else's data; label in
  the audit catalogues (en, it, de); listed under "Exports and imports" in docs/security.md.
- **Test:** `tests/security/test_wauth.py::test_w1_3_admin_export_of_another_account_is_audited`

### W1-4 A plain administrator changes a superuser account through the custom role it holds
- **Severity:** Low. **Attacker:** plain administrator.
- **Repro:** editor `trusted` with custom role "Support" and `is_superuser=1`; a plain
  admin's direct `change_role` was refused, but `POST /admin/roles/<id>` with
  `base_role=user` moved `trusted` to `user` (302). Deleting the role likewise rewrote the
  holder.
- **Fix:** `features/admin/roles.py` `update()` and `delete()` call
  `_require_manageable_holders` inside their transaction: the actor is reloaded and every
  holder must pass `admin.service.protection_error`; otherwise the change is refused with
  `admin.roles.error.protected_holder` (en, it, de) and nothing is written. The rule is
  the same as for direct changes, so a role held by a superuser is changed only once that
  account is no longer a superuser or no longer holds it. Documented in docs/permissions.md.
- **Test:** `tests/security/test_wauth.py::test_w1_4_plain_admin_cannot_demote_a_superuser_through_its_custom_role`,
  `tests/security/test_wauth.py::test_w1_4_roles_held_only_by_manageable_accounts_still_change`

### W1-5 A temporarily promoted administrator keeps the role by switching the expiry feature off
- **Severity:** Low. **Attacker:** account given a temporary admin role by an owner.
- **Repro:** owner schedules `tempadmin` back to `user` (already expired); `tempadmin`
  `POST /admin/plugins/temporary_accounts/disable` succeeded, the expiry job no longer
  ran and the role stayed `admin` indefinitely.
- **Fix:** the rule is that nobody may pause a schedule they could not cancel. The
  `temporary_accounts` feature adds a `feature.switch` interceptor
  (`hooks.refuse_switch_off` -> `service.switch_off_error`): switching the feature off is
  refused to anyone but owners and superusers while any `temp_roles` / `temp_users`
  schedule waits for an account the person could not remove the schedule of
  (`removal_error`: their own, another administrator's). Because it goes through
  `registry.switch_refusal`, the plugin manager, the onboarding wizard (which then does not
  offer it) and BM-mode hiding all follow it. The plugin manager's disable view now catches
  `registry.SwitchRefused` and flashes the reason instead of failing. Owners keep the
  documented "switching off pauses every schedule". Message in en, it, de; docs/features.md
  and docs/permissions.md updated.
  **Accepted risk:** a temporary administrator holds every administrator power until the
  revert, including `create_user` and invite codes up to administrator; accounts and codes
  made meanwhile do not expire with the promotion. Plain administrators creating
  administrators is the documented design (owners and superusers stay above them and can
  remove such accounts); the docs now say to promote temporarily only people trusted as
  administrators. Invite codes re-check the creator's authority at redemption, so codes
  stop granting admin once the revert ran.
- **Test:** `tests/security/test_wauth.py::test_w1_5_temporary_admin_cannot_switch_the_feature_off`,
  `tests/security/test_wauth.py::test_w1_5_another_plain_admin_cannot_pause_the_revert_either`,
  `tests/security/test_wauth.py::test_w1_5_owners_still_switch_the_feature_off`,
  `tests/security/test_wauth.py::test_w1_5_plain_admin_switches_off_when_only_user_schedules_wait`

### W1-6 The merge-request form discloses protected accounts before the password and blocks other people's merges
- **Severity:** Low. **Attacker:** any signed-in user.
- **Repro:** `POST /settings/merge-request` with `other_username=<owner>` and a wrong
  password answered "Protected accounts cannot be merged." / "The last owner ..." instead
  of the wrong-password message (also "no such user" before the password). A request also
  marked the other account, and every later merge involving it was refused
  (`already_pending`) until that person denied the stranger's request.
- **Fix:** `features/users/merge.py` `create()` checks the requester's password first,
  then the existence of the other account (the route passes `None` instead of answering
  itself), and answers every pair problem except "same account" with one generic
  `users.merge.error.not_possible` (en, it, de); administrators still see the precise
  reason in the direct merge. Only a request an account confirmed itself (made it, or
  confirmed its side) keeps it out of other merges (`_committed`), checked in `create()`
  for both accounts and in `confirm()` for the confirming account, so each account still
  commits to at most one open merge. Documented in docs/features.md.
- **Test:** `tests/security/test_wauth.py::test_w1_6_merge_form_tells_nothing_about_the_other_account_without_the_password`,
  `tests/security/test_wauth.py::test_w1_6_an_unconfirmed_request_does_not_block_the_other_account`

### W1-7 An account merge transfers the hosting-portal sign-in link
- **Severity:** Low. **Attacker:** n/a (consented or administrator merge); whoever controls the source's portal account.
- **Repro:** source with a `platform_oauth_links` row, target without one; after
  `merge.execute` the link pointed to the target, so the source's portal account signed
  straight in to the target, although credentials are documented as never transferred.
- **Fix:** `platform_oauth_links` added to `KEEP_WITH_SOURCE` in
  `features/users/merge.py`: the link stays on the locked source (suspended, so it only
  reaches the status page) or is deleted with it (`ON DELETE CASCADE`). No portal call is
  made inside the merge transaction: a portal link naming a deleted wiki user resolves to
  nobody (`realign_portal_link` finds no account). docs/features.md lists the credentials
  that stay with the source.
- **Test:** `tests/security/test_wauth.py::test_w1_7_merge_keeps_the_portal_link_with_the_source`

### W3-3 API tokens bypass the "two-step verification required for administrators" gate
- **Severity:** Low. **Attacker:** holder of an administrator's API token issued before the requirement.
- **Repro:** admin `boss` with a token; owner sets `two_step_required_for_admins=1`. The
  browser was locked to `/settings/two-step`, but the token kept `GET /users` 200,
  `POST /users {role: admin}` 201 and `POST /tokens` 201.
- **Fix:** `features/api_service/api/__init__.py` `_account_gate` asks the same
  `account.pending_step` interceptors as the web pipeline and answers
  `403 two_step_required` (or `account_step_required` for any other feature step).
  Messages in en, it, de; docs/api.md and docs/security.md updated.
  `tests/features/test_auth_two_step.py::test_api_tokens_keep_working_while_it_is_required`
  asserted the insecure behaviour; it is replaced by
  `test_api_tokens_wait_until_a_required_second_step_is_on` (refused, then working again
  once the administrator turned two-step verification on). Compatibility: admin scripts
  stop until the administrator turns it on, which is the documented intent.
- **Test:** `tests/security/test_wauth.py::test_w3_3_admin_token_stops_until_two_step_is_turned_on`

### W3-4 No attempt limit on the /setup token
- **Severity:** Low. **Attacker:** anonymous visitor before setup is finished.
- **Repro:** repeated `POST /setup` with a wrong `setup_token` answered 403 every time,
  never 429; `BW_SETUP_TOKEN` had no minimum length, so a short operator-chosen token
  could be guessed for the owner and superuser account.
- **Fix:** `features/auth/routes.py` `_setup_token_refusal()`: at most 10 token attempts
  per client network in 15 minutes (`SqlLimiter`, bucket `setup:token`, shared by all
  workers; 429 after that), and in production a configured token shorter than 20
  characters is refused at `/setup` with an explanation (`auth.setup.token_too_short`, en,
  it, de). The length check is at use rather than at config load on purpose: refusing it
  at load would stop an upgraded production wiki from starting over a token it never
  uses again once setup is done. Documented in docs/configuration.md and docs/security.md.
- **Test:** `tests/security/test_wauth.py::test_w3_4_setup_token_guesses_are_limited`,
  `tests/security/test_wauth.py::test_w3_4_production_refuses_a_short_setup_token`

### W4-5 Deleted accounts' group-chat messages keep their IP addresses
- **Severity:** Low. **Attacker:** plain administrator.
- **Repro:** a member posts in a group from 198.51.100.7, then the account is deleted:
  `group_messages.sender_id` became NULL but `ip_address` stayed, and `address_visible`
  treats a deleted sender as unprotected, so every administrator saw it (also for former
  owners and superusers).
- **Fix:** the chat feature registers a `user.delete` interceptor
  (`chat/groups.forget_addresses`; `registry.prepare` runs it even while chat is
  switched off) that blanks `ip_address` of the account's group messages in the deletion
  transaction; `ops/tenant_task.remove_user`, which deletes users without the wiki
  application, does the same. The other tables holding the deleted account's addresses
  already go with it (`ON DELETE CASCADE`): `user_sessions`, `chat_messages` (direct
  messages), `api_service__audit_log`; `rate_limit_hits` expire within a day.
  The audit log keeps a deleted actor's address by design: deleting an account (or
  having it deleted) must not erase the record of what it did, and owners control the
  retention. Because the actor's rank can no longer be checked, the audit page now shows
  those addresses only to owners and superusers (`audit/routes._hide_protected_addresses`).
  Documented in docs/security.md.
- **Test:** `tests/security/test_wauth.py::test_w4_5_deleted_accounts_group_messages_lose_their_address`,
  `tests/security/test_wauth.py::test_w4_5_audit_addresses_of_deleted_accounts_are_for_owners_only`

### W1 (coverage) Profile images of accounts deleted by others stay until the daily clean-up
- **Severity:** Low. **Attacker:** n/a (data minimisation).
- **Repro:** a member uploads an avatar; an owner deletes the account at
  `/admin/users/<id>/edit` (`action=delete`): the file stayed in `uploads/avatars` until
  `users.collect_orphan_images` ran (daily); only self-deletion removed it.
- **Fix:** the users feature notes the account's avatar and background in a `user.delete`
  interceptor and deletes them in its `user.deleted` handler (after the deletion is
  committed and announced, whoever deleted the account: administrator, schedule, sign-up
  clean-up, merge), unless another account still shows the file (a background a merge
  carried over). `users.service.delete_account` relies on the same path.
- **Test:** `tests/security/test_wauth.py::test_w1_coverage_admin_deletion_removes_profile_images`

### W5-W1 A transfer offered before moderation brings a suspended or doomed owner's wiki back online
- **Severity:** Medium. **Attacker:** hosting tenant with a second approved account (a sock) that has room under its wiki limit.
- **Repro:** The abuser offered its wiki to the sock (`POST /instances/<id>/transfer`), an administrator suspended the abuser's account (default form: wikis not suspended with it) or scheduled its deletion, then the sock accepted (`POST /transfers/<tid>/accept`). Observed: owner = sock, status running, `domains.may_serve` True (with scheduled deletion the wiki also escaped the deletion). Expected: the offer is cancelled or refused.
- **Fix:** `collaborators.accept_transfer` refuses and cancels an offer for a suspended wiki or from an owner that is suspended, denied, deleted or pending deletion (new message `hosting.transfers.moderated`, en/it/de), and a moderated recipient cannot accept. New `accounts.cancel_transfers` cancels the account's pending offers in `accounts.suspend` and `schedule_deletion`, and offers in both directions in `accounts.delete`; `instances.suspend` cancels the wiki's pending offer. docs/hosting.md updated.
- **Test:** `tests/security/test_whost.py::test_w5_w1_transfer_offered_before_moderation_cannot_be_accepted`, `::test_w5_w1_accept_refuses_a_moderated_owner_even_with_the_offer_still_pending`, `::test_w5_w1_suspending_the_wiki_cancels_its_pending_transfer`, `::test_w5_w1_unrelated_transfers_survive_a_suspension`

### W5-W2 The owner of an admin-suspended wiki can still transfer it and manage its collaborators
- **Severity:** Low. **Attacker:** owner of an admin-suspended wiki.
- **Repro:** With the wiki suspended (`instances.suspend`), the owner's `POST /instances/<id>/transfer` created a pending transfer and `POST /instances/<id>/collaborators/add` (role `full_access`) added a collaborator; `use-case` edits also went through. Only stop/restart/reset/terminate were locked. Expected: refused while suspended.
- **Fix:** `routes/dashboard.py` applies the `owner_locked` check (new `_locked` helper) to every mutating owner action: use-case, feature requests, adding and changing collaborators, offering a transfer, and claiming, verifying or checking a custom domain (the background domain check no longer verifies on a locked wiki). Removing collaborators, cancelling a transfer and removing a domain stay possible (they only take access away). `collaborators.start_transfer` also refuses a suspended wiki at the service layer. Administrators are unaffected. docs/hosting.md lists what a suspended wiki's owner can and cannot do.
- **Test:** `tests/security/test_whost.py::test_w5_w2_owner_of_a_suspended_wiki_cannot_transfer_or_add_collaborators`

### W5-W3 "Sign out other devices" leaves platform sign-in sessions on wikis alive
- **Severity:** Low. **Attacker:** a thief holding a stolen portal session.
- **Repro:** The thief's portal session signed in to a wiki through platform sign-in and obtained an access token. The owner then used **Sign out other devices** (`POST /account/sessions/logout-all`). Observed: the thief's portal session ended, but no `hosting_oauth_signouts` row was queued and `/oauth/userinfo` still answered 200 with the token (valid up to 1 h). Expected: the wiki sessions and tokens end too.
- **Fix:** `routes/account.py::logout_everywhere` also calls `oauth.sign_out_everywhere`, which queues sign-out events for every wiki and now drops unused authorization codes as well as access tokens (`revoke_account_credentials`; this also applies to `/oauth/revoke`). Wiki sessions cannot be told apart by portal device, so the current device's wiki sessions end too. The flash message, the wiki sign-ins hint (en/it/de) and docs/hosting.md ("Single sign-out", account security list) say so.
- **Test:** `tests/security/test_whost.py::test_w5_w3_signing_out_other_devices_reaches_platform_sign_in`

### W5-W4 An admin opening a customer's wiki page uses up the owner's one-time 1.4 initial password
- **Severity:** Low. **Attacker:** platform admin (trusted); integrity and privacy issue.
- **Repro:** For a migrated wiki with `admin_password_plain` set, an administrator's GET of `/instances/<id>` (directly or while impersonating) showed the password and erased it; the owner never saw it. Expected: only the owner is shown it.
- **Fix:** `routes/dashboard.py::instance_detail` calls `instances.take_legacy_password` only for the owner signed in as themselves (`is_owner and not auth.impersonating()`).
- **Test:** `tests/security/test_whost.py::test_w5_w4_admin_viewing_a_migrated_wiki_leaves_the_owners_initial_password`

### W5-1 Platform backups fail as a whole past the AES-GCM single-nonce limit (~64 GiB)
- **Severity:** Medium. **Attacker:** none needed (platform growth); tenants can speed it up by filling their quota with incompressible data.
- **Repro:** With cryptography's `GCM._MAX_ENCRYPTED_BYTES` scaled down to 128 KiB and a wiki holding 256 KiB of random data, `runtime.export_platform(work)` raised `ValueError: Exceeded maximum encrypted byte limit` (not a RuntimeFailure) and wrote no backup at all. The scheduled Drive backup then failed hourly with a generic log line, the admin export answered 500 and left its `bwh-platform-*` folder, and backups over `HOSTING_IMPORT_MAX_BYTES` (10 GiB) were refused by the restore without warning. Expected: the backup succeeds, or fails clearly.
- **Fix:** New backup format `BWBACKUP2` in `hosting/runtime/crypto.py` (the STREAM construction): per-file key from HKDF-SHA256(backup key, random 32-byte salt), 1 MiB AES-256-GCM segments, nonce = 11-byte segment number + final flag, header as associated data, so truncation, reordering, appending and header changes are detected and size has no practical limit. `decrypt_file` still reads `BWBACKUP1` (1.4 to 1.7.3). The admin export removes its work folder on any error; the scheduled Drive backup logs unexpected errors with a traceback (still retried only after `GDRIVE_RETRY_SECONDS`); the latest backup's size is recorded, and the platform settings warn (and the portal logs) when it exceeds `HOSTING_IMPORT_MAX_BYTES`. docs/hosting.md documents the format and restore limits, docs/configuration.md the limits. Compatibility: 1.7.3 and earlier cannot restore a `BWBACKUP2` backup (the secure, working option; old backups stay restorable). `tests/hosting/test_runtime_backups.py`'s independent decryptor was rewritten for `BWBACKUP2`, since it encoded the old format.
- **Test:** `tests/security/test_whost.py::test_w5_1_platform_backup_goes_past_the_single_gcm_stream_limit`, `::test_w5_1_segmented_backups_refuse_truncation_and_reordering`, `::test_w5_1_settings_warn_when_the_latest_backup_is_larger_than_a_restore_accepts`, `::test_w5_1_an_unexpected_export_error_leaves_no_work_folder`

### W5-2 The tenant host firewall is required but never checked or loaded
- **Severity:** Low. **Attacker:** hosting tenant with code execution in its container (plugins on, or a sandbox/SSRF-guard bypass).
- **Repro:** Code review: nothing in `install`, `update`, `restart`, `status` or the agent checked that `inet bananawiki_tenants` was loaded, so after a reboot without a persistent nftables include, tenants silently reached host services, the LAN and cloud metadata.
- **Fix:** `System.tenant_firewall()` (ops/system.py) runs `nft list table inet bananawiki_tenants` as the root controller. `Manager.check_tenant_firewall` runs it on `status` (`tenant_firewall` field plus a warning), `start`/`restart` (cli.lifecycle, `firewall_warning`), `update` (the hourly automatic one included) and `install`, printing a warning; it records the answer and the boot id in `config/host-firewall.json`. The runtime agent (whose own network namespace is private) passes the record on in `ping` (`ops/firewall.recorded`, which treats a check from an earlier boot as unknown), and the portal's administrators' dashboard shows an error while the table is missing and a warning while it was not checked since the host started (en/it/de). Wikis are not refused (warn, not block). docs/hosting.md and docs/operations.md updated. Tests mock the probe; nothing runs as root.
- **Test:** `tests/security/test_whost.py::test_w5_2_status_warns_and_records_a_missing_tenant_firewall`, `::test_w5_2_the_probe_reports_nft_missing_or_refusing`, `::test_w5_2_a_check_from_before_the_last_boot_counts_as_none`, `::test_w5_2_the_admin_dashboard_calls_attention_to_the_firewall`, `::test_w5_2_the_portal_reads_the_firewall_check_the_agent_passes_on`

### W2-5 A page rename rewrites links in pages the renamer cannot read, write or that are protected, crediting them
- **Severity:** Low. **Attacker:** category-restricted editor.
- **Repro:** Mallory (read/write only in "Open") created "Target X", which a hidden, protected page in "Secret" linked to, then renamed it. Observed: the hidden page's text pointed to `/page/chosen`, `last_edited_by` and a history entry named Mallory, and other users' drafts were rewritten. Expected: pages the renamer may not edit stay untouched.
- **Fix:** `pages/service.py` `_rewrite_links` now rewrites only pages (and their drafts) that the renamer may `can_edit` and that are not `edit_blocked` for them (protection, someone else's check-out, pending deletion). Those pages keep the old link, and the skip is logged. System renames without an actor still rewrite every page. Docs updated: features.md and the en/it user guides.
- **Test:** `tests/security/test_wcontent.py::test_w2_5_rename_leaves_pages_the_renamer_may_not_edit`

### W2-6 Category delete with move/uncategorize moves protected and checked-out pages and reparents subcategories the editor cannot write
- **Severity:** Low. **Attacker:** editor with `category.delete` and write access to the category.
- **Repro:** A page in "Team" was protected by another editor, so the restricted editor's direct `/page/<slug>/move` was refused. `POST /category/<Team>/delete` with `page_action=move` or `uncategorize` still moved it. Restricted editors could also leave pages at the top level, and a child category they cannot read was reparented. Expected: the same rules as moving each page or category.
- **Fix:** `pages/categories.py` `delete_with_pages`, which the web, REST API and bulk deletion all use, now refuses with `PagesRefused` before changing anything in these cases:
  - `hidden`: the category holds pages the user cannot view.
  - `blocked`: a page is protected, checked out or pending deletion (API 409).
  - `top_level`: uncategorising needs top-level write access the user lacks (API 403).
  - `subcategories`: subcategories would move up without write access to each of them and to the new parent (API 403).
  
  New messages added in en/it/de, and the "blocked" text now says "moved or deleted". docs/api.md and features.md updated.
- **Test:** `tests/security/test_wcontent.py::test_w2_6_category_delete_does_not_move_protected_pages_or_foreign_subcategories`

### W2-7 History revert and contribution approval rename a page without `page.edit_metadata`
- **Severity:** Low. **Attacker:** editor with `history.revert`, or a reviewer together with any proposer, in both cases without `page.edit_metadata`.
- **Repro:** `/edit/title` returned 403. Then `POST /page/<slug>/revert/<first>` restored the old title. Approving a proposal titled "Hijacked title" also renamed the page. Expected: the current title is kept.
- **Fix:**
  - `pages/service.py` `revert` restores the entry's title only when the acting user (resolved from `author_id`; the system always may) passes `access.can_edit_metadata`.
  - `contributions/service.py` `approve` applies a proposed title only if the reviewer may edit metadata.
  - The review screen says when a proposed title will not be applied (new string in en/it/de). Docs updated.
- **Test:** `tests/security/test_wcontent.py::test_w2_7_history_revert_keeps_the_title_without_edit_metadata`, `tests/security/test_wcontent.py::test_w2_7_contribution_approval_keeps_the_title_without_edit_metadata`

### W2-8 An editor's history revert makes a page-builder page public again
- **Severity:** Low. **Attacker:** editor with `history.revert`.
- **Repro:** An admin published builder version V1 as public, then withdrew it (V2, not public). An editor reverted to V1. Observed: `builder_public=1` again. Expected: only administrators publish publicly.
- **Fix:**
  - `pages/service.py` `revert` restores `builder_public` only when the actor may publish publicly (an administrator, and the host allows it, or the system).
  - Anyone else's revert leaves the page non-public until an admin publishes it. This follows the page builder's own rule that edits by others need fresh approval. It is more conservative than keeping the current flag, which would expose an old, never-approved version publicly.
  - The rule is now `pages.service.may_publish_public`, and `page_builder.service.may_publish_public` delegates to it.
- **Test:** `tests/security/test_wcontent.py::test_w2_8_editor_revert_does_not_make_a_builder_page_public_again`

### W2-9 Moving a page into the docs category, then deleting it, skips the 48-hour grace period
- **Severity:** Low. **Attacker:** editor with `page.delete`, where `docs_bypass_deletion_slowdown` is on.
- **Repro:** An ordinary deletion gave `pending_deletion=1`. A page moved into the docs category with `/page/<slug>/move` and then deleted was removed at once, with its history. Expected: it is scheduled like any page.
- **Fix:**
  - `deletion_slowdown/service.py` `bypasses` now also requires that the documentation installer wrote the page: its first history entry is by the system with the installer's message (`auth/docs.py` `EDIT_MESSAGE`). Other pages in that category wait the grace period.
  - The old `test_docs_bypass`, which expected the bypass for a moved page, now uses the installer.
- **Test:** `tests/security/test_wcontent.py::test_w2_9_moving_a_page_into_the_docs_category_keeps_the_grace_period`

### W2-10 Admin skips the 72-hour protection unlock delay by making the page the home page, unaudited
- **Severity:** Low. **Attacker:** administrator.
- **Repro:** A page was protected by an editor, so the admin's edit was refused. The admin set it as home, edited it, then set the old home back. Observed: the edit went through and nothing was audited. Expected: protection holds against admins until the unlock delay has passed.
- **Fix:** `pages/editing.py` `set_home` and `pages/api.py` `api_set_home` now refuse while the page is `edit_blocked` for the admin: protected, checked out by someone else, or pending deletion. The web route shows the reason and the JSON route returns 423. As a result, a pending-deletion page must be restored before it can become home. Documented in features.md.
- **Test:** `tests/security/test_wcontent.py::test_w2_10_administrator_cannot_lift_a_protection_by_making_the_page_home`

### W2-11 Protection has no quota or rate limit: one editor locks every page for 72 hours
- **Severity:** Low. **Attacker:** editor (with governance on).
- **Repro:** One editor protected 120 of 120 pages in one burst, after which admin edits were refused. Expected: a bounded number of protections.
- **Fix:**
  - `page_governance/protection.py` `protect` counts the editor's protected pages against their reservation quota (`quota.effective`; the default is `default_reserved_pages_quota`). The count is kept apart from reservations and checked inside the write transaction. Admins can raise the quota per user or make it unlimited.
  - `POST /page/<slug>/protection` is rate limited to 20 per minute per account.
  - No schema change, so the existing quota requests apply.
  - Docs now name demoting the controller as the emergency lever.
  - Compatibility: existing protections over the limit stay, but new ones are refused until the editor is below their quota.
- **Test:** `tests/security/test_wcontent.py::test_w2_11_one_editor_cannot_protect_every_page`

### W2-12 /my-contributions shows the current title and address of pages the proposer can no longer read
- **Severity:** Low. **Attacker:** a user whose read access to a page was removed.
- **Repro:** A reader proposed an edit. The page then moved into a category the reader cannot read and was renamed "Layoffs Q3 list". Observed: `/my-contributions` showed the new title and slug. Expected: as with My drafts, only the proposal's own title.
- **Fix:** `contributions/routes.py` `_my_contributions` checks `pages.can_view` for each proposal. A page the user cannot view shows the proposal's stored title with a "page not available" badge (new string in en/it/de), no link and no edit/withdraw buttons.
- **Test:** `tests/security/test_wcontent.py::test_w2_12_my_contributions_hides_pages_the_proposer_can_no_longer_read`

### W2-13 An editor blocked by protection or a check-out can delete a page's read-aloud audio
- **Severity:** Low. **Attacker:** editor blocked by a protection, someone else's check-out or a pending deletion.
- **Repro:** A page with queued audio was protected by another editor. `POST /page/<slug>/tts/cancel` by a third editor returned 200 and deleted the audio. Expected: refused.
- **Fix:** `tts/routes.py` `cancel` also requires `not page_access.edit_blocked(page)` and answers 423 with the reason, as attachments and other page changes do.
- **Test:** `tests/security/test_wcontent.py::test_w2_13_blocked_editor_cannot_delete_read_aloud_audio`

### W4-2 Administrators clear or delete private groups and delete members' messages without audit entries
- **Severity:** Medium. **Attacker:** plain administrator who is not a group member.
- **Repro:** Alice created a private group with Bob and posted "secret evidence". A plain admin used `/groups/<id>/clear`, `/groups/<id>/delete`, `/admin/groups/<id>/delete` and `/groups/<id>/delete_message`. Observed: no audit_log rows. Expected: entries, as the docs promise.
- **Fix:**
  - `chat/routes_groups.py` records these entries when the admin acts through the site role rather than a role in the group (`Actor.moderates_as_admin` / `manages_as_admin`; the global room always counts):
    - `chat.group_cleared`, with message and attachment counts;
    - `chat.group_deleted`, with name and counts;
    - `chat.group_message_deleted`, with message and sender ids.
  - `chat/routes_admin.py` `admin_group_delete` always records `chat.group_deleted`.
  - Audit labels added in en/it/de, and docs/features.md and docs/security.md updated.
- **Test:** `tests/security/test_wcontent.py::test_w4_2_administrators_moderating_groups_they_are_not_in_are_audited`

### W4-3 Canvas image upload bypasses the daily upload quota
- **Severity:** Low. **Attacker:** any user with edit access to a canvas.
- **Repro:** With `upload_quota_per_day_count=2`, a canvas editor uploaded 3 images through `/canvas/<slug>/upload` and all returned 200. The quota counters were untouched. Expected: the third is refused.
- **Fix:**
  - The quota logic of page images is now `pages/uploads.py` `save_counted`, which `save_image` uses.
  - `canvas/routes.py` `upload` goes through it and answers 429 on `QuotaExceeded`.
  - Canvas uploads still need only canvas edit access, not page-image permission: canvases are shared with plain users by design.
  - Docs updated.
- **Test:** `tests/security/test_wcontent.py::test_w4_3_canvas_image_uploads_count_against_the_daily_upload_quota`

### W4-4 Kanban board owners get the full account directory with roles, bypassing `search.users`
- **Severity:** Low. **Attacker:** any user who owns a kanban board (`kanban.create`).
- **Repro (code-confirmed, then tested):** The board page rendered `SELECT id, username, role FROM users` into the share and transfer dialog for every owner. That list included pending and suspended accounts, and also went to owners without `search.users`. Expected: no directory without `search.users`, and no roles.
- **Fix:**
  - `kanban/views.py` `_share_directory` lists only active, approved accounts, without roles, and only to owners holding `search.users`.
  - Other owners get a user-name field. `_share_target` resolves it for add_user and transfer_ownership.
  - Template and en/it/de hint added, and features.md updated.
  - Not changed: `access.assignable_users`, which lists only people who can reach the board (all global-access accounts for public boards, by design).
- **Test:** `tests/security/test_wcontent.py::test_w4_4_kanban_sharing_dialog_does_not_list_every_account_and_role`

### W4-6 `/badges/reading` returns 500 on a non-object JSON body, and reading time can be inflated
- **Severity:** Low. **Attacker:** any signed-in user.
- **Repro:** The body `[1]` returned 500 (`AttributeError`). Scripted beats credited 120 s for every 50 s window, even when no reading badge existed. Expected: 400, and reading time no faster than the clock.
- **Fix:**
  - `badges/routes.py` answers 400 for a body that is not an object, and records nothing while no reading-time badge is tracked.
  - `badges/service.py` `add_reading_time` credits at most the time since the previous credited report, still capped at 120 s so a missed report is caught up. It does this inside a transaction.
- **Test:** `tests/security/test_wcontent.py::test_w4_6_reading_heartbeat_refuses_odd_bodies_and_follows_the_clock`

### W2-1 Quadratic Pygments lexers break the highlighting budget: one request costs minutes of CPU
- **Severity:** High. **Attacker:** editor (preview, page save/view, code highlighting API, page-builder code blocks); anyone with write access to a kanban board (descriptions, comments) or a canvas (text and code nodes).
- **Repro:** `scratchpad/W2/x/test_lexer_dos.py`: an editor's `/api/preview` of a ```` ```csharp ```` block of `"x\n"` lines took 1.3 s (4 KB), 5.0 s (8 KB) and 19.7 s (16 KB); the 64 KB allowance came to about 5 minutes. A scan of every lexer found many quadratic ones and some that yield no token until they are done: C/C++ need about 0.65 s for one token on 4 KB, Easytrieve is cubic and Maple exponential (48 characters: 0.8 s). Expected: about 1 s per document at worst. After the fix the same repro takes 0.3 / 0.5 / 0.5 s, and the worst of the known adversarial inputs at 64 KB costs about 0.55 s per document. Ordinary 60 KB code in 14 common languages still highlights (0.1 to 0.36 s).
- **Fix:** `bananawiki/wiki/markdown.py`: all highlighting (fenced and indented blocks, `highlight_code`) goes through `_highlight`. It lexes code in pieces of at most 1 KB, cut between lines and preferably before a first-column line (`HIGHLIGHT_CHUNK_CHARS`, `_code_chunks`). Code with a longer line is formatted without colours. Each document has a processor-time clock (`MAX_HIGHLIGHT_SECONDS` = 0.5 s, `time.thread_time`), checked between tokens. When it runs out, that block and every later one stay plain code. Lexers that are slow even on 1 KB (`_UNBOUNDED_LEXERS`: Maple, Easytrieve) are never coloured. `scripts/measure_lexers.py` re-measures every lexer, and a test pins `MEASURED_PYGMENTS_VERSION`, so a Pygments upgrade must be re-measured. The time is shared by the blocks of a page-builder page and a ticket's comments (`shared_highlighting`) and by the nodes of a canvas answer (`bounded_highlighting` in `canvas/present.py`). Results cut short by shared time are not cached. The render cache is now single-flight (`_RenderCache.making`): concurrent first readers wait for one rendering. `docs/security.md` ("Rendering is bounded") is updated. Compatibility: code in blocks over about 7 KB in some slow lexers, lines over 1 KB, and Maple/Easytrieve are shown uncoloured. Multi-line constructs that cross a 1 KB piece boundary can be coloured as if they ended there.
- **Test:** `tests/security/test_wperf.py::test_w2_1_highlighting_stops_at_deadline`, `::test_w2_1_lexers_that_answer_late_see_short_pieces`, `::test_w2_1_canvas_nodes_share_one_highlighting_time`, `::test_w2_1_concurrent_first_readers_render_once`, `::test_w2_1_the_lexers_were_measured_with_the_installed_pygments`

### W2-2 PDF export builds an unbounded in-memory PDF from up to 50 images
- **Severity:** Medium. **Attacker:** account holding `page.export_pdf` (off by default).
- **Repro:** `scratchpad/W2/a/test_pdf_jpegs.py`: 50 accepted 12.7 MB JPEGs gave a 609 MiB PDF and a 2071 MB peak per export. GIF, PNG and WEBP images were decoded by fpdf2 outside `storage.image_slot()`, and any number of exports could build at once. After the fix, 20 such images take 3.0 s, give a PDF under 1 MiB and a 356 MB maxrss (including the test's own image generation).
- **Fix:** `bananawiki/wiki/features/page_export/pdf.py`: the simplifier emits `bw-image:<n>` markers. When the PDF is laid out (cache miss only), each image is decoded inside `storage.image_slot()`, large JPEGs at a reduced draft scale, and re-encoded as a JPEG on white of at most 1400x2000 px (`_picture`). Limits per PDF: 160 megapixels decoded (`MAX_DECODED_PIXELS`) and 32 MB embedded (`MAX_IMAGE_BYTES`). Images past either budget show their alt text. Each process lays out one PDF at a time (`_build_slot`); an export that waits more than 10 s raises `Busy`. The route then redirects to the page with a translated flash (`page_export.pdf.busy`, en/it/de). Docs: `features.md` and `security.md`.
- **Test:** `tests/security/test_wperf.py::test_w2_2_pdf_pictures_are_decoded_in_the_slot_downscaled_and_budgeted`, `::test_w2_2_one_pdf_is_laid_out_at_a_time`

### W2-3 "Download all attachments" re-compresses every attachment synchronously with no cap
- **Severity:** Medium. **Attacker:** any signed-in reader (`attachment.view` is a default).
- **Repro:** `scratchpad/W2/a/test_attach_zip.py`: 3 × 100 MiB attachments were deflated into a temporary file before the first byte was sent (4.9 s, the full archive on disk), on every request, with nothing limiting parallel requests. After the fix the archive is streamed in pieces with `ZIP_STORED` and no temporary file, and a third concurrent archive in the process is refused.
- **Fix:** `bananawiki/wiki/features/attachments/service.py`: `zip_files` resolves the files and names, and `zip_stream` writes a stored, uncompressed ZIP to an unseekable sink and yields it while it is made. At most `MAX_ZIP_STREAMS` (2) archives are sent per process. `routes.py` returns a streaming `Response`, and when busy it redirects with the flash `attachments.flash.zip_busy` (en/it/de). Docs: `features.md` (Attachments).
- **Test:** `tests/security/test_wperf.py::test_w2_3_attachment_zip_is_streamed_stored_and_limited_per_process`

### W2-4 A page rename rewrites every linking page with a full history copy each, no cap
- **Severity:** Medium. **Attacker:** editor (default permissions).
- **Repro:** `scratchpad/W2/b/test_w2b_rename_scale.py`: one rename of a page linked from 300 pages of 900 KB each added 270 MB of history (about 2.7 GB a minute at the rename rate limit). After the fix, the history written by one rename is at most 4 million characters, and every linking page still gets the new link.
- **Fix:** `bananawiki/wiki/features/pages/service.py`: `_rewrite_links` stores history versions for rewritten pages only until `MAX_REWRITE_HISTORY_CHARS` (4,000,000, measured as `length(content) + length(builder_json)`). Later pages are rewritten through `update(..., record_history=False)`: the new links, with no version of their own; their previous version stays in the history. Documented in `features.md` (Page history). W2-5 (authorship and permissions of these rewrites) is not covered here.
- **Test:** `tests/security/test_wperf.py::test_w2_4_rename_copies_a_bounded_amount_of_linking_pages_into_history`

### W3-5 Some API reads are not paginated, against the documented convention
- **Severity:** Low. **Attacker:** API token holder with the `categories` (or `canvas`/`kanban`) scope.
- **Repro:** `scratchpad/W3/tests/test_api_gates.py::test_category_detail_unbounded`: `GET /api/v1/categories/<id>?limit=10` returned all 600 pages. The canvas list, canvas history and board list read every row into Python before slicing. After the fix the category answer holds `limit` pages with `next_offset`, and the other listings read only their window.
- **Fix:** `api_service/api/categories.py`: `GET /categories/<id>` pages its `pages` with `limit`/`offset` (at most 500, the default) and adds `limit`/`offset`/`next_offset` at the top level. Small categories answer as before, plus those fields. It is marked paginated in `openapi.py`. Canvas history pages in SQL (`canvas/service.history(limit, offset)`). The board list orders by the saved order in SQL and pages there (`kanban/access.ordered_boards`). The canvas list reads canvases in batches of 500 until the window is filled (`canvas/access.visible_layouts(limit, offset)`). Documented in `docs/api.md` (Paging, Categories).
- **Test:** `tests/security/test_wperf.py::test_w3_5_category_detail_pages_its_pages`, `::test_w3_5_listings_read_only_their_window`

### W4-1 Every kanban write re-serialises and stores the whole board inside the writer transaction
- **Severity:** Medium. **Attacker:** editor (default `kanban.create`) or anyone with write access to a board.
- **Repro:** `scratchpad/W4/wiki/test_w4_kanban_snapshot.py`: after importing a 1,900-ticket, 18.9 MB board, each one-field edit took 0.10 to 0.19 s and stored a 19.1 MB history row under the write lock. After the fix that import is refused (board full). On a board at the cap (790 tickets, 7.9 MB), five edits took about 4 ms each and added no history row within the minute.
- **Fix:** `kanban/fields.py`: `MAX_TICKETS_PER_BOARD` = 5000 (archived tickets included) and `MAX_BOARD_BYTES` = 8 MB of ticket text. `service.check_room` refuses, inside the transaction, ticket creation, longer descriptions, checklist additions and longer checklist text, and imports (`transfer._create`) that pass the caps (new keys `kanban.error.board_full`, `kanban.error.board_full_tickets`, en/it/de). `history.board_bytes` estimates the board size with `octet_length`, without reading the text. `history.record` skips the snapshot of a board over 1 MB within 60 s of its latest entry. `service.revert` first records the current state (`force`, message `kanban.log.before_revert`), so changes made in between are not lost. `_prune` uses `octet_length`. Documented in `features.md` (Kanban). Pre-existing larger boards keep working; only writes that grow them are refused.
- **Test:** `tests/security/test_wperf.py::test_w4_1_boards_are_capped_and_large_ones_snapshot_at_most_once_a_minute`

### W4-7 Chat polling returns an unbounded list of deleted-message ids
- **Severity:** Low. **Attacker:** any group member or DM participant.
- **Repro:** code review: `GET /groups/<id>/messages?after=…&since=2000-01-01` listed every message ever deleted in the conversation, on every poll (120 a minute). Reproduced in the test with old and recent deletions and a lowered cap.
- **Fix:** `chat/store.py` `deleted_since`: looks back at most `DELETED_WINDOW_HOURS` (24) and returns at most `MAX_DELETED_IDS` (500), the newest deletions first. Documented in `features.md` (chat). A page left open for longer than that shows older deletions once it is reloaded.
- **Test:** `tests/security/test_wperf.py::test_w4_7_poll_reports_a_bounded_list_of_recent_deletions`

### R1-1 A made-up bearer header unlocked the large upload limit on session upload views (re-audit, bypass of W3-2)
- **Severity:** Medium (denial of service). **Attacker:** anonymous visitor.
- **Repro:** the W3-2 fix gave the large ceiling to any request carrying
  `Authorization: Bearer ...` on a view marked `@auth.large_body`. On a session
  view such as `/page/<slug>/attachments` CSRF still parses the form, so an
  anonymous POST with `Authorization: Bearer not-a-token` and a body above
  `BW_MAX_CONTENT_LENGTH_BYTES` was read in full (302) instead of refused (413).
- **Fix:** `app._request_limits` counts a bearer header only on `/api/v1/` (where
  the API authenticates it) and only when `api_service.tokens.authenticate`
  accepts the token; the API refuses a bad token (401) before reading the body.
- **Test:** `tests/security/test_audit_regressions.py::test_r1_1_*`.

### R2-1 Pending, denied, suspended and plain accounts still got the large upload limit (re-audit, bypass of W3-2)
- **Severity:** Medium (denial of service). **Attacker:** a self-registered account waiting for approval (or denied, suspended, or a plain user).
- **Repro:** an 8 MiB upload to `/admin/migration/import`: anonymous 413 with
  nothing read, but a pending account (302 to account status) and a plain user
  (403) each had all 8 MiB spooled by the CSRF check first; the ceiling there
  is 500 MiB. Any live session counted.
- **Fix:** `auth.session_may_send_large_body(minimum_role)` requires an active
  session of an account that passes the approval and suspension gates
  (`account_block`) and holds the view's role; `@auth.large_body(minimum_role="admin")`
  marks the administrator uploads (site import, plugin import, bulk Markdown,
  custom pages).
- **Test:** `tests/security/test_audit_regressions.py::test_r2_1_blocked_and_plain_accounts_do_not_get_large_bodies`.

### R2-2 Switching BM mode on paused a temporary administrator's demotion (re-audit, bypass of W1-5)
- **Severity:** Low. **Attacker:** temporarily promoted administrator.
- **Repro:** with `temporary_accounts` already stored as hidden in BM mode's
  choices (unticked while the feature was off, or by an owner while BM mode was
  off), the temporary admin posted `/admin/bm-mode` with `bm_mode=1`: the
  feature counted as switched off, the expiry job skipped and the role stayed
  admin after a forced expiry run. `bm.save` only asked `switch_refusal` when a
  tick box went from shown to hidden. The audit log could be hidden the same way.
- **Fix:** `bm.save` asks `switch_refusal` for every feature the save hides that
  is not hidden now, whether through its tick box or because BM mode comes on;
  refused features stay shown.
- **Test:** `tests/security/test_audit_regressions.py::test_r2_2_switching_bm_mode_on_does_not_pause_a_temporary_promotion`.

### R3-1 A portal password-reset link kept working after the password changed
- **Severity:** Low. **Attacker:** someone who briefly obtained a reset link.
- **Repro:** request a reset, then the owner changes the password and signs out
  other devices; the old link still set a password of the attacker's choosing
  (`set_password` ended sessions and tokens but not the reset token).
- **Fix:** `hosting.accounts.set_password` clears the reset token, sent time and
  expiry in the same update as the new password.
- **Test:** `tests/hosting/test_security_reaudit.py::test_r3_1_a_reset_link_dies_when_the_password_changes`.

### R3-2 Accepting a wiki transfer checked moderation outside the transaction that moved the wiki (re-audit of W5-W1)
- **Severity:** Low (needs a race). **Attacker:** the recipient of a transfer offered by an account being moderated.
- **Repro:** a suspension committed by another worker between `accept_transfer`'s
  checks and `move_to_owner` was ignored: the wiki moved to the recipient and
  the `cancelled` offer was overwritten with `accepted`.
- **Fix:** `instances.move_to_owner(guard=...)` runs the caller's checks inside
  the transaction that changes the owner; `accept_transfer` re-checks the wiki,
  both accounts' standing, and claims the offer only while it is still pending.
- **Test:** `tests/hosting/test_security_reaudit.py::test_r3_2_a_suspension_during_acceptance_keeps_the_wiki`.

### R3-3 The firewall check accepted an empty or outdated table (re-audit of W5-2)
- **Severity:** Low. **Attacker:** none (operator mistake leaves tenants unprotected).
- **Repro:** `tenant_firewall()` returned "loaded" whenever `nft list table inet
  bananawiki_tenants` succeeded, so an emptied table or one saved from an older
  release or for other DNS resolvers raised no warning.
- **Fix:** `ops.firewall.rules()` stamps the table with a comment holding the
  digest of the rendered rules; `System.tenant_firewall()` reports the rules
  as not loaded unless the loaded table carries this release's digest for this
  host's resolvers. Docs updated (hosting.md, operations.md).
- **Test:** `tests/security/test_whost.py::test_r3_3_an_empty_or_outdated_table_does_not_count_as_loaded`.

### R1-2 HEAD requests switched "Download all attachments" off until a restart (re-audit, side effect of W2-3)
- **Severity:** Medium (denial of service). **Attacker:** any reader of a page with attachments.
- **Repro:** `zip_stream` took one of the per-process slots before its generator
  started and gave it back only in the generator's `finally`; a HEAD request
  never starts the generator, so two HEADs left that process answering "busy"
  (303) for good.
- **Fix:** the archive is returned as an iterable whose `close()` (always called
  by the server) releases the slot exactly once, read or not.
- **Test:** `tests/security/test_audit_regressions.py::test_r1_2_head_requests_do_not_use_up_the_attachment_zip_slots`.

### R1-3 Exponential lexers still escaped the highlighting time (re-audit, bypass of W2-1)
- **Severity:** High (denial of service). **Attacker:** any editor (preview, pages, PDF export), and anyone who may write to a kanban board or canvas.
- **Repro:** `rita` on `'` + 24 zeros + `x` doubled its time per character
  (about 40 characters: hours) and `tcsh` on `"` + backslashes grew fourfold
  every four characters, inside a single token: the time check between tokens
  and the 1 KB pieces of W2-1 never ran. A fuzzer over 82 common lexers
  (opener, run of 56 characters, mismatching end) found the same in Bash,
  Docker, Fish, Scala, reStructuredText and Common Lisp, so no allow- or
  block-list of lexers can bound it.
- **Fix:** lexers run in helper processes (`bananawiki/wiki/highlighter.py`, two
  per web process, started with `spawn` on first use): `markdown._highlight`
  hands them the pieces of code and stops the helper (and starts another) when
  the document's time runs out; the code then stays plain (a page cut short
  for want of a free helper is not cached: T1-4). Where helpers cannot start, code is shown without colours. The
  desktop launcher calls `multiprocessing.freeze_support()` so a packaged build
  starts helpers, not a second app. Docs: security.md.
- **Test:** `tests/security/test_audit_regressions.py::test_r1_3_exponential_lexers_are_stopped_at_the_highlighting_time`
  (hung past 120 s before the fix).

### S1-1 Waiting for a busy highlighting helper did not count against the time (third pass, rest of R1-3)
- **Severity:** Medium (denial of service). **Attacker:** readers of a public canvas holding slow code, with anyone who can edit a canvas (or ordinary load).
- **Repro:** three readers kept both helpers busy with a canvas holding 60 KB of
  C#; a canvas answer with 20 code nodes, under one 0.5 s highlighting time,
  took 10.6 s (each node waited up to 2 s for a helper, and that wait and
  helper starts were not counted).
- **Fix:** `highlighter.highlight` counts everything from before it asks for a
  helper (waiting, starting one, lexing), waits at most the time left, and
  reports "out of time" when no helper was free so later code of the same
  answer stays plain at once; a helper that fails to start turns highlighting
  off for a minute, not for the life of the process.
- **Test:** `tests/security/test_audit_regressions.py::test_s1_1_waiting_for_a_busy_highlighter_counts_against_the_time`
  (11 s before the fix, about 2 s with the readers' set-up).

### S1-2 A refused transfer acceptance left an apex wiki renamed, stopped and expiring (third pass, rest of R3-2)
- **Severity:** Low. **Attacker:** the recipient of an administrator's apex wiki (accept and decline at once), or a race with moderation.
- **Repro:** `move_to_owner` renames an apex wiki for a non-admin recipient
  (container stopped, hosting mode, expiry date) before the transaction where the
  R3-2 guard runs; when a decline committed meanwhile made the guard refuse, the
  administrator's wiki stayed renamed, stopped (database still `running`) and
  due for termination in 14 days.
- **Fix:** `accept_transfer` claims the offer (pending -> accepted) with its
  checks under the write lock before anything changes, so a decline or
  cancellation can no longer come in between; the move re-checks moderation in
  its own transaction, and a refused move sets the offer back (cancelled after
  moderation, pending after the recipient's wiki limit).
- **Residual:** moderation of the owner in the moment between the claim and the
  move still leaves an apex wiki renamed for a hosting address; an administrator
  renames it back.
- **Test:** `tests/hosting/test_security_reaudit.py::test_s1_2_a_decline_during_acceptance_cannot_leave_the_wiki_half_moved`.

### T1-4 Pages rendered while the highlighting helpers were busy were cached without colours (fourth pass)
- **Severity:** Low. **Attacker:** anyone keeping a process's two helpers busy (a public canvas with slow code, previews), or ordinary load.
- **Repro:** with both helpers busy, a page with ordinary Python was rendered
  plain and kept in the render cache: it stayed uncoloured after the helpers
  were free again (the R1-3 entry said such pages were not cached; that held
  only inside shared clocks).
- **Fix:** the highlighter reports "no helper was free" apart from "the code
  used up its time"; such a rendering (`render`, `highlight_code`) is not
  cached, while code that used up its own time still is.
- **Test:** `tests/security/test_audit_regressions.py::test_t1_4_a_page_rendered_while_the_helpers_were_busy_is_not_kept_plain`.

### T1-3 The portal's wiki proxy leaked its slots on an invalid upstream header (fourth pass)
- **Severity:** Low (needs `HOSTING_ALLOW_TENANT_PLUGINS=1` and traffic through the portal's fallback proxy). **Attacker:** a hosting tenant running code in their wiki.
- **Repro:** a wiki answering with a folded header (`X-A: a\r\n b`) made gunicorn's
  `start_response` raise `InvalidHeader` outside the code that releases the
  per-wiki slot, the process-wide place and the upstream socket; three such
  responses emptied a portal process's pool and every later proxied request
  waited 10 s and got "busy".
- **Fix:** `wikihosts._proxy` leaves out upstream headers whose name is not an
  HTTP token or whose value holds CR, LF or other control characters, and
  releases everything if `start_response` raises anyway.
- **Test:** `tests/hosting/test_security_reaudit.py::test_t1_3_an_invalid_upstream_header_does_not_leak_proxy_slots`.

### T1-2 A restart in flight brought back the container of a wiki suspended meanwhile (fourth pass, sibling of the start() fix)
- **Severity:** Low. **Attacker:** a wiki owner or collaborator with start/stop rights who expects moderation (also automatic storage suspension).
- **Repro:** the runtime agent's restart is a stop then a start; a suspension
  committed in between (which stops the container) was undone by the restart's
  start: the database said `suspended` and the container ran indefinitely.
  `restart`, `apply_policy`, `rename`, `reset_content`, a create completing with
  a changed policy and a failed rename's restart lacked the re-check `start()` had.
- **Fix:** `instances._keep_only_if_running` re-reads the wiki after every
  container start or restart and stops it again when it is no longer `running`;
  all those paths use it (`restart` then answers `changed_state`).
- **Test:** `tests/hosting/test_security_reaudit.py::test_t1_2_a_suspension_during_a_restart_keeps_the_wiki_down`.

### T1-1 A wiki created during an acceptance left an administrator's apex wiki half-moved (fourth pass, rest of S1-2)
- **Severity:** Low. **Attacker:** the recipient of a transfer of an administrator's apex wiki.
- **Repro:** while `move_to_owner` renamed the apex wiki for the recipient
  (stopping its container, up to 15 s), the recipient created one more wiki of
  their own; the move's capacity check then failed: the administrator's wiki
  stayed renamed out of apex mode, stopped, expiring in 14 days, and the offer
  was pending again (repeatable).
- **Fix:** `accept_transfer` checks the recipient's wiki limit when it claims the
  offer, and an accepted offer whose wiki has not moved yet counts against the
  recipient's limit (`instances.check_account_capacity(receiving=...)`), so a wiki
  created meanwhile is refused instead; a database error during the move also
  puts the offer back instead of leaving it `accepted`.
- **Test:** `tests/hosting/test_security_reaudit.py::test_t1_1_a_wiki_created_during_acceptance_cannot_leave_the_transfer_half_done`.

### U1-1 A finished transfer kept using up one of the recipient's wiki places (fifth pass, from T1-1)
- **Severity:** Low. **Attacker:** none (an account that received a wiki and passed it on).
- **Repro:** bob received a wiki, gave it to carol and owned nothing, but could
  create only 4 of his 5 wikis: every `accepted` transfer whose wiki was no
  longer his counted, for good (and so would one left by a worker that died).
- **Fix:** only a transfer accepted less than `ACCEPTING_MINUTES` (15) ago whose
  wiki has not reached the recipient yet holds a place (no schema change).
- **Test:** `tests/hosting/test_security_reaudit.py::test_u1_1_a_finished_transfer_frees_the_recipients_place`.

### U1-2 A policy change while a wiki was being made left it routed with its container stopped (fifth pass, regression from T1-2)
- **Severity:** Low. **Attacker:** none (an administrator changing a policy setting during a slow import or duplicate).
- **Repro:** the T1-2 re-check after the provisioning restart read the row while
  it was still `stopped`/`pending` by design and stopped the new container; the
  row then became `running` and the portal routed to a wiki that was down.
- **Fix:** provisioning no longer applies that re-check (it checks the owner
  itself before marking the wiki running).
- **Test:** `tests/hosting/test_security_reaudit.py::test_u1_2_a_policy_change_while_a_wiki_is_made_leaves_it_running`.

### U1-4 Maintenance recovery restarted a wiki suspended meanwhile (fifth pass, sibling of T1-2)
- **Severity:** Low. **Attacker:** a tenant who keeps their container unhealthy (with plugins) so every pass recovers it.
- **Repro:** the pass reads the `running` wikis, then the agent stops and starts
  each queued container (up to 15 s per stop); a suspension committed in that
  window was undone and nothing looked again, so the portal said suspended while
  the container ran on.
- **Fix:** after `recover`, maintenance re-checks every wiki it handed over
  (`instances._keep_only_if_running`) and stops those no longer `running`.
- **Test:** `tests/hosting/test_security_reaudit.py::test_u1_4_maintenance_recovery_keeps_a_wiki_suspended_meanwhile_down`.

### U1-3 The canvas cache kept code rendered while no highlighting helper was free (fifth pass, sibling of T1-4)
- **Severity:** Low. **Attacker:** anyone keeping the helpers busy while a canvas node is previewed.
- **Repro:** `POST /canvas/<slug>/render` rendered a node outside any shared
  highlighting clock, so `highlighting_cut_short()` read 0 and the plain
  rendering went into the canvas's own cache, served uncoloured to every viewer.
- **Fix:** `canvas.present._cached` renders under `markdown.bounded_highlighting()`
  when the caller has no clock, so a cut-short rendering is never kept.
- **Test:** `tests/security/test_audit_regressions.py::test_u1_3_a_canvas_node_rendered_while_the_helpers_were_busy_is_not_kept_plain`.

### U1-5 The portal's wiki proxy passed the wiki's reason phrase through (fifth pass, hardening next to T1-3)
- **Severity:** Low (hardening; no impact behind the documented Caddy front). **Attacker:** a hosting tenant with plugins.
- **Repro:** traced: `http.client` keeps a bare CR inside the upstream reason
  phrase and gunicorn writes the status line without checking it.
- **Fix:** `wikihosts._status_line` sends the standard phrase of the status code.
- **Test:** `tests/hosting/test_security_reaudit.py::test_u1_5_the_proxy_sends_its_own_status_phrase`.

### V1-1 A hosting user could make the platform refuse every backup and update by filling their wiki with small files (sixth pass)
- **Severity:** Medium. **Attacker:** any hosting user who owns a wiki (no plugins needed).
- **Repro:** `BANANA_PACKAGE_MAX_FILES` (1,000,000) counts every tenant file, and
  over it every update (the hourly automatic one included), backup, restore and
  rollback of the whole platform is refused; one kanban import makes 4,999 files
  in 0.3 s (10 imports a minute). Without hard quotas one wiki passes the limit
  in about 20 minutes; with them (100,000 inodes per wiki, 5 wikis per account)
  two accounts do.
- **Accepted risk, deploy-time decision:** a first fix left tenants' surplus files
  out of snapshots; the next pass (Y1-1) showed that a failed update's rollback
  then deleted those files from the live wikis (WAL included) and that other
  tenants could choose the victim, so it was reverted: refusing the snapshot
  loses nothing, leaving files out loses data. The operator must enforce hard
  storage quotas (inode caps per wiki) and size `BANANA_PACKAGE_MAX_FILES` above
  wikis x `HOSTING_AGENT_MAX_INODES` plus the platform's own files
  (docs/operations.md says so now). To make the refusal actionable, it and the
  80 % `status` warning name the hosted wikis holding the most files.
- **Test:** `tests/security/test_ops_packages.py::test_v1_1_a_refused_snapshot_names_the_wikis_holding_the_most_files`.

### Y1-1 Trimmed tenant files were deleted from the live disk on rollback (eighth pass, caused by the first V1-1 fix)
- **Severity:** Medium. **Attacker:** hosting users against another tenant.
- **Repro:** with the trimming fix, a rolled-back update left 991 of a wiki's 1,201
  files, and four smaller attacker wikis made a 503-file victim lose all but its
  database (WAL included).
- **Fix:** the trimming was reverted (see V1-1); snapshots are complete again or refused.
- **Test:** covered by the existing rollback tests (tests/ops) with complete snapshots.

### Y1-2 Tenants using the space their plans admit can make the snapshot space check refuse every backup and update (eighth pass)
- **Severity:** Medium (availability). **Attacker:** hosting users uploading within their plans (also plain growth).
- **Repro:** on a 512 MiB volume with four 64 MiB plans admitted, two tenants
  uploading 60 MiB each made the pre-update snapshot check refuse ("would use
  space reserved for the hosted wikis' storage quotas").
- **Accepted risk, deploy-time decision:** the check exists so a snapshot never
  takes space a wiki was promised; a full copy of the wikis' data must fit on
  the staging volume on top of their reservations. The operator sizes the
  volume for it (now documented in docs/operations.md). Copying with reflinks
  (XFS `reflink=1`) would remove the cost; left as a follow-up.

### Y1-3 Long non-ASCII tenant file names can overflow the package manifest limit (eighth pass)
- **Severity:** Medium only with `HOSTING_ALLOW_TENANT_PLUGINS=1`. **Attacker:** a tenant running plugin code.
- **Repro:** 1,100 files with ~3,000-byte undecodable names in one wiki made
  `backup()` fail with "Package manifest is too large" after the downtime.
- **Accepted risk:** the wiki names stored files with random hex, so only tenant
  plugin code can create such names, and tenant plugins are off unless the
  operator enables them with the host controls docs/security.md requires
  ("Hosting operators trust neither tenant wikis ..."). Follow-up: size the
  manifest limit from the names and check it before anything stops.

### Y1-4 Writing and verifying a package ignore the wikis' storage reservations (eighth pass)
- **Severity:** Low. **Attacker:** none specific.
- **Repro:** traced: after the snapshot, `package_from` and `verify_package` write and
  extract the package with only fixed free-space margins while the wikis run.
- **Accepted risk:** a full volume makes verification fail and delete the package
  (reported), it cannot corrupt data; sizing as for Y1-2 covers it.

### Y1-5 Packages over 1 TiB cannot be verified (eighth pass)
- **Severity:** Low. **Attacker:** none (scale).
- **Repro:** traced: `read_package` stops at 1 TiB while `write_package` has no limit.
- **Accepted risk, documented:** docs/operations.md now states the limit (the
  installation's files, uncompressed; Z-2 corrected the wording); larger
  platforms back wikis up separately (tenant archives) or split the volume.

### Y1-6 Partial-backup reporting and listing memory (eighth pass, about the first V1-1 fix)
- **Severity:** Low.
- **Resolution:** moot after the revert (no backup is partial any more because of
  tenant file counts); listing memory grows with the platform's files as
  before, bounded by the sizing V1-1 asks for.

### Z-2 The docs named the wrong size for the 1 TiB verification limit (ninth pass)
- **Severity:** Low (documentation).
- **Repro:** `extract_archive` limits the total uncompressed size of a package's
  files, while docs/operations.md said "a package under 1 TiB".
- **Fix:** the docs say the installation's files, uncompressed.
- **Test:** none (documentation).

### FW1-1 Search with only non-word terms skipped the full-text index and ran up to 46 full-content scans per request (full re-audit pass)
- **Severity:** Medium. **Attacker:** any signed-in user with `search.pages` (a default for users), or an anonymous visitor while public mode is on.
- **Repro:** 2,000 pages of 50,000 characters (100 MB), signed in as a plain user. `GET /search?q=<word>` took 0.08 s, but `q=". -zqaa -zqbb ... -zqww"` (139 characters) took 1.14 s and `q=". . ... ."` (24 dots) 0.29 s. A positive term with no word character made `search_pages` take the LIKE fallback even with FTS5 present, and that path had no `MAX_TERMS` cap: 23 `(p.title || ' ' || p.content) NOT LIKE` clauses, each copying and scanning every visible page, run twice (COUNT and the result page). Expected: indexed or bounded work for any query.
- **Fix:** `bananawiki/wiki/features/pages/search.py`: `parse` drops general, `title:`, `content:` and excluded terms that have no letter or digit (`_usable`), so a punctuation-only query is empty and shows "no results"; `category:` and `slug:` values are kept. With the index present the LIKE path is never used: queries left with only `category:`/`slug:` filters list those pages, and their exclusions go through the index (`p.id NOT IN (SELECT rowid FROM pages_fts WHERE pages_fts MATCH ...)`). Without the index (SQLite lacking FTS5), positive and excluded terms are each capped at `MAX_TERMS` (12), as `slug:` terms are, and title and content are matched separately instead of through a concatenated copy per row. After the fix on the same data the crafted query took 0.01 s, 24 dots 0.01 s, a word plus 23 exclusions 0.03 s; on the no-FTS fallback a word plus 23 exclusions went from 0.91 s to 0.55 s. docs/features.md documents the ignored words and the caps. The sidebar search (`service.search`) is unchanged: it was not part of the finding and does at most one title/content LIKE per request.
- **Test:** `tests/security/test_fw1.py::test_fw1_1_punctuation_query_does_not_scan_page_text`, `::test_fw1_1_like_fallback_caps_the_terms`, `::test_fw1_1_punctuation_is_ignored_but_filters_still_work`

### FW1-2 Editors blocked by a protection or a check-out could rewrite or delete a page's assessment and wipe everyone's attempts (full re-audit pass)
- **Severity:** Low. **Attacker:** editor with `assessment.manage` blocked on the page by someone else's protection or check-out; administrators (bound by protection); holders of `page.delete` on pages pending deletion.
- **Repro:** An editor writes a quiz, a reader submits an attempt, the editor protects the page. Another editor's `POST /page/<slug>/edit` was refused, but `POST /page/<slug>/assessment/manage` (title "Hijacked") answered 302 and changed the quiz, and `POST /page/<slug>/assessment/delete` removed the assessment and all attempts. Expected: refused with the blocked reason, as other page changes are.
- **Fix:** `bananawiki/wiki/features/assessments/service.py` adds `change_blocked` (`page_access.edit_blocked`); `routes.py` checks it in `manage` (GET and POST), `delete` and `reset` and redirects to the page with the reason (`_refuse_if_blocked`), like `_blocked_redirect`. Reading results and attempts stays allowed. The existing `page_governance.blocked.*` and `pages.blocked.pending_deletion` messages are reused, so no new strings. docs/features.md says so.
- **Test:** `tests/security/test_fw1.py::test_fw1_2_protection_blocks_assessment_changes`, `::test_fw1_2_check_out_and_pending_deletion_block_assessment_changes`

### FW1-3 Admins could hide or unhide a protected or pending-deletion page through "Temporary items" (full re-audit pass)
- **Severity:** Low. **Attacker:** administrator (protection binds administrators until the 72-hour unlock delay).
- **Repro:** An editor protects a page. The admin's `POST /page/<slug>/deindex` left `is_deindexed=0`, but `POST /admin/temporary/visibility` (`state=hide`) set `is_deindexed=1`; the same for a page with `pending_deletion=1`. Expected: refused as the direct route and `schedule_page_deletion` refuse.
- **Fix:** `bananawiki/wiki/features/temporary_accounts/service.py`: `schedule_visibility` raises `TemporaryError(page_access.edit_blocked(page, actor))` before changing anything, as `schedule_page_deletion` does. At expiry `_expire_visibility` leaves the page and keeps the schedule while the page is frozen for whoever scheduled it (`_edit_blocked`, run in a request context like `_delete_refused`), so it is retried on the next run, as a refused scheduled deletion is. Module docstring and docs/features.md updated. Existing messages reused.
- **Test:** `tests/security/test_fw1.py::test_fw1_3_temporary_visibility_respects_protection_and_pending_deletion`, `::test_fw1_3_visibility_restore_waits_while_the_page_is_protected`

### FW1-4 The reservation-quota page showed the current title and address of checked-out pages the editor could no longer read (full re-audit pass)
- **Severity:** Low. **Attacker:** editor who checked a page out and then lost read access to it (page moved to a restricted category, or the editor's categories narrowed).
- **Repro:** Editor Mallory, restricted to category "Open", reserves "Draft plan". The page moves to "Secret" and is renamed "Layoffs Q3 list". Her `GET /page/layoffs-q3-list` is refused, but `GET /settings/reservation-quota` listed "Layoffs Q3 list" with its link. The stale check-out also kept blocking every other editor, and Mallory could not release it. Expected: no title leak; a holder who lost access does not lock the page.
- **Fix:** `bananawiki/wiki/features/page_governance/reservations.py`: a check-out counts as active only while its holder may still edit the page (`_holder_may_edit`, `pages.can_edit`, which includes `can_view`). `current()` (and so `status()`, the `page.edit_blocked` interceptor and protection), `statuses()` (the `/reservations` directory), `of_user()` (quota page, also in the admin's view of a user) and `active_count()` (quota) skip the others. The row is not deleted, so it resumes if access comes back before it expires, as with a holder who loses and regains an editing role. `all_active()` (**Admin → Check-outs**) still lists every unreleased row so administrators, who can read every page, can force-release it; the force-release route is unchanged. No new strings. Module docstring and docs/features.md updated.
- **Test:** `tests/security/test_fw1.py::test_fw1_4_quota_page_hides_check_outs_of_unreadable_pages`, `::test_fw1_4_stale_check_out_stops_blocking_and_admin_still_releases_it`

### FW2-1 Anonymous 32 MiB bodies were read by the portal's CSRF check before the global rate limit, and never counted (full re-audit pass)
- **Severity:** Medium (denial of service; the portal sibling of W3-2). **Attacker:** anonymous visitor.
- **Repro:** `FW2/repro_body.py`: one `GET /login` for a CSRF token, then 310 `POST /login` with a 30 MiB multipart file and a wrong `csrf_token`. All 310 answered 302 (CSRF redirect), the application read 9300 MiB, and `GET /login` afterwards still answered 200: `web.install_csrf` ran before `_pipeline`, which held the 300/min global limit, and `MAX_CONTENT_LENGTH` (32 MiB, or chunk size + 1 MiB) applied to every route. Expected: the body refused (413) or the client limited before the body is read.
- **Fix:** `bananawiki/hosting/app.py`: a new `_request_limits` `before_request`, registered ahead of CSRF, applies the global per-client limit (moved out of `_pipeline`, with the same exemptions) and caps every unsafe request at `SMALL_BODY` (2 MiB). Only views marked `@auth.large_body` keep `MAX_CONTENT_LENGTH`, and only when `auth.session_may_send_large_body()` holds: the cookie carries an administrator's own active session (row live, account and session version match, not deleted or suspended, not impersonating, administrator two-step satisfied). Marked: `/admin/instances/import`, `/admin/instances/import/chunk/<id>`, `/admin/restore-platform`, `/admin/restore-platform/chunk/<id>` (`routes/admin.py`); these were the only file uploads in the portal besides the Google Drive credentials (read up to 1 MiB, under the cap). The API takes no bodies of that size. Administrators keep every archive and backup upload limit; nothing else accepted more than 2 MiB. docs/configuration.md and UPGRADING.md say so.
- **Test:** `tests/security/test_fw2.py::test_fw2_1_anonymous_bodies_are_capped_before_csrf_reads_them`, `::test_fw2_1_requests_refused_by_csrf_count_against_the_global_limit`, `::test_fw2_1_administrator_still_uploads_large_archive_chunks`, `::test_fw2_1_large_upload_bodies_need_the_administrators_own_session`, `::test_fw2_1_other_signed_in_forms_keep_the_small_limit`

### FW2-2 A previous owner kept administrator access to a transferred wiki through platform sign-in, even after the documented password reset (full re-audit pass)
- **Severity:** Medium. **Attacker:** the previous owner of a wiki, after a transfer accepted with "keeps none" or "view", or after an administrator's **Move now** (also used when moving an abusive owner's wikis away).
- **Repro:** `FW2/tests/test_fw2_transfer_platform_link.py`: the owner connects the wiki's `owner`-role account `admin_team` through `/platform-oauth/merge`, transfers the wiki (recipient accepts with `previous_owner=none`), the recipient resets the administrator password (`tenant_task.reset_admin`) and sets a new one. No sign-out event was queued, and the previous owner's `/platform-oauth/login` still signed in as `admin_team` (`GET /admin/users` 200): `move_to_owner` left their grant, access tokens and `hosting_oauth_account_links` row, the wiki trusts its local `platform_oauth_links` row first, and `reset_admin` did not touch links (dropping only the wiki's row would not have helped either: the wiki adopts the portal's link again through `realign_portal_link`). Expected: no access after the change of owner, at the latest after the reset.
- **Fix:** `bananawiki/hosting/instances.py` `move_to_owner` (accepted transfers, Move now, moving a deleted account's wikis) calls the new `oauth.end_former_owner_access` for the previous owner inside the transaction that changes the owner: it queues a sign-out with the new reason `owner_changed` (`signouts.REASONS`), drops their unused codes and access tokens for the wiki's client, their consent and their portal link. The wiki's `apply_signouts` (`bananawiki/wiki/features/auth/platform_oauth.py`) now reads the event's `reason` and, for `owner_changed`, also drops its local link of that portal account when the link is no newer than the event (a replayed event after a portal restore never drops a newer link). `bananawiki/ops/tenant_task.py` `reset_admin` deletes the reset accounts' `platform_oauth_links` and returns their ids (`user_ids`, bounded); `AgentRuntime.reset_admins` (new runtime method; `reset_admin_password` kept for compatibility) passes them on and `instances.reset_admin_password` drops the portal's links to them (`oauth.forget_wiki_users`). The collaborator access the recipient chooses for the previous owner is unchanged; other collaborators are untouched; the previous owner's next platform sign-in asks for consent and starts at the connect page (new account, or an existing one with its password). No migration (the reason column is free text). docs/hosting.md (transfers, reset, single sign-out) and UPGRADING.md updated.
- **Test:** `tests/security/test_fw2.py::test_fw2_2_transfer_ends_the_previous_owner_link` (keep none and view), `::test_fw2_2_administrators_move_now_ends_the_previous_owner_link`, `::test_fw2_2_an_older_owner_changed_event_leaves_a_newer_link`, `::test_fw2_2_admin_password_reset_drops_its_platform_links`, `::test_fw2_2_portal_reset_forgets_links_to_the_reset_accounts`

### FW2-3 Idle, unauthenticated connections took every connection slot of the GPU speech server (full re-audit pass)
- **Severity:** Low (optional contrib server; from tenants it needs `HOSTING_ALLOW_TENANT_PLUGINS=1`). **Attacker:** a hosting tenant running plugin code, or any host on the server's network.
- **Repro:** `FW2/repro_tts_slots.py`: after 64 connections that sent `POST /synthesize HTTP/1.1\r\n` and then one byte now and then, a legitimate request got `RemoteDisconnected`, still after the trickle; the 30 s socket timeout bounds each read, not the request, and the 64-connection cap was global and counted before the token check. Expected: idle clients cannot hold every slot.
- **Fix:** `contrib/tts-gpu-server/server.py`: the handler reads through `_RequestReader`, which waits at most `TTS_SOCKET_TIMEOUT` per receive and raises `TimeoutError` once the whole request (line, headers and body) has taken `TTS_REQUEST_TIMEOUT` (default 10 s), counted from when the server starts waiting for it; the stdlib then closes the connection as for any read timeout. `SpeechServer` also caps open connections per source address (`TTS_MAX_CONNECTIONS_PER_CLIENT`, default 8) next to the global `TTS_MAX_CONNECTIONS`. Token check and protocol unchanged. Measured: a byte every 0.1 s kept a connection open for 3 s before and closed it at the 0.5 s deadline after; with one address holding the global cap, another address got 200 after (refused before). A hosting platform whose wikis reach the server through one address shares those 8 connections (refused requests fall back to local Piper); README and UPGRADING.md say to raise it then.
- **Test:** `tests/security/test_fw2.py::test_fw2_3_defaults`, `::test_fw2_3_a_trickling_client_is_dropped_at_the_request_deadline`, `::test_fw2_3_one_address_cannot_take_every_connection`, `::test_fw2_3_requests_sent_in_pieces_within_the_deadline_still_work`

### GW1-1 One- and two-letter search words matched almost every word, and excerpts were made for every match (full re-audit pass 2, rest of FW1-1)
- **Severity:** Medium. **Attacker:** any user with `search.pages`, or an anonymous visitor in public mode (`/api/sidebar/search` is public-read).
- **Repro:** on a 240 MB wiki (2,000 pages of 50,000 characters), `/search?q=a b c d e f g h i j k l`
  took 5.4 s for page 1 and 64 s for page 50, and `/api/sidebar/search?scope=content`
  with the same words 4.9 s, while an ordinary word took 0.09 s. Each word became an FTS5
  prefix query (`"a"*`), which stands for nearly every word in the index, and the result
  query sorted rows with `snippet()` in the select list, so SQLite made an excerpt for
  every matching page, not just the 20 shown. A few such requests kept every worker
  thread busy.
- **Fix:** `pages.service.fts_phrase` makes a word a prefix only when it has at least
  `MIN_PREFIX` (3) characters; shorter words match only themselves. `/search`
  (`search.search_pages`) and the sidebar and API search (`service.search`) select the
  page ids first and make excerpts for those rows only (`service.fts_snippets`). After
  the fix, on the same wiki, every shape measured (12 single letters, 12 common
  three-letter prefixes, 12 words, page 1 and page 50, sidebar) took at most 0.07 s.
  docs/features.md says which words find longer ones.
- **Test:** `tests/security/test_gw1.py::test_gw1_1_short_words_are_not_prefix_searches`, `::test_gw1_1_excerpts_are_made_for_the_rows_shown_only`.

### GW1-2 Sign-in in the window after a change of owner (full re-audit pass 2)
- Same issue as GW2-1; fixed and tested there.

### GW2-1 A platform sign-in between a change of owner and the wiki's sign-out job keeps an administrator session (full re-audit pass 2)
- **Severity:** Low. **Attacker:** the previous owner of a wiki (transfer accepted with "keeps none" or "view", or an administrator's Move now).
- **Repro:** Platform fixture (real portal and wiki). The owner connected `admin_team` (or a personal administrator account `oldowner`) through `/platform-oauth/merge`, the transfer was accepted, and the previous owner signed in through the portal again before the wiki's `auth.platform_signouts` job ran. Observed: signed in at once as the connected administrator; the job then dropped the link but ended only sessions older than the `owner_changed` event, so the new session kept `/admin/users` (200) for its full lifetime, and for `oldowner` also through the documented `reset_admin` of the named administrator. Expected: no session through the dropped link survives, and no sign-in through it after the change of owner.
- **Fix:** `bananawiki/wiki/features/auth/platform_oauth.py`: new `confirm_link`, which the platform sign-in callback (`routes_oauth.platform_oauth_callback`) calls before trusting a local link. It asks the portal's `link-status` lookup (already used for realignment) whether the portal still links that portal account to that wiki account; a link the portal no longer has is dropped and the platform sessions of that wiki account end, so the previous owner meets the connect page (consent, then a wiki password). One extra server-to-server call per platform sign-in; a portal without the lookup is not asked. `apply_signouts`: an `owner_changed` event that drops a link (`drop_link` now returns the linked wiki account) ends every platform session of that account up to the moment the event is applied, not only up to the event's time, so the design holds even when the callback could not ask and the job lags. `routes_oauth._current_link`: a new owner connecting the administrator account before the job ran is no longer refused by the stale link (it is confirmed with the portal and dropped). Members whose links the portal still has sign in as before. docs/hosting.md describes both. Only the portal's explicit answer (`linked` false or another link) drops a link: a 429, a 5xx or a malformed answer fails that sign-in and keeps the link (found in review). Two older tests (`tests/features/test_auth_oauth.py::test_linked_account_signs_in_by_stable_id`, `tests/features/test_auth_oauth_connect.py::test_an_account_connected_to_another_hosting_account_is_refused`) inserted local links the fake portal did not know; they now register them on the fake portal too, as `link()` does.
- **Test:** `tests/security/test_gw2.py::test_gw2_1_sign_in_before_the_signout_job_meets_the_connect_page`, `::test_gw2_1_owner_changed_ends_sessions_started_after_it` (both for `admin_team` and a personal `oldowner` administrator), `::test_gw2_1_new_owner_and_members_keep_signing_in`, `::test_gw2_1_a_failing_portal_keeps_the_link`

### GW2-2 Safe-method bodies and unrouted requests escape the portal's body cap and global limit (full re-audit pass 2)
- **Severity:** Low. **Attacker:** anonymous visitor.
- **Repro:** With sign-up open, an anonymous `GET /signup` carrying a 30 MiB multipart body answered 200 after Werkzeug parsed and spooled the body (the page reads `request.form`); only bodies above 32 MiB got 413. With `GLOBAL_RATE_LIMIT=5`, 50 `POST /no-such-page` with 2 MiB bodies and a wrong CSRF token were all parsed and none counted (`GET /login` still 200). Expected: 413 for the GET body, 429 after 5 requests.
- **Fix:** `bananawiki/hosting/app.py` `_request_limits`: the 2 MiB `SMALL_BODY` cap applies to every method, GET and HEAD included; only an administrator's own session on the `@auth.large_body` upload views (unsafe methods) keeps the larger ceiling. Requests that match no route (404, 405) count against the global per-client limit; only `static`, `health`, `healthz` and `favicon_ico` (`UNLIMITED_ENDPOINTS`) stay exempt. docs/configuration.md says the cap holds whatever the method.
- **Test:** `tests/security/test_gw2.py::test_gw2_2_get_bodies_are_capped_too`, `::test_gw2_2_unrouted_requests_count_against_the_global_limit`

### GW2-3 Scheduling an account's deletion or suspending it from the CLI leaves its wikis routed until the next maintenance pass (full re-audit pass 2)
- **Severity:** Low. **Attacker:** the owner of a moderated account, and visitors of their wikis meanwhile.
- **Repro:** Owner with a running wiki, routes published. An administrator's `POST /admin/accounts/<id>/schedule-deletion`, or `cmd_account_suspend` without `--also-instances`. Observed: `domains.may_serve` False but the wiki still in the runtime's published routes (Caddy keeps proxying it for up to 5 minutes). Cancelling the deletion and the CLI unsuspend did not re-publish either. Expected: re-published at once, as the web suspension does.
- **Fix:** `bananawiki/hosting/accounts.py`: `suspend`, `unsuspend` (except the automatic lift of a timed suspension, which may run from `is_suspended` anywhere and which the maintenance pass re-publishes right after lifting), `schedule_deletion` and `cancel_deletion` re-publish the routing table themselves after their transaction (`_publish_routes` -> `instances.sync_routes`), so no caller (web, CLI, future code) can forget.
- **Test:** `tests/security/test_gw2.py::test_gw2_3_scheduled_deletion_republishes_routes`, `::test_gw2_3_cli_suspension_republishes_routes`

### GW2-4 Keep-alive tokenless requests hold GPU speech server connections; eight shut out every hosted wiki (full re-audit pass 2)
- **Severity:** Low. **Attacker:** a hosting tenant running plugin code (`HOSTING_ALLOW_TENANT_PLUGINS=1`), or any host on the server's network.
- **Repro:** Server with the fake engine and defaults (64, 8 per address), deadline 1 s. Eight connections from 127.0.0.1 each sent `GET /health HTTP/1.1` without a token every 0.6 s. Observed: after 5 s the wiki's authenticated `POST /synthesize` from 127.0.0.1 got `RemoteDisconnected`; with 7 more addresses x 8, a request from 127.0.0.50 too. Expected: tokenless clients cannot keep connections or shut out the address.
- **Fix:** `contrib/tts-gpu-server/server.py`: every answer to a request without a valid token (`/health`, 404, 405 included) closes the connection and sends `Connection: close`; only requests with a valid token keep a connection alive. `SpeechServer` tracks each connection's state (pending: no valid token yet; busy: handling an authenticated request; idle: authenticated keep-alive). One address may hold at most `TTS_MAX_PENDING_PER_CLIENT` (new, default 4) pending connections within its `TTS_MAX_CONNECTIONS_PER_CLIENT` (8); over either cap a new connection takes the place of the address's oldest pending connection (else its oldest idle one) instead of being refused, and is refused only when every connection of the address is busy. Tokenless connections therefore cannot shut out the next wiki's request from the shared address, and never close a connection that is busy with an authenticated request. When an address is full and has no pending connection left to close, a tokenless newcomer does close its oldest idle authenticated keep-alive connection (as the README says: the oldest connection not busy); that client only reconnects, as it would after `TTS_REQUEST_TIMEOUT`. Authenticated keep-alive and the protocol are unchanged. README documents the new variable and the closing behaviour. `tests/security/test_fw2.py::test_fw2_3_one_address_cannot_take_every_connection` expected the newcomer to be refused while tokenless connections held the address (the lockout); it now expects the oldest of them to be closed. Remaining: a tenant with its own valid token can still use authenticated requests within the server's other limits.
- **Test:** `tests/security/test_gw2.py::test_gw2_4_answers_without_a_token_close_the_connection`, `::test_gw2_4_tokenless_connections_cannot_shut_out_the_shared_address`

### HW1-1 A kanban revert recreated deleted tickets past the W4-1 caps, a whole board per revert (full re-audit pass 3)
- **Severity:** Medium. **Attacker:** anyone with write access to one board (an invited plain user, or global write access).
- **Repro:** `scratchpad/HW1/tests/test_hw1_kanban_revert.py`: a plain user with a write share filled a board to the cap (5,000 title-only tickets; or 830 tickets of 10 KB, 8 MB), deleted all but the newest ticket, then reverted to the full version again and again. Observed: 5,000 -> 9,999 -> ... -> 59,989 tickets in 12 reverts (and 8.3 -> 33.2 MB of text in 4), because recreated tickets got new ids and the same version still named the old ones. The board state then answered 13.9 MB per request. Expected: the revert refused, as an import is.
- **Fix:**
  - `kanban/history.py` `_restore_ticket` brings a deleted ticket back under its own id when that id is free (ticket ids are `AUTOINCREMENT`, so a free id is never another ticket's). Reverting to the same version again finds it instead of adding a copy.
  - `kanban/service.py` `revert` counts tickets and measures text (`history.board_bytes`) before and after `history.revert`, in the same transaction. It refuses with `kanban.error.board_full_tickets` or `kanban.error.board_full` (existing en/it/de messages) when the result passes a cap and grows the board, and the transaction rolls back. `check_room` gained an `allowed` size, so a board already over the cap from an older version can still revert if the revert does not grow it. The new `ticket_count` helper is shared with `create_ticket`.
  - `kanban/views.py` `revert` flashes the refusal on the board.
  - Documented in `features.md` (Kanban, History).
- **Test:** `tests/security/test_hw1.py::test_hw1_1_revert_obeys_the_ticket_cap_and_does_not_multiply_tickets`, `::test_hw1_1_revert_obeys_the_text_cap`

### HW1-2 A profile disabled by an administrator was published again by deleting it first (full re-audit pass 3)
- **Severity:** Low. **Attacker:** user (holder of `profile.edit_own`, a default) whose profile an administrator disabled.
- **Repro:** `scratchpad/HW1/tests/test_hw1_profile_disable_bypass.py`: an administrator disabled mallory's profile, so her publish was refused. Mallory posted `/settings/profile/delete`, then `/settings/profile/publish published=1`. Observed: the row was recreated as published and not disabled, and `GET /users/mallory` answered 200 with the custom field ("abusive field") that had survived the delete. Expected: still 404 until an administrator enables the page.
- **Fix:**
  - `users/service.py` `delete_profile(keep_block=True)` empties a disabled profile (name, bio, birth date, avatar, unpublished) instead of deleting its row, so `page_disabled_by_admin` stays. A profile that is not disabled is still deleted in full. Both statements are conditioned on the flag in SQL, so an administrator disabling the profile at the same moment is not lost.
  - The owner's route now calls `user_profiles/service.py` `delete_own`. It also deletes the owner's profile field answers (`user_profile_fields__values`), which the delete left behind; the owner asked for their profile details to go, so keeping them was a data-deletion gap. Administrator tags stay, since they are moderation data rather than the owner's.
  - The confirmation text says so in en, it and de. The userbot endpoint (`POST /api/v1/userbot/profile`) has no delete of its own and already refuses publishing while the kept row carries the block; the test checks it after the owner's delete.
  - The administrator's `moderate("delete")` is unchanged (it still removes the row and the block).
  - Documented in `features.md` (People and profiles).
- **Test:** `tests/security/test_hw1.py::test_hw1_2_deleting_a_disabled_profile_keeps_the_block`, `::test_hw1_2_deleting_an_own_profile_removes_it_with_its_field_answers`

### HW1-3 A plain administrator lifted any page protection at once, unaudited, by switching protection off and on (full re-audit pass 3)
- **Severity:** Low. **Attacker:** administrator (protection binds administrators until the 72-hour unlock delay).
- **Repro:** `scratchpad/HW1/tests/test_hw1_protection_switch.py`: editor `controller` protected "Guarded", so the admin's edit was refused. The admin posted `/admin/governance` without `page_protection_enabled`, edited the page, and switched it back on. Observed: the content changed, `protected_by` was unchanged, and there was no audit entry. The same worked through `PUT /api/v1/settings` and by switching the `page_governance` feature off. Expected: refused (sibling of W2-10).
- **Fix:**
  - `page_governance/protection.py` `switch_off_error(actor)` refuses while protection is active and a page is protected by someone other than the actor. It counts only protections in force: not the home page, and only while the controller still holds an editing role.
  - Per features.md, protection binds everyone except its controller, "administrators included", so owners and superusers are refused too. The server's own commands (no request context) may still switch it off. The emergency levers are unchanged and audited: the unlock request, and demoting the controller.
  - It is applied in three places: the admin form (`admin.governance`, flash `page_governance.protection.error.switch_off_protected`), the settings API (a `settings_rules` policy, 403 `refused.pages_protected`), and a `feature.switch` interceptor for the `page_governance` feature (plugin manager, onboarding wizard and BM mode all go through `registry.switch_refusal`).
  - Every change of `page_protection_enabled` from the form or the API is recorded as `page_governance.protection_switched` with `{"enabled": ...}`. Feature switches were already recorded.
  - Messages are in en, it and de. Documented in `features.md` (Page governance) and `api.md` (Settings).
- **Test:** `tests/security/test_hw1.py::test_hw1_3_administrators_cannot_switch_protection_off_while_others_protect_pages`, `::test_hw1_3_the_controller_may_switch_protection_off_and_every_switch_is_audited`

### HW2-1 Anyone could use up a wiki's platform sign-ins with bogus codes, since the portal limited each wiki to 20 sign-ins a minute (full re-audit pass 3)
- **Severity:** Medium (availability of platform sign-in; accounts made through platform sign-in have only a random wiki password, so for them it is a full lockout). **Attacker:** anonymous visitor of a hosted wiki. The limit also tripped with no attacker.
- **Repro:** In the platform fixture (real portal and wiki), member alex signed in once. Then one anonymous address made 20 wiki logins, each followed by `/platform-oauth/callback` with `code=bogus` (all within the wiki's 30-per-10-minutes callback limit). The portal answered each with 400 and counted it in `@rate_limit(20)` on `/oauth/token`, keyed by the network, which is the wiki container's own address. Observed: alex's next platform sign-in ended at `/login` ("token endpoint answered 429"). In a second test with no attacker, 25 members from 25 addresses signed in within a minute. Members 21 to 25 failed with 429. `/oauth/link` (30 a minute) and the portal's global 300-a-minute limit were also keyed by the wiki's address. Expected: valid codes are always redeemed, and a wiki's visitors cannot use up its users' sign-ins.
- **Fix:** `bananawiki/hosting/limits.py` has a new `server_to_server` marker. The portal's global per-network limit skips views with this marker (`app._request_limits`). `/oauth/token`, `/oauth/userinfo`, `/oauth/me`, `/oauth/verify`, `/oauth/link`, `/oauth/unlink`, `/oauth/link-status` and `/oauth/signouts` (`routes/oauth.py`) lost their per-network `rate_limit` and carry the marker. They authenticate the client first and count only failures:
  - Wrong client credentials (including 1.4's `instance_id` + secret) count per network, 30 a minute. A network over the limit is refused with 429 before its next credentials are checked, so guesses stay bounded.
  - Unknown bearer tokens count per network, 60 a minute.
  - Refused codes count per wiki, 60 a minute.

  Over either of the last two limits, a failed request answers 429, but a valid token or code is still accepted. With the right credentials, `/oauth/token` is never refused for other requests, because codes are random, single-use and each needs a user's sign-in. The other authenticated calls share a per-wiki ceiling in each worker, `HOSTING_OAUTH_CLIENT_RATE` (new, default 600 a minute, minimum 60, in-memory `client_call`). The protocol is unchanged, so earlier wikis keep working. Docs: docs/hosting.md (Platform sign-in → Limits), docs/configuration.md, `.env.example` and UPGRADING.md. Wiki side: the callback stays bounded by the existing 30 per visitor address per 10 minutes. A tighter per-network count of bad codes was not added, because it would lock out the pupils of a school behind one NAT address when one of them replays a code. With valid codes never refused, bogus callbacks now only use the attacker's own budget.
- **Test:** `tests/security/test_hw2.py::test_hw2_1_bogus_callbacks_do_not_use_up_the_wikis_sign_ins`, `::test_hw2_1_a_class_signs_in_through_the_portal_at_once`, `::test_hw2_1_wrong_client_secrets_are_bounded_per_network`, `::test_hw2_1_each_wiki_has_a_ceiling_of_authenticated_calls`

### HW2-2 The admin wiki download was an uncounted GET that built a full archive each time, and a HEAD left the archive on disk (full re-audit pass 3)
- **Severity:** Low. **Attacker:** an administrator's session, or a tool acting with it (link checker, monitor). A cross-site page could start one export per top-level navigation, because the request was a GET with no CSRF token.
- **Repro:** As administrator, 15 `GET /admin/instances/<id>/download` all answered 200 with 15 `export_archive` calls. `@rate_limit(10)` never counts GET. Then 5 HEADs each answered 200 and left 5 `bwh-archive-*` folders in `HOSTING_EXPORT_TEMP_DIR` (0 before). `HeadAsGet` closes the response without iterating it, and a generator that never started does not run its `finally`. The daily sweep removed the folders only after 24 h. Expected: the download is counted and needs a form, and no work folder outlives its response.
- **Fix:** `bananawiki/hosting/routes/admin.py`:
  - The download is a POST, protected by CSRF and counted by `@rate_limit(10)`. Both buttons on the admin wiki page (`templates/hosting/admin/instance.html`) use `ui.post`, like the owner's download.
  - A GET or HEAD of the old URL (`download_instance_page`) redirects to the wiki's admin page and builds nothing.

  `routes/dashboard.py` `stream_file` also removes the work folder from `response.call_on_close`, so the folder goes when the server closes the response, read or not. This fixes every caller: the admin and owner downloads and `/admin/export-platform`. No other portal view streams a temporary file. Two tests in `tests/hosting/test_runtime_archives.py` downloaded with GET and now POST. UPGRADING.md describes the change.
- **Test:** `tests/security/test_hw2.py::test_hw2_2_admin_download_get_and_head_only_lead_to_the_button`, `::test_hw2_2_a_download_closed_unread_removes_its_work_folder`

### HW2-3 Concurrent route publications could publish a stale routing table last (full re-audit pass 3)
- **Severity:** Low. **Attacker:** none (a race between two portal workers; found during the GW2-3 re-check).
- **Repro:** Two `instances.sync_routes` calls in threads. The first read the table while the wiki was running and was slow to publish. The second suspended the wiki and published its own table. Observed: the first table reached the runtime last and routed the suspended wiki again, until the next maintenance pass. Expected: the last table published reflects every change made before it.
- **Fix:** `bananawiki/hosting/instances.py` `sync_routes` reads and publishes the table under a host-wide `FileLock` (`<HOSTING_PLATFORM_STATE_DIR>/.routes.lock`, 120 s timeout), which serialises portal workers and the CLI. A sync that starts after another worker's change therefore publishes after any older read. A timeout is logged and skipped, like the other best-effort failures, and maintenance publishes again.
- **Test:** `tests/security/test_hw2.py::test_hw2_3_a_stale_routing_table_is_never_published_last`

### GW2-4 wording (full re-audit pass 3)
- **Severity:** none (documentation). **Attacker:** none.
- **Repro:** The GW2-4 entry said tokenless connections cannot "displace authenticated keep-alive connections". When an address is full and has no pending connection left, `contrib/tts-gpu-server/server.py` does close its oldest idle authenticated keep-alive connection, as the README states ("oldest connection not busy").
- **Fix:** AUDIT.md GW2-4 now says that only connections busy with an authenticated request are never closed, and that an idle keep-alive one may be closed, after which its client reconnects.
- **Test:** none (wording only).

### IW1-1 Underscores made a one-letter search word long enough to be a prefix again (full re-audit pass 4, rest of GW1-1)
- **Severity:** Medium. **Attacker:** any user with `search.pages`, or an anonymous visitor through the sidebar search in public mode.
- **Repro:** the GW1-1 length check counted Python's `\w`, which includes `_`, but the
  index's tokenizer drops underscores: `a__` passed the 3-character check and became the
  one-letter prefix query `"a"*`. On the 240 MB test wiki twelve such words took 6.5 s
  on `/search` and 6.3 s on `/api/sidebar/search` (an ordinary word 0.07 s).
- **Fix:** `pages.service._FTS_TOKEN` and `search._WORD` take words as the index does
  (`[^\W_]+`), and `fts_phrase` splits its words again that way before the length check.
  After: 0.01 s for every padded shape. docs/features.md says underscores do not count.
- **Test:** `tests/security/test_iw1.py::test_iw1_1_padded_short_words_are_not_prefix_searches`.

### IW1-2 Kanban columns had no cap outside imports (full re-audit pass 4)
- **Severity:** Low. **Attacker:** anyone with write access to one board.
- **Repro:** `POST /api/kanban/<board>/columns` accepted 300 columns (imports stop at 200);
  at 50,000 columns each write took 0.22 s holding the write lock, the board page was
  19.8 MB and each change stored an 8 MB history snapshot.
- **Fix:** `fields.MAX_COLUMNS_PER_BOARD` (200, also the import's cap): creating a column
  past it is refused (409 `board_full_columns`, en/it/de), and so is a revert that would take
  the board past it and grow it. docs/features.md lists the cap.
- **Test:** `tests/security/test_iw1.py::test_iw1_2_columns_are_capped_on_create_and_revert`.

### IW2-1 Tenant-chosen file and folder names reach the root operator's terminal with raw escape sequences (full re-audit pass 4)
- **Severity:** Low. **Attacker:** hosting tenant running plugin code (`HOSTING_ALLOW_TENANT_PLUGINS=1`), which names and nests folders in its own directory.
- **Repro:** 70 folders nested under `data/instances/acme/<ESC]52;c;…BEL ESC]0;all good BEL>`, then `snapshot.sources()` as `bananawiki status`/`backup`/`update` call it. Observed: the "Leaving out folders nested more than 64 levels deep" warning wrote the OSC 52 clipboard write and title change raw to stderr (the CLI has no log handler, so logging's last resort printed it). `one_line` only replaced CR and LF. Expected: control characters escaped.
- **Fix:** `core.logformat.printable()` writes C0 (except line feed and tab), DEL, C1, U+2028/2029 and the bidi overrides as `\xNN`/`\uNNNN`; `one_line()` uses it, so every caller benefits. The text log formatter (`TextFormatter`, used by `formatter("text")` for the wiki and portal) escapes every message. The controller replaces `logging.lastResort` with `StderrHandler`, which also escapes. The runtime agent and the maintenance service log through the formatter. The controller's error and warning prints (`cli.py`, `manager.py`), the hosting admin CLI's messages and the agent's dropped-hostname warning go through `printable()`/`one_line()`. Docs: operations.md (Logs), UPGRADING.md.
- **Test:** `tests/security/test_iw2.py::test_iw2_1_a_tenant_folder_name_is_logged_escaped`, `::test_iw2_1_every_control_character_is_escaped`, `::test_iw2_1_the_controller_and_the_portal_logs_escape_whatever_a_call_left_raw`

### IW2-2 The HW2-1 exemption lets anonymous clients send unlimited requests to the portal's OAuth endpoints (full re-audit pass 4)
- **Severity:** Low (denial of service by volume; regression from HW2-1). **Attacker:** anonymous visitor, with platform sign-in switched on.
- **Repro:** global limit patched to 5. 200 anonymous `POST /oauth/token` with `grant_type=x` and a 2 MiB multipart part were all answered 400 (399 MiB parsed, 0 rate-limit rows written). 300 `GET /oauth/userinfo` with `Bearer nope` gave 60 x 401 and 240 x 429, but ran 300 `BEGIN IMMEDIATE` transactions. Expected: bounded per network before the body is read, and a refusal costs at most a read.
- **Fix:** `hosting/limits.py`: `server_to_server` is now also a gate. A network over its `BAD_CLIENT` budget (30 a minute) is refused 429 before the view reads anything of the request. Bearer views (`server_to_server(bearer=True)`: userinfo, me, verify) are exempt, so a valid token is never refused for others' failures. Malformed token requests (another `grant_type`, no credentials) now count in `BAD_CLIENT`. Unknown bearer tokens and refused codes use the new `limits.failure()`, which reads before it writes: over the limit, a refusal writes nothing and takes no write lock. A token request with valid credentials but no code (a wiki's visitors can cause it; the wiki's link callback forwards an empty code) counts per wiki like a refused code, so the wiki's address is never locked out. Server-to-server bodies are capped at 64 KiB (`SERVER_TO_SERVER_BODY`). The HW2-1 guarantees and tests are unchanged: valid codes and tokens are always accepted, class-size bursts pass. Docs: hosting.md, UPGRADING.md.
- **Test:** `tests/security/test_iw2.py::test_iw2_2_malformed_token_requests_are_counted_and_then_refused_unread`, `::test_iw2_2_server_to_server_bodies_are_small`, `::test_iw2_2_bogus_bearers_over_the_limit_cost_a_read_only`, `::test_iw2_2_codes_a_wiki_is_made_to_send_without_a_code_count_per_wiki`

### IW2-3 With the gunicorn backend, the desktop launcher runs Python code from the data folder (full re-audit pass 4)
- **Severity:** Low (gunicorn backend only: source installs on Linux/macOS without waitress, or `--backend gunicorn`). **Attacker:** whoever hands a teacher a data folder.
- **Repro:** a folder holding the marker file and a `gunicorn.conf.py`: the wiki started normally, and the config file had run. A folder holding `gunicorn/__main__.py`: its code ran instead of gunicorn (`python -m` puts the working directory first on `sys.path`, and the child ran with `cwd` set to the data folder and no `-c`). Expected: nothing in the data folder is executed.
- **Fix:** `desktop/server.py` `_GunicornRunner` now starts the child in the package's parent directory, which is also passed as `--chdir` and `PWD` (gunicorn itself inserts its chdir into `sys.path`). Python runs with `-P`, and gunicorn reads its settings via `-c` from the packaged, empty `bananawiki/desktop/gunicorn_child.py`, so it never reads a `./gunicorn.conf.py`. Paths still reach the worker through `BW_*`. Checked: the waitress backend runs in the launcher process, which never changes into the data folder. The other children (`ipconfig`/`ip`/`ifconfig`, the file manager, the Piper child already started with `-P`) do not run from it either. Docs: desktop.md, UPGRADING.md.
- **Test:** `tests/security/test_iw2.py::test_iw2_3_the_gunicorn_child_starts_outside_the_data_folder`, `::test_iw2_3_a_handed_over_folder_runs_no_code_of_its_own`

### IW2-4 Slow readers of large responses take every server thread of the wiki and the portal (full re-audit pass 4)
- **Severity:** High for the wiki (site-wide denial of service; the wiki sibling of BananaChat's IC1-1), Medium for the portal. **Attacker:** any signed-in wiki user (any visitor in public mode); on the portal, a wiki owner where owner downloads are switched on.
- **Repro:** live gunicorn with the shipped `bananawiki/ops/gunicorn_conf.py` (2 workers x 4 gthread threads, `BW_WRITE_TIMEOUT=300`) on localhost. 8 clients with a 2 KiB receive buffer read a 6 MB `/static/uploads/` file at 2 KiB/s. Observed: every `/health` probe got no answer within 5 s for the whole 40 s run. The write timeout ends only a client that stops reading. 300 KB bodies held threads as well; 70 KB fitted in the socket buffers. Caddy streams responses at the client's pace, so the thread is not freed early. The portal's archive `stream_file` downloads behave the same way. Expected: short requests keep their threads.
- **Fix:** new `core/downloads.py`. A response that is a stream or larger than 64 KiB takes one of the process's places and one of its client's (the signed-in account, else the network). Both are given back when the server closes the response: for sendfile bodies through the file wrapper's `close()`, which the server calls instead of the response's. Over a limit the client gets 503 or 429 with `Retry-After: 5`. Wiki: `BW_DOWNLOAD_THREADS` (new, default 8, 1–32) gunicorn threads per worker on top of `BW_THREADS`, used as the places, with one client limited to half. Portal: `hosting/gunicorn.conf.py` runs `PORTAL_PLACES` (2) more threads, one per client. Desktop: 4 of the 8 threads. Test apps leave places off (test clients do not close responses), and the tests switch them on. After the fix, the same live run with 20 readers from one network: `/health` answered 200 in under 10 ms every probe, pages in 10 ms, further downloads from that network got 429. Places were all free again once the readers left. Trade-off: a burst of more than `BW_DOWNLOAD_THREADS` large responses being written at once in one worker is answered 503 instead of queued. Docs: configuration.md, deployment.md, security.md, .env.example, UPGRADING.md.
- **Test:** `tests/security/test_iw2.py::test_iw2_4_large_responses_take_a_place_until_the_server_closes_them`, `::test_iw2_4_the_servers_run_threads_for_the_places`, `::test_iw2_4_the_portal_holds_its_downloads_to_its_places`

### JW2-1 Hosted wikis never got the IW2-4 download threads (full re-audit pass 5, rest of IW2-4)
- **Severity:** High (a hosted wiki stops answering). **Attacker:** any signed-in user of a hosted wiki (any visitor in public mode).
- **Repro:** the tenant entry point started gunicorn with `--threads 4`, which overrides the
  configuration file's `BW_THREADS + BW_DOWNLOAD_THREADS`, while the wiki allowed 8 large
  responses at once (4 per client): with the unchanged tenant command one account's 4 slow
  reads (2 KiB/s) of a 6 MB upload left `/health` unanswered for the whole 25-second run.
  The IW2-4 test only read the configuration module.
- **Fix:** `tenant_entrypoint.web_command` passes `BW_INSTANCE_GUNICORN_THREADS +
  BW_DOWNLOAD_THREADS` as `--threads`, and the runtime sets `BW_DOWNLOAD_THREADS` to 4 for
  tenants (`agent.TENANT_DOWNLOAD_THREADS`; the wiki reads the same variable for its places,
  2 per client), so threads and places come from one number. Live after: `--threads 8`, and
  six slow readers from one account left `/health` at 200 throughout (also with the
  standalone default, `--threads 12`). docs/configuration.md says so.
- **Test:** `tests/security/test_jw2.py::test_jw2_1_hosted_wikis_run_threads_for_their_download_places`.

### JW1-3 Letters that SQLite's tokenizer drops still padded a one-letter search word into a prefix query (full re-audit pass 5)
- **Severity:** Medium. **Attacker:** any user with `search.pages`; an anonymous visitor through the sidebar search in public mode.
- **Repro:** IW1-1 split search words with `[^\W_]+`, which uses Python's current Unicode data. The index's `unicode61` tokenizer uses Unicode 6.1 and treats 21 newer letters (U+19B0–19C0, U+19C8–19C9, U+1CF2–1CF3) as separators. So `"a" + U+19B0×2` passed `MIN_PREFIX = 3` and became the one-letter prefix query `"a"*`. On a wiki of 2,000 pages × 50 KB, twelve such words made `/search` take 6.54 s and `/api/sidebar/search?scope=content` 6.26 s. Expected: about 0.07 s. After the fix, JW1's script on the same data takes 0.01–0.02 s for `/search` and 0.00 s for the sidebar. `fts_phrase(["a"+U+19B0×2])` is now `"a"`.
- **Fix:** `pages/service.py` gets `fts_words()`, which splits text with SQLite's own tokenizer. It inserts the text into a process-wide in-memory FTS5 table with the index's `tokenize=` option (`FTS_TOKENIZE`), reads the terms back through `fts5vocab` ordered by offset, and rolls back, all under a lock (about 16 µs a call). `fts_phrase()` builds the phrase and checks `MIN_PREFIX` against those tokens. `search()` and `search.py` `_phrase()` use it too, and the regexes `_FTS_TOKEN` and `_WORD` are gone. The phrase words are now case- and diacritic-folded, which the index does anyway, so results are unchanged. A word with no token gives the empty phrase `""`, which matches nothing, as before. A survey of all 142,877 characters Python counts as letters or digits checked `a+c+c`, `c+c+a`, `ab+c` and `c` with an independent FTS5 instance. No phrase with a prefix of fewer than 3 characters remained, and every token re-tokenizes to itself. Docs: features.md (search).
- **Test:** `tests/security/test_jw1a.py::test_jw1_3_letters_the_index_ignores_do_not_pad_short_words`, `tests/security/test_jw1a.py::test_jw1_3_the_words_are_split_with_the_index_tokenizer` (checks that `FTS_TOKENIZE` matches the tokenizer `pages_fts` was created with)

### JW1-4 Kanban history restore had no per-route rate limit (full re-audit pass 5)
- **Severity:** Low. **Attacker:** anyone with write access to one board (an invited plain user is enough).
- **Repro:** `POST /kanban/<id>/revert/<entry>` skips the once-a-minute snapshot throttle for large boards twice: a forced `before_revert` snapshot, then the `is_revert` one. It also rewrites every ticket. On a board at the caps, one restore held the writer lock for 0.22 s and stored two snapshots of about 8 MB. Only the global 300 requests a minute per network limited it, while import and export of the same board allow 10 a minute. With 6 parallel loops, another user's column create rose from 0.00 s to 1.24 s. Expected: a per-route limit like import and export.
- **Fix:** `kanban/views.py` `revert` now calls `guard.rate_limited("revert", 10)` after the write check (429 on the 11th call in a minute, per account). The REST API has no restore route. The other history and restore paths need no limit: deleting or clearing history is admin-only and only deletes, board archive and restore only flip a flag, and ticket restore goes through the normal throttled snapshot. `history.record` already skips the forced snapshot when the board matches the latest entry. Docs: features.md (Kanban history).
- **Test:** `tests/security/test_jw1a.py::test_jw1_4_kanban_restores_are_rate_limited`

### JW1-5 A plain administrator skipped the 72-hour unlock delay by demoting the controller and promoting them back (full re-audit pass 5)
- **Severity:** Low. **Attacker:** plain administrator (not the controller), against an editor's protected page.
- **Repro:** The editor `controller` protects "Guarded", and `plainadmin`'s edit is refused. `plainadmin` sets the controller to `user`, edits the page to "admin text" (accepted), then sets them back to `editor`. Observed: `protected_by` was still the controller, so the protection was back in force as if nothing had happened, and the unlock delay HW1-3 enforces was skipped. Expected: refused, or the protection gone for good.
- **Fix:** The documented model is that a demotion ends the controller's protections ("demoting them ends all their protections at once (recorded in the audit log)"), so the lapse is now permanent instead of refusing the demotion. That keeps the documented way to deal with an editor who locks pages against the team. The `user.role_changed` handler (`page_governance/hooks.py` `end_lapsed_protections`, formerly `record_lapsed_protections`) now calls the new `protection.end_all()`. It clears `protected_by`, `protected_at` and any unlock request on every page that person controls, then records `page_governance.protection_lapsed` with the pages as before. Every path that changes a base role emits that event: admin role and custom-role assignment, custom-role base change, self promotion, merge and temporary-role revert. Re-promotion therefore no longer restores anything, and the controller sees that the page is unprotected. `_describe`'s role check stays as a backstop. One edge remains: a role change made while the page_governance feature is switched off (possible only through the server's own commands while protections are in force) does not run the handler. Docs: features.md, user guide en and it (organising).
- **Test:** `tests/security/test_jw1a.py::test_jw1_5_demoting_and_promoting_the_controller_does_not_restore_protection`

### JW1-1 / KW2-1 Two clients hold every download place of a process, so nobody else gets a file, an export or a page over 64 KiB (full re-audit pass 5)
- **Severity:** Medium. **Attacker:** any signed-in user; in public mode one person alone (their account plus their own address without the cookie count as two clients); otherwise two accounts or two networks. Portal: two owners.
- **Repro:** JW1's `places_fill.py` against a live gunicorn with the shipped config (1 worker, `BW_THREADS=4`, `BW_DOWNLOAD_THREADS=8`). alice opened 4 slow readers of a 6 MB upload (2 KiB receive buffer, 512 B/s) and an anonymous visitor from the same address opened 4 more. All 8 got 200, and for the next 10 s carol's 400 KB image got 503 on every try while `/health` and `/` got 200. KW2 saw the same on the tenant command (4 places, 2 per client) with two accounts, two addresses, or one account plus its own address. The refusal also came after the view had run, so a refused data export or whole-site export was built, counted against its limit (5 an hour) and audited, then thrown away. Expected: one person cannot take every place.
- **Fix:** `core/downloads.py` `Places(total)` now gives one client a quarter of the places (`max(1, total // 4)`), so it takes four clients reading slowly to fill a process. Accounts are still not counted against their network as well: a school class behind one NAT would then share a single quarter (BananaChat chose the same). Totals:
  - Standalone wiki: 8 places per worker, 2 per client.
  - Hosting portal: `PORTAL_PLACES` 4 (was 2), 1 per client, 8 threads per worker.
  - Hosted wikis: `TENANT_DOWNLOAD_THREADS` 8 (was 4), 2 per client, so 12 threads. Measured with the tenant command under `BW_MEMORY_LIMIT_MB=768`, `MALLOC_ARENA_MAX=2`: 8 slow downloads held plus 8 concurrent page requests all got 200, `/health` stayed 200, and the worker's address space peaked at 385 MB.
  - Desktop: 12 threads, 8 places (was 8 threads, 4 places).

  New `downloads.place_first` takes the client's place before the view runs; `hold()` keeps it for a large response or gives it back for a small one, and `give_back` (teardown) covers a view that raises. It is applied to these exports:
  - Wiki: personal data export (own and admin), whole-site export, bulk Markdown export, page and revision PDF, attachments ZIP, DM and group chat exports.
  - Portal: owner archive download, admin archive download, platform export.

  So a refused export is neither built, counted nor audited. The kanban board export takes its place first too (added at merge). Slow readers are not cut off under pressure: not done, because it is not simple to do robustly. After the fix, the same live run gave alice 2× 200 and 2× 429, the anonymous visitor the same, and carol's image 200 on every try. Docs: configuration.md, deployment.md, security.md, UPGRADING.md.
- **Test:** `tests/security/test_jw1b.py::test_jw1_1_it_takes_four_clients_to_fill_the_places`, `::test_jw1_1_kw2_1_every_server_gives_four_clients_their_share`, `::test_jw1_1_a_refused_export_is_not_built_counted_or_audited`, `::test_jw1_1_a_place_taken_first_is_given_back_without_a_large_response`, `::test_jw1_1_exports_take_their_place_before_the_work`, `::test_jw1_1_the_portal_refuses_an_export_before_doing_it`

### JW1-2 Small responses sent back to back on one connection, and connections the client never closes, hold the worker (full re-audit pass 5)
- **Severity:** Low (Medium where Gunicorn is reachable without a buffering proxy). **Attacker:** anonymous visitor.
- **Repro:** live gunicorn 26.2.0 with the shipped config (1 worker, 12 threads). JW1's `keepalive_small.py 16 12 /login`: 16 connections, each sending 12 GETs of the 28 KB login page 50 ms apart and reading at 512 B/s. `/health` got no answer within 5 s on every probe. All 12 request threads were in `poll_schedule_timeout` inside `send`, with about 83 KB of Send-Q per attacker connection. Separately, the lead found on BananaChat that gthread closes every finished non-keep-alive connection on its main loop with `util.close_graceful`, which waits up to 2 s for the client to close its side. Confirmed on the wiki: 24 connections that send `GET /health` with `Connection: close` and never close left `/health` unanswered for 10 s or more. Measurements:
  - The close patch alone, keep-alive unchanged: `keepalive_small` still blocked `/health` on every probe (threads in `send`).
  - `keepalive = 0` alone: worse (every request ends in the waiting close).
  - Both together: `/health` 200 in 0.0 s on every probe, with 16×12 `/login`, 16×6 of a 60 KB upload, 64×12 `/login`, and 24 or 200 never-closing clients.

  Cost of `keepalive = 0` on loopback, 500 sequential requests from one client: `/health` 0.80 s against 0.86–0.89 s with keep-alive, `/login` 2.34 s against 2.58 s.
- **Fix:** `ops/write_timeout.py` adds `close_without_waiting` and `patch_graceful_close()`, which replaces `gunicorn.util.close_graceful` (used by gthread and sync through the module attribute). The replacement shuts down the write side, reads only what is already queued (at most 64 KiB, non-blocking), then closes. `ops/gunicorn_conf.py` (and so the root `gunicorn.conf.py` and the tenant command), `hosting/gunicorn.conf.py` and `desktop/gunicorn_child.py` call it at load time, before forking, and set `keepalive = 0`. Nothing found depends on keep-alive: Caddy, browsers and the portal's proxy fallback open a new connection when the server closes one. Trade-off: unread request bytes beyond 64 KiB at close may make the kernel reset the connection, which can cut off the end of an error answer to a client that sent a large body nobody read. A service command or `GUNICORN_CMD_ARGS` that sets `--keep-alive` restores the old behaviour (UPGRADING.md). Docs: deployment.md, security.md, desktop.md, UPGRADING.md.
- **Test:** `tests/security/test_jw1b.py::test_jw1_2_gunicorn_closes_a_connection_without_waiting_for_the_client` (wiki, portal, desktop), `::test_jw1_2_requests_sent_back_to_back_do_not_hold_the_threads` (real gunicorn; fails with the close patch alone)

### KW2-2 The desktop gunicorn child runs without the write timeout (full re-audit pass 5)
- **Severity:** Low. **Attacker:** a device on the LAN of a teacher who shares the wiki (gunicorn backend only).
- **Repro:** code-confirmed. `desktop/gunicorn_child.py` (IW2-3) was empty, so the child had no `post_worker_init` and `ops.write_timeout` was never imported. A device that starts large downloads and stops reading holds the download places and their threads until the wiki is restarted. Expected: the 300 s write timeout of a standalone wiki.
- **Fix:** `desktop/gunicorn_child.py` imports `post_worker_init` from `bananawiki.ops.gunicorn_conf` (`BW_WRITE_TIMEOUT`, 300 s, since the launcher passes no `BW_*` of its own). It also takes JW1-2's `keepalive = 0` and the non-blocking close. IW2-3's isolation is unchanged (`-P`, `-c` with the packaged file, started outside the data folder). The IW2-3 test asserted that the file contained no `import` at all; it now asserts that it imports only `bananawiki.*` modules, which is the property IW2-3 protects. Checked by booting a child with the launcher's flags: `--print-config` shows `keepalive = 0` and `post_worker_init`, and the child serves. Docs: desktop.md.
- **Test:** `tests/security/test_jw1b.py::test_kw2_2_the_desktop_gunicorn_child_installs_the_write_timeout`

### KW2-3 One hosted wiki's own speech token keeps every connection of the shared address busy, and the logs do not say which wiki (full re-audit pass 5)
- **Severity:** Medium. **Attacker:** hosting tenant running plugin code, with its own `bwt1.<id>.<hmac>` token.
- **Repro:** KW2's test (`SpeechServer`, default limits, 1 s per synthesis). Wiki `evil` ran 8 keep-alive `/synthesize` loops with its own token. Over 20 s, wiki `school` got 1 of 24 requests through; 23 ended `RemoteDisconnected` (closed at accept). Every log line showed only the shared address. Expected: one wiki gets at most its share of connections and GPU time, and the log names the wiki.
- **Fix:** `contrib/tts-gpu-server/server.py`:
  - **Busy cap:** `SpeechServer.busy()` caps the busy connections of one tenant token over all addresses at `TTS_MAX_BUSY_PER_TENANT` (new, default 2, a quarter of the per-address 8). A further request gets 429 with `Retry-After` and `Connection: close`, its connection stays evictable, and a warning names the tenant. The wiki postpones on 429.
  - **Master token:** not capped. It is never given to hosted wikis, so standalone wikis behave as before.
  - **Fair GPU queue:** `_FairSlots` replaces the semaphore. A freed slot goes to the waiting tenant served longest ago (the master token counts as one), oldest request first.
  - **Logs:** every request line carries `tenant=<wiki id>`, `operator` or `-`; tokens are never logged.

  GW2-4's eviction rules and the protocol are unchanged. With KW2's test after the fix, `school` got 10 of 10 answers while `evil` hogged (that script now fails, because it asserted the bug). Docs: contrib README, hosting.md, UPGRADING.md.
- **Test:** `tests/security/test_jw1b.py::test_kw2_3_one_wikis_token_cannot_shut_the_other_wikis_out`, `::test_kw2_3_a_free_gpu_slot_goes_to_the_wiki_served_longest_ago`

### LW1-1 Search excerpts cost the square of the query's matches in a page; one planted 16 KB page made each search take 31 s of a thread (full re-audit pass 6)
- **Severity:** Medium. **Attacker:** an editor plants the page; any user with `search.pages`, or an anonymous visitor in public mode (`/api/sidebar/search`), triggers it. On large natural-language wikis no planting is needed.
- **Repro:** LW1's `test_lw1_snippet_quadratic.py`: an editor publishes "Notes" with `"a " * 8000`. Then `/search?q=a` took 0.26 s, `/search?q=a a a a a a a a a a a a` **30.1 s** and the sidebar search with the same words 30.3 s. FTS5's `snippet()`, which GW1-1 had moved to the 20 rows shown, scores every candidate window against every phrase instance. Repeated words were not deduplicated. On LW1's 100 MB wiki of natural-like text (`search_long_phrase.py`), 12 common words took 2.40 s on `/search` and 2.38 s on the sidebar, `"a the" × 74` 2.49 s, and 149 × `a` in quotes 0.56 s. Expected: bounded work per request.
- **Fix:** excerpts no longer use `snippet()`.
  - `pages/service.py` `excerpts()` (replaces `fts_snippets`) reads the first `EXCERPT_SCAN` (20,000) characters of each page shown. It marks them with FTS5 `highlight()` in a private in-memory FTS5 table with the same tokenizer and phrases, so prefixes, phrases and diacritics match as in search. `highlight()` costs time linear in the slice, not quadratic in the matches. The excerpt is a window of 14 words (sidebar/API) or 16 words (`/search`) cut in Python around the first match. A run of characters without a space counts as one word per 30 characters, so an excerpt stays under about 500 characters. The `\x02`/`\x03` markers, `snippet_text` and `snippet_html` are unchanged. A `title:` term is not highlighted in the text, as before.
  - Repeated words count once: `dict.fromkeys` in `service.search`, and `search._bounded` deduplicates terms, quoted phrases and exclusions. `/search` also counts at most `MAX_WORDS` = 24 words in all, so a longer phrase is cut and later terms are dropped.
  - Backstop: `service.time_limit()` installs a SQLite progress handler on the request's connection around the search statements (`/search` count and rows; the sidebar, picker and REST search). It stops them after `SEARCH_SECONDS` (5 s) with `SearchTooSlow`, a 503 carrying the new message `pages.search.too_slow` (en/it/de); REST answers 503 `service_unavailable`. The handler is removed in `finally`. Any `OperationalError` after the deadline counts as the stop, because an interrupt while SQLite opens the FTS table reports "vtable constructor failed" rather than "interrupted".
  - Trade-off: a match after the first 20,000 characters of a page gets the start of the page as its excerpt, without a highlight.
  - After the fix: the planted page 0.01 s (both routes). On the natural wiki, 12 common words 0.17 s / 0.16 s, `"a the" × 74` 0.08 s, 149 × `a` 0.14 s, the exclusion case 0.72 → 0.19 s. With 20 planted pages of 1 MB of 12 distinct words plus a 24 KB one: 0.29 s.
  - Docs: features.md, api.md, UPGRADING.md. `tests/security/test_gw1.py::test_gw1_1_excerpts_are_made_for_the_rows_shown_only` asserted that `snippet()` was used for the shown rows. It now asserts the same bound for the new excerpt query (`substr(content, 1, …) … WHERE id IN`, no `ORDER BY`) and that no `snippet()` runs.
- **Test:** `tests/security/test_lw1.py::test_lw1_1_excerpts_do_not_cost_the_square_of_the_matches`, `::test_lw1_1_excerpts_are_bounded_by_the_scan_and_the_word_count`, `::test_lw1_1_repeated_words_and_long_phrases_are_bounded`, `::test_lw1_1_a_search_that_runs_too_long_is_stopped`

### LW1-2 Ticket assignees were outside the W4-1 board caps, so every one-field edit wrote a 12.5 MB snapshot under the write lock (full re-audit pass 6)
- **Severity:** Medium. **Attacker:** anyone with write access to one board that many accounts can reach (a plain user with a write share while kanban access is "all", or a board shared with a role).
- **Repro:** LW1's `test_lw1_kanban_assignees.py` (5,000 tickets, 102 assignable accounts). Mallory bulk-assigned everyone to every ticket in 10 requests. `board_bytes` reported 0.52 MB, under both the 1 MB throttle and the 8 MB cap, with 510,000 assignee rows. Each `PUT /api/kanban/tickets/<id>` then took 1.2–1.3 s and stored a 12.5 MB snapshot, unthrottled. The board state was 22 MB. Expected: the board counts as large, or the assignment is refused.
- **Fix:**
  - `kanban/fields.py` adds `MAX_ASSIGNEES_PER_TICKET` = 20. `service._assignees` refuses more with `kanban.error.too_many_assignees` (400, en/it/de). That covers ticket create (web and REST, the `@user` shorthand included), update and bulk assign. `store.set_assignees`, the only writer of `kanban_ticket_assignees`, keeps the first 20, which covers import and revert of snapshots that name more.
  - `history.board_bytes` now adds each assignee's id and username bytes (plus quoting), as in a snapshot. So the 8 MB cap, the import and revert checks, ticket create, checklist items, and the once-a-minute snapshot throttle for boards over 1 MB all see assignees.
  - `update_ticket` now runs `check_room` when a ticket gains assignees. Bulk assign runs `check_room(allowed=size before)`.
  - `remove_invalid_assignees` only removes assignees. Account merge and deletion do not add any.
  - Tickets that already have more than 20 assignees keep them until their assignees are changed (UPGRADING.md).
  - After the fix, the same repro is refused at the first bulk request (400). With the maximum of 20 people on each of the 5,000 tickets, `board_bytes` is 2.8 MB, so the board is throttled: an edit took 0.03 s and wrote no new snapshot, and the state was 5.2 MB.
  - Docs: features.md, api.md, UPGRADING.md.
- **Test:** `tests/security/test_lw1.py::test_lw1_2_a_ticket_has_at_most_twenty_assignees`, `::test_lw1_2_assignees_count_towards_the_board_caps`

### LW1-3 The canvas export was left out of JW1-1's `place_first`, so a refused export was still built and counted (full re-audit pass 6)
- **Severity:** Low. **Attacker:** any user who can edit a canvas, while the process's download places are taken or the user holds their quarter.
- **Repro:** LW1's `test_lw1_canvas_export_place.py`. With `Places(1)` held by another network, 11 `GET /canvas/<slug>/export` answered 503 ×10 and then 429. `build_export`, which can deflate up to 512 MB of images, ran 10 times, and once the place was free the next export still got 429. Expected: 503 before building, and nothing counted.
- **Fix:** `canvas/routes.py` `export` is decorated with `@downloads.place_first`, outermost under the route, as the other exports are. `canvas.export` is added to `EXPORTS` in `tests/security/test_jw1b.py`. No compatibility impact.
- **Test:** `tests/security/test_lw1.py::test_lw1_3_a_refused_canvas_export_is_not_built_or_counted`, `tests/security/test_jw1b.py::test_jw1_1_exports_take_their_place_before_the_work`

### LW2-1 The GPU speech server writes client request lines to its log with raw control characters (full re-audit pass 6)
- **Severity:** Low. **Attacker:** anyone who can reach the speech server's port, no token needed (on a hosting platform, tenant plugin code on the shared network).
- **Repro:** LW2's `tts_log_escape.py` ran the server with `main()`'s logging set-up and sent `GET /health\x1b]0;all good\x07 HTTP/1.1` and `GET /\x1b]52;c;...\x07 HTTP/1.1 junk` without a token. Both were answered 400, and the log held both request lines with raw ESC and BEL, so an OSC 0 (window title) or OSC 52 (clipboard write) sequence reached the operator's terminal. `Handler.log_message` overrode `BaseHTTPRequestHandler.log_message`, which escapes control characters, and logged `fmt % args` as it was. Expected: escaped, as Python's own handler does.
- **Fix:** `contrib/tts-gpu-server/server.py` escapes the logged message with `_LOG_ESCAPES`, the same set as `core.logformat.printable`: C0 (line feed and tab included, so a line cannot forge another), DEL, C1, U+2028/2029 and the bidirectional overrides. The table is copied in because the server must stay standard library only, and Python 3.11 has no `_control_char_table`. The KW2-3 tenant field (`tenant=<id>`, `operator`, `-`) is unchanged, and the token is still never logged. README: the API section says so.
- **Test:** `tests/security/test_lw2.py::test_lw2_1_speech_server_logs_request_lines_with_control_characters_escaped`

### LW2-2 A device on the LAN takes a shared desktop wiki offline by sending request headers one byte at a time (full re-audit pass 6)
- **Severity:** Medium for a wiki shared on the LAN; Low otherwise (local processes only). **Attacker:** an anonymous device on the teacher's network once "share on LAN" is on.
- **Repro:** LW2's `desktop_waitress_trickle.py` and `desktop_trickle.py` ran the real `WikiServer`. Each slow connection sent `GET / HTTP/1.1\r\nHost: ...\r\nX-Slow: ` and then one byte every 30 s.
  - Waitress, 100 connections: `/health` got no answer on any probe for 150 s. Waitress's `channel_timeout` counts from the last byte received, and once `connection_limit` (100) connections were open it accepted nothing more, the teacher's browser on 127.0.0.1 included.
  - Gunicorn, 16 connections: no answer for 70 s. Its gthread worker reads headers on a blocking socket with no timeout, so the slow connections held all 12 threads.
  - Expected: a client that does not finish its headers in time is dropped, and one address cannot hold every connection or thread.
- **Fix:** the launcher now sets the limits a proxy would. The values are in `desktop/server.py`.
  - Waitress: new `desktop/waitress_server.py` `LimitedServer`, which `_WaitressRunner` now builds instead of calling `waitress.create_server`.
    - A request's headers must be complete within `HEADER_TIMEOUT` (30 s). The time counts from when the channel starts waiting for a request: when it is accepted, or once the previous response is sent. A trickling client or an idle keep-alive connection is closed. Bodies are not bounded by it.
    - `accept()` closes a connection straight away when its address already has `CONNECTIONS_PER_ADDRESS` (16) open, and logs that once per episode. `connection_limit` is raised from 100 to 256.
    - Expired channels are found in the server's `maintenance` pass. `cleanup_interval` is now 5 s, down from 30. They are closed through the trigger on the server thread, because closing a socket while the loop builds its `select` lists would hand it a closed descriptor. A bare `will_close` is not enough: it only takes effect once the socket is writable.
  - Gunicorn child: new `desktop/gunicorn_limits.py` `HeaderGuard`, installed by `gunicorn_child.post_worker_init` together with the write timeout. Gunicorn has no setting or hook before the headers are read (`pre_request` runs after them), and its socket timeout is reset by `setblocking(True)`, so the guard wraps the worker's `handle` and `handle_request`.
    - A watchdog shuts down (`SHUT_RDWR`) a connection whose headers are not in 30 s after its thread took it. The blocked read then ends, and gunicorn closes the connection as a client that went away.
    - While `HEADER_READERS_PER_ADDRESS` (8 of the 12 threads) connections from one address are still sending headers, a further one from that address is closed at once. Browsers send headers immediately, so only slow clients count.
    - `post_worker_init` refuses to start a worker that is not gthread.
  - Live check, gunicorn child on port 19081: 9 trickling connections from 127.0.0.2. The 9th was closed at once, `/health` from 127.0.0.1 answered 200, and the other 8 were dropped at 29.5 to 30.8 s.
- **Residual** (in docs/desktop.md, "Limits per device"):
  - Several devices together, or one device with many addresses, can still fill the 256 connections or gunicorn's threads.
  - With gunicorn, a request body sent slowly holds a thread for as long as the client keeps sending. Waitress buffers bodies on its event loop, so there it holds only one of the address's 16 connections.
  - Recommendation: share with waitress, the default and the only backend in desktop builds.
- **Test:** `tests/security/test_lw2.py`:
  - Attacks: `::test_lw2_2_waitress_drops_a_client_that_trickles_its_headers`, `::test_lw2_2_waitress_drops_a_connection_that_sends_nothing`, `::test_lw2_2_waitress_caps_the_connections_of_one_address`, `::test_lw2_2_gunicorn_drops_a_client_that_trickles_its_headers`, `::test_lw2_2_gunicorn_caps_the_slow_header_readers_of_one_address`.
  - No false positives: `::test_lw2_2_waitress_lets_slow_but_complete_headers_and_bodies_through`, `::test_lw2_2_gunicorn_serves_ordinary_requests`.

### LW2-3 With waitress, the default desktop backend, a download the client stops reading keeps its place and thread until restart (full re-audit pass 6)
- **Severity:** Low. **Attacker:** a device on the LAN of a teacher who shares the wiki. In public mode no account is needed: an anonymous visitor and a signed-in account count as two clients.
- **Repro:** LW2's `desktop_waitress_stall.py` used the real `WikiServer` with waitress and a 24 MiB upload. An anonymous client and the signed-in `pupil` each opened two GETs with a 4 KiB receive buffer and read only the status line.
  - Observed: `places taken 4/8` at every check from 60 s to 420 s, and a further download got 429. For a body larger than `outbuf_high_watermark` (16 MiB), the application thread waits in `outbuf_lock.wait()` with no timeout. `maintenance()` never closes a channel with a request in progress, so `channel_timeout` did not apply.
  - Expected: as with gunicorn (KW2-2), a client that takes in nothing for `BW_WRITE_TIMEOUT` is dropped and its place freed.
- **Fix:**
  - `desktop/waitress_server.py`'s maintenance pass closes a channel that has output pending and has sent no byte for `WRITE_TIMEOUT` (300 s, gunicorn's `BW_WRITE_TIMEOUT` default, which the data folder's environment never changes). This applies when no request is running, or when the running request has at least `send_bytes` queued, which the event loop would send.
  - The close goes through the trigger, because the stalled socket is never writable, so `will_close` would never act. `handle_close` wakes the application thread. Waitress's `write_soon` then raises `ClientDisconnected`, the task closes the body, and the body's close gives back the download place.
  - A response still being computed, with nothing pending, is not affected. Any byte sent counts as progress. Gunicorn's write timeout instead needs a minimum amount per period (`FLOOR`), so a very slow but steady reader keeps its place longer with waitress (residual, docs/desktop.md).
  - Committed with LW2-2: both live in the new waitress server and its maintenance pass.
- **Test:** `tests/security/test_lw2.py::test_lw2_3_waitress_drops_a_download_its_client_stopped_reading`. The test checks that the body is closed, the place is given back, the connection is closed, and the next client gets the whole download. Also `::test_lw2_3_waitress_keeps_a_response_that_is_still_being_worked_on` (no false positive).

### LW2-4 A wiki refused for its busy cap could still push another wiki's new connection out of the GPU speech server (full re-audit pass 6, rest of KW2-3)
- **Severity:** Low. **Attacker:** a hosted wiki with plugin code holding its own speech token.
- **Repro:** `tests/security/test_jw1b.py::test_kw2_3_one_wikis_token_cannot_shut_the_other_wikis_out`
  failed about one run in six: connections answered 429 for their tenant's busy cap still counted
  as pending, so a wiki reconnecting after each 429 evicted another wiki's newest connection, and
  `socketserver`'s listen backlog of 5 dropped connection attempts while it reconnected.
- **Fix:** `contrib/tts-gpu-server/server.py`: a connection refused for the busy cap is marked
  *crowded*: it is the first to make room and never counts as pending; the server listens with a
  backlog of 128. The KW2-3 test then passed 30 consecutive runs.
- **Test:** `tests/security/test_lw2.py::test_kw2_3_crowded_connections_never_push_out_another_wikis_new_connection`,
  `::test_kw2_3_connection_attempts_are_not_dropped_by_a_short_listen_backlog`.

### MW1-2 The text log keeps line feeds from logged values (forged log lines), and the JSON log leaves C1 controls raw (full re-audit pass 7)
- **Severity:** Low. **Attacker:** anonymous visitor, whenever a request ends in an unhandled error; anyone whose text reaches a `%s` of a log call.
- **Repro:** With public mode on, an anonymous `GET /api/embed/canvas/x%0A2026-10-28%2001:00:00,000%20WARNING%20[bananawiki]%20Login%20failed%20for%20admin%20from%20203.0.113.9%C2%9B31m%E2%80%AE` answered 500. The text log (`BW_LOG_FORMAT=text`) got two lines: `... ERROR [bananawiki] Unhandled error on GET /api/embed/canvas/x` and a second line that read as a real record with the sender's timestamp, level and logger. `TextFormatter` escaped controls through `printable()`, which keeps line feeds, and `request.path` is percent-decoded. The JSON log escaped the line feed but wrote U+009B (8-bit CSI) and U+202E raw because of `json.dumps(ensure_ascii=False)`. Expected: one line per record, every control escaped in both formats.
- **Fix:** `bananawiki/core/logformat.py`: `TextFormatter.formatMessage` also writes line feeds in the message as `\n`. The traceback and stack that `format()` appends after the message keep their lines. `JsonFormatter.format` translates the dumped line through `_JSON_ESCAPES`, the characters of `_ESCAPES` written as JSON `\uXXXX`, so C1, DEL, U+2028/2029 and the bidi overrides are escaped and the line stays valid JSON. Other non-ASCII text is still written as it is. This covers the wiki, the portal, the runtime agent and `sudo bananawiki`, which share these formatters. No log call in the code base has a deliberate line feed in its message. docs/operations.md and UPGRADING.md describe the change.
- **Test:** `tests/security/test_m1.py::test_mw1_2_a_line_feed_in_a_logged_value_does_not_start_a_new_text_line`, `::test_mw1_2_a_traceback_keeps_its_lines_after_the_one_line_message`, `::test_mw1_2_json_lines_escape_c1_and_bidi_controls_and_stay_valid`, `::test_mw1_2_the_wiki_log_holds_one_line_for_a_500_on_a_forged_path`

### MW3-1 Impersonating a member skips the audit entries for data export and private-conversation reads and writes (full re-audit pass 7)
- **Severity:** Low. **Attacker:** plain administrator. Impersonating a non-administrator needs no superuser.
- **Repro:** A plain admin's direct `GET /admin/users/<bob>/export` and `GET /admin/chats/<id>` recorded `user.data_exported` and `chat.dm_read`. After `POST /admin/users/<bob>/impersonate`, nothing new was recorded for any of these: `GET /settings/export` (200, the full ZIP of bob's data), `GET /chats` and `/groups` (latest-message previews), `GET /chats/<id>`, `/chats/<id>/messages` and `/chats/<id>/export` (200, holding the private text), and `POST /chats/new` and `/chats/<id>/send`. The sent message was stored with `sender_id` = bob, and the audit log held only `user.impersonation_started`. Private groups behaved the same way. The confirmation before impersonating says "Your actions will be logged". Expected: the same entries as the administrator routes, under the administrator's name.
- **Fix:** I chose to record these actions rather than refuse them, which keeps impersonation working as the prompt describes it. `user_data_export/routes.py`: `own_export` passes `auth.real_user()` as requester, so an impersonated self-export records `user.data_exported` (actor: the administrator) like the admin route. `chat/web.py`: `audit_impersonated()` records the administrator routes' entries while impersonating, with an `as_user` detail. It covers DM view, poll, export and attachment (`chat.dm_read`, `chat.dm_exported` (new), `chat.dm_attachment_read`) and the private-group equivalents (`chat.group_read`, `chat.group_exported`, `chat.group_attachment_read`), but not the global room. JSON polling is recorded once per 10 minutes, and opening the page always records. A chat blueprint `after_request` hook records each non-GET chat request made in the member's name as `chat.impersonated_action`, refused attempts included, with endpoint, status, target and message id. That covers sends, deletions, reports, new conversations, clears, blocks and group moderation. The DM and group lists (`/chats`, `/groups`) showed the first 90 characters of every private conversation's latest message with no entry, so while impersonating `withhold_previews()` (`chat/web.py`) leaves those previews out (the global room keeps its own) and the list says that opening the conversation is recorded; the lists themselves record nothing, since nothing private is shown. Marking a conversation read is not recorded. Members acting for themselves are still not audited. Labels and the withheld-preview text are in en/it/de, and docs/security.md and UPGRADING.md are updated.
- **Test:** `tests/security/test_m1.py::test_mw3_1_an_impersonated_data_export_is_audited`, `::test_mw3_1_reading_a_dm_while_impersonating_is_audited`, `::test_mw3_1_a_dm_sent_in_the_members_name_is_audited_with_the_message`, `::test_mw3_1_a_private_group_read_and_written_while_impersonating_is_audited`, `::test_mw3_1_a_member_exporting_their_own_data_is_not_audited`, `::test_mw3_1_members_reading_and_sending_themselves_are_not_audited`, `::test_mw3_1_the_conversation_lists_show_no_previews_while_impersonating`, `::test_mw3_1_attachments_downloaded_while_impersonating_are_audited`

### MW2-1 Pending, denied and suspended accounts get the page tree and page excerpts on published custom pages (full re-audit pass 7)
- **Severity:** Medium. **Attacker:** Unapproved account (anyone, where open sign-up with approval is on), denied account, or suspended account.
- **Repro:** With custom_pages on, a category 'Board Secrets Zq' holding 'Reorg Plan Zq' (body 'SalaryTableZq'), an uncategorised page and a published wiki_page custom page `/about-us`, an account with approval_status pending (also denied, also suspended=1) got 302 to /account-status from `/` and `/page/<slug>`, but `GET /about-us` answered 200 with the category and both titles in the sidebar tree; a builder custom page with a recent-pages block showed titles and the excerpt 'SalaryTableZq', and `/api-docs` (api_service on) listed the titles. Anonymous visitors saw none of it. Cause: `_pipeline` skipped the approval gate on `@auth.public` views but left `g.user` set to the refused account. Expected: refused accounts served as anonymous visitors.
- **Fix:** `bananawiki/wiki/app.py` `_pipeline`: a refused account on an `@auth.public` view not exempt from the `approval` gate now runs it with `g.user = None` (its language resolved first, `g.real_user` kept), as `load_page_user` already does for error pages; this covers the custom page catch-all, `/_cpf`, `/_cpd`, `/api-docs` and any plugin's public views. The members-only gate of custom pages (`custom_pages/routes.py` `_members_gate`) still sends refused accounts to their status via `auth.real_user()`. `/signup` and the `/admin` entry, which handle signed-in accounts themselves, are marked `@auth.exempt("approval")` so they behave as before. Documented in ARCHITECTURE.md, docs/security.md and UPGRADING.md.
- **Test:** `tests/security/test_m2.py::test_mw2_1_refused_accounts_get_no_page_tree_or_excerpts_on_public_views`, `tests/security/test_m2.py::test_mw2_1_approved_members_keep_the_page_tree_on_public_views`

### MW2-2 A history diff of two planted versions costs over 2 s of CPU on every request, with no per-route limit (full re-audit pass 7)
- **Severity:** Medium. **Attacker:** An editor plants two versions; any signed-in reader triggers it (history.view is a default for plain users).
- **Repro:** An editor saved 'Notes' as `'@abc ' x 200000` and then `'@abd ' x 200000`; a plain reader requested `/page/notes/history/<latest>?view=diff` 8 times: all 200, 2.1-2.3 s of CPU and 16.6 MB each, never cheaper on repeats (verified independently at 1.2-1.65 s and 13.6 MB with another payload). 120 requests in one window all answered 200: no per-route limit. `rendered_diff` bounds only SequenceMatcher; splitting, splicing and the second sanitize scale with the rendering (up to 16 M characters). Expected: a bounded cost per request and a per-route limit.
- **Fix:** `bananawiki/wiki/features/pages/diff.py`: `rendered_diff` returns None when either rendering is over `RENDERED_DIFF_CHARS` (1 M characters, about 0.5 s of CPU at worst); `page_history/routes.py` `history_entry` then shows the source diff with a note (`page_history.diff_too_large`, en/it/de) and is limited to 30 requests a minute per account (`rate_limited("history-view", 30)`). The renderings of both versions are now cached (MW2-3). Documented in docs/features.md and UPGRADING.md.
- **Test:** `tests/security/test_m2.py::test_mw2_2_rendered_diff_of_large_versions_shows_the_source_diff`, `tests/security/test_m2.py::test_mw2_2_history_versions_are_rate_limited_per_account`

### MW2-3 Renderings over 8 MiB as Python holds them are never cached, so every view of a planted page renders it again (full re-audit pass 7)
- **Severity:** Low. **Attacker:** An editor plants the page; anonymous visitors in public mode, or any reader, pay the cost on every view.
- **Repro:** In public mode an editor created a page of U+1F600 + `'@someone ' x 111000`. Anonymous `GET /page/<slug>` took 0.34-0.43 s of CPU and 6.0 MB on every request, and `preview-by-slug` 0.31 s each time; the same content without the emoji was cached (0.02 s on repeats). `_RenderCache.put` dropped entries whose `sys.getsizeof` exceeded `RENDER_CACHE_LIMIT // 4` (8 MiB), while `_render` accepted 16 M characters; one character above U+FFFF makes CPython hold the 6 M-character rendering in 24 MB. Expected: an accepted rendering is rendered once.
- **Fix:** `bananawiki/wiki/markdown.py`: the render cache keeps entries UTF-8 encoded (decoded on a hit, a few ms for the largest) and takes entries up to half of `RENDER_CACHE_LIMIT`; `_render` shows the escaped source for HTML over the new `MAX_HTML_BYTES` (16 MB encoded) as it does over `MAX_HTML_CHARS`, so every rendering it returns, the escaped source of a 1 M-character page included (at most 5 MB), fits in one entry. ASCII pages keep the 16 M-character limit. Noted in UPGRADING.md.
- **Test:** `tests/security/test_m2.py::test_mw2_3_a_page_with_an_emoji_is_rendered_once`, `tests/security/test_m2.py::test_mw2_3_renderings_over_the_byte_bound_show_the_source_and_are_kept`, `tests/security/test_m2.py::test_mw2_3_anonymous_views_of_a_planted_page_render_it_once`

### MW2-4 A sidebar search with no index words runs a LIKE costing pattern x page length per page, past the 5 s LW1-1 backstop (full re-audit pass 7)
- **Severity:** Medium. **Attacker:** An editor plants the pages; any user with search.pages, an anonymous visitor in public mode (/api/sidebar/search is public_read) or a `pages`-scope API token triggers it.
- **Repro:** In public mode an editor created 6 pages of 999,000 U+1F600 each; anonymous `GET /api/sidebar/search?scope=content&q=<U+1F600 x199 + '!'>` took 6.7 s and answered 200 (30 pages: 33.6 s), while `q=hello` took 0.00 s and `/search` with the same query 0.04 s. With no index token `service.search` ran `title LIKE ? OR content LIKE ?` with the whole query, and one row's LIKE is a single SQLite step, so the progress handler (every 10,000 steps) never fired. Expected: bounded work, stopped at 5 s at the latest.
- **Fix:** `bananawiki/wiki/features/pages/service.py` `search`: with the FTS5 index present, a query without index words returns no results (as `/search` does since FW1-1). Without FTS5, the LIKE matches only the first `LIKE_CHARS` (32) characters of the query and runs under `time_limit(_LIKE_PROGRESS_STEPS)` (the clock read every 10 steps), so the deadline holds to about one page's LIKE (measured: a 0.2 s deadline stopped at 0.38 s over 1 M-character emoji pages, 1.0 s at 1.10 s). Documented in docs/features.md and UPGRADING.md.
- **Test:** `tests/security/test_m2.py::test_mw2_4_queries_without_index_words_find_nothing_with_the_index`, `tests/security/test_m2.py::test_mw2_4_the_like_fallback_is_short_and_stops_at_the_deadline`

### MW1-1 Anonymous JSON requests to canvas views crashed with 500 (abort() was given redirect_to_login()'s tuple) (full re-audit pass 7)
- **Severity:** Low. **Attacker:** anonymous visitor (public mode on).
- **Repro:** public mode on, anonymous `GET /api/embed/canvas/nope` (and `/sync`, or `/canvas/nope`,
  `/data`, `/outline`, `/history` with `Accept: application/json`) answered 500 and logged a full
  `LookupError` traceback each time: `canvas/routes._deny` called `abort(auth.redirect_to_login())`,
  which returns a `(body, 401)` tuple to JSON callers. Expected: 401 JSON, as the Kanban guard answers.
- **Fix:** `canvas/routes._deny` turns the helper's answer into a response first
  (`abort(make_response(auth.redirect_to_login()))`): JSON callers get the 401 login-required body,
  HTML callers keep the redirect to the sign-in page.
- **Test:** `tests/security/test_m3.py::test_mw1_1_anonymous_json_canvas_request_answers_401`,
  `tests/security/test_m3.py::test_mw1_1_anonymous_html_canvas_request_still_redirects_to_login`

### MW3-2 Canvas import stored its images without the daily upload quota (full re-audit pass 7, rest of W4-3)
- **Severity:** Low. **Attacker:** anyone who may create canvases (administrators included: the quota has no exemption).
- **Repro:** `upload_quota_per_day_count=2`: three `/canvas/<slug>/upload` calls answered 200, 200, 429,
  but two `POST /canvas/import` of a zip with 20 image nodes each answered 302, stored 40 files and
  left `upload_window_count` at 2. `transfer._read_bundle` called `storage.save` directly.
- **Fix:** `canvas/transfer.read_import` takes the importing user (`routes.import_canvas` passes it).
  Before reading any asset the import checks the user has room for all its images
  (`pages/uploads.check_room`), and after storing them counts files and bytes in one step
  (`pages/uploads.count_stored`, `_check_quota` gained a `files` count). When the quota does not allow
  them the import is refused whole and the stored files are removed, so a refused import costs no
  quota. When `service.create` then refuses the canvas (a document that fits as exported can pass the
  size limit once its images have their longer stored names), `transfer.discard(stored, user)` removes
  the files and gives their count back (`pages/uploads.uncount_stored`). Compatibility: on wikis with a daily quota, an import with more images than are left today is
  refused (documented in docs/features.md).
- **Test:** `tests/security/test_m3.py::test_mw3_2_canvas_import_counts_its_images_against_the_daily_quota`,
  `tests/security/test_m3.py::test_mw3_2_canvas_import_obeys_the_daily_byte_quota`,
  `tests/security/test_m3.py::test_mw3_2_an_import_refused_when_the_canvas_is_created_costs_no_quota`

### MW3-3 Unlimited board creation and an unpaged board list made each /kanban view cost seconds of CPU (full re-audit pass 7)
- **Severity:** Medium. **Attacker:** an account holding `kanban.create` (editors under `kanban_write_access=editor`, everyone under open access); afterwards any viewer of /kanban, anonymous visitors included with public Kanban access.
- **Repro:** `kanban_access=all`, `kanban_write_access=editor`: an editor posted `/kanban/create` 295
  times with a 10,000-character table description, all accepted within 1.3 s; a plain user's
  `GET /kanban` then took 2.1 s on every view (14.8 s at 2,065 boards), because every board's excerpt
  was rendered on every request, unpaged, and the archived list was loaded in full to count it.
- **Fix:** board creation is limited to 10 boards a minute per account (`kanban/guard.CREATES_PER_MINUTE`),
  one budget shared by the web route `kanban/views.create` and the REST API
  `POST /api/v1/kanban/boards` (`api_service/api/kanban_api.create_board`, which answers 429
  `rate_limited` with `Retry-After: 60`; both count through `guard.within_limit`), so using both does
  not double it; `POST /api/kanban/board-order` is limited to 30 saves (`guard.rate_limited`). The list shows `LIST_PAGE_SIZE`
  (100) boards a page, read with LIMIT/OFFSET (`forms.pagination`); archived boards are counted with
  `COUNT(*)` (`access.count_boards`) and at most the first 100 are listed, with a note when more exist.
  Excerpts are kept in the render cache by source hash (`markdown.excerpt(..., keep=True)`). Only the
  first page shows the reorder controls, so a move never sends a partial order from a later page.
  `kanban-list.js` saves arrow-button moves one second after the last one (a drop saves at once) and
  sends a pending save with `keepalive` when the page is left (`BW.fetchJSON` passes `keepalive`), so a
  keyboard user moving a board many places sends one save instead of meeting the 30-a-minute limit;
  the list order is self-healing anyway, as each save sends the full order. The script change has no
  automated test: no JavaScript runtime was available (the repository's node-based script tests skip
  without one); documented in docs/features.md.
  Measured: 600 boards with 10,000-character tables, first view 0.9 s, then 0.016 s.
  Compatibility: the HTML list is paged (the REST list was paged already by W3-5).
- **Test:** `tests/security/test_m3.py::test_mw3_3_board_creation_is_rate_limited`,
  `tests/security/test_m3.py::test_mw3_3_rest_board_creation_shares_the_per_account_limit`,
  `tests/security/test_m3.py::test_mw3_3_board_order_is_rate_limited`,
  `tests/security/test_m3.py::test_mw3_3_board_list_is_paged_and_excerpts_are_kept`,
  `tests/security/test_m3.py::test_mw3_3_the_archived_list_is_capped`

### MW3-4 Every canvas operation rewrote the whole document twice (layout and coalesced history) under the write lock (full re-audit pass 7)
- **Severity:** Medium. **Attacker:** anyone with edit access to one canvas (an edit share, or everyone under `canvas_open_access`).
- **Repro:** a plain user with an edit share filled a canvas to 5 MB through `/ops`, then sent
  one-node `upsert_node` ops: 0.056 s each, each rewriting about 10 MB (the layout plus the history
  entry, which `record_history` loaded twice into Python, compared and rewrote), 240 a minute per
  account; three such loops slowed another user's unrelated write from 0.037 s to 0.32 s worst.
- **Fix:** `canvas/service.record_history` compares and copies snapshots inside SQLite (no 5 MB values
  through Python). A canvas over `LARGE_DOCUMENT_BYTES` (1 MB) has its latest coalesced entry
  rewritten at most once every `HISTORY_MIN_SECONDS` per worker (the in-memory limiter, as the
  per-account limits). Changes in between are written into that entry by its next rewrite, before a
  change that starts a new entry (another person, or a new session: `keep_pending_history`, at most
  one extra copy per session; a first version missed this and lost them, found in review) and before
  a restore (`_flush_history`), as W4-1 does for Kanban; this is in the service, so the REST API is covered too.
  Editor batches and whole-document saves on such a canvas are limited to 60 and 15 a minute per
  account (`canvas/service.large_write_allowed`), one budget shared by the web routes
  (`canvas/routes`) and the REST API (`POST /api/v1/canvas/<slug>/ops`, `PUT /canvas/<slug>/document`,
  which answer 429 `rate_limited` with `Retry-After: 60`), so using both does not double it; the
  editor already retries a refused batch after 4 s with everything not yet sent. Measured on 5 MB: 0.040 s per op
  (history 0.0002 s, was 0.0126 s), and at most 60 such ops a minute per account. Compatibility: on a
  canvas over 1 MB the latest history entry can lag the live document until the next change or
  restore; no change is lost.
- **Test:** `tests/security/test_m3.py::test_mw3_4_large_canvas_history_is_rewritten_at_most_once_a_minute`,
  `::test_mw3_4_changes_skipped_by_the_throttle_reach_history_before_a_new_entry`,
  `tests/security/test_m3.py::test_mw3_4_small_canvas_history_still_follows_every_edit`,
  `tests/security/test_m3.py::test_mw3_4_large_canvas_ops_and_saves_have_a_lower_limit`,
  `tests/security/test_m3.py::test_mw3_4_large_canvas_budget_is_shared_with_the_rest_api`

### MW3-5 Deleting an account destroyed the private canvases it had shared with others (full re-audit pass 7)
- **Severity:** Low. **Attacker:** n/a (data loss), on any account deletion: by an administrator, a temporary-account schedule, self-deletion or the hosting portal.
- **Repro:** alice creates "Team plan" (private by default) and shares it with bob for editing; bob
  adds a node; alice is deleted (`/settings/delete` or `accounts.delete(..., deleted_by=owner)`). The
  canvas row, bob's node, its history and shares were gone and bob got 404:
  `accounts._hand_over_shared_canvases` only moved canvases with `visibility != 'private'`, and
  `canvas__layouts.creator_id` cascades. The hosting portal's "remove wiki user" task
  (`ops/tenant_task.remove_user`, from `hosting/admin.py cmd_instance_remove_user`) handed over no
  canvas at all: it ran `DELETE FROM users` after the kanban hand-over, so "Team plan" and a
  "shared" canvas were both destroyed (`canvases = []`).
- **Fix:** the hand-over is now `accounts.hand_over_canvases(session, user_id, deleted_by)`, which
  needs no Flask application and is called by `accounts.delete` (every in-wiki deletion) and by
  `tenant_task.remove_user` (the portal; there the heir is the longest-standing active owner or
  administrator). Besides shared and public canvases it hands over a private canvas that has a
  `canvas__permissions` row granting view or edit to another person or to a role, and, while
  `canvas_open_access` is on (every user with global access may then edit every canvas not archived),
  every canvas that is not archived; private canvases with a share row are kept whether archived or
  not, and only private canvases shared with nobody (or, under open access, archived) go with the account. Documented under Canvases in docs/features.md.
  Not changed: the hand-over records no canvas history entry and emits no `canvas.updated`, as for
  shared canvases before; the deletion warning text is unchanged.
- **Test:** `tests/security/test_m3.py::test_mw3_5_private_canvas_shared_with_a_person_survives_its_creators_deletion`,
  `tests/security/test_m3.py::test_mw3_5_private_canvas_shared_with_a_role_survives_self_deletion`,
  `tests/security/test_m3.py::test_mw3_5_portal_removal_keeps_canvases_others_use`,
  `tests/security/test_m3.py::test_mw3_5_private_canvas_under_open_access_survives_its_creators_deletion`

### MW4-1 Self-service sign-out events are not capped per account, so the 30-day sign-out queue can grow large (full re-audit pass 7)
- **Severity:** Low. **Attacker:** approved hosting user.
- **Repro:** A member who consented to 6 platform wikis posted `/oauth/revoke` and `/account/sessions/logout-all` 12 times each from 5 client networks: 100 accepted sign-outs wrote 600 `hosting_oauth_signouts` rows (100 per wiki) for only 6 distinct (wiki, account, reason) combinations, all kept 30 days. Only the per-network rate limits applied, so rows grew with wikis x requests x networks. Expected: repeated sign-outs of one account coalesce.
- **Fix:** `bananawiki/hosting/signouts.py` `queue()` now deletes the identical older event (same wiki, account, wiki account and reason) in the same transaction before inserting the new one, so an account holds at most one event per wiki, wiki account and reason. A newer event reaches every session and link the older one did (wikis end sessions and drop links made at or before the event), and the table's AUTOINCREMENT ids put the replacing event beyond every wiki's cursor, so no sign-out is lost, also for a wiki that already fetched the older one. Skipping repeats within a poll interval, as suggested, was not used: it would leave a session started between two sign-outs signed in. docs/hosting.md says so; `test_signouts_are_paged` now pages events of many accounts, since it relied on repeated identical events.
- **Test:** `tests/security/test_m4.py::test_mw4_1_repeated_sign_outs_keep_one_event_per_wiki`, `tests/security/test_m4.py::test_mw4_1_a_replaced_event_still_ends_every_earlier_session`

### MW5-1 The portal's ioctl-only seccomp filter does not cover file_setattr(2), so the service account can still change XFS project assignments (full re-audit pass 7)
- **Severity:** Low. **Attacker:** code running as the hosting service account (compromised portal or maintenance process); needs Linux 6.17 or newer and XFS hard quotas.
- **Repro:** On kernel 7.0.0 with systemd 259 (whose `@system-service` allows `file_getattr`/`file_setattr`), after the real `install_filter()` the ioctls `FS_IOC_FSGETXATTR`/`FS_IOC_FSSETXATTR` failed with EPERM, but `file_getattr` (468) succeeded and `file_setattr` (469) wrote `FS_XFLAG_NODUMP` to the file; on XFS the same call sets the project ID and PROJINHERIT (code-traced, no XFS mount available). Expected: the fsxattr setters are refused however they are reached.
- **Fix:** `bananawiki/ops/hosting_entrypoint.py` `_instructions()` now also denies syscalls 468 and 469 with EPERM, and answers every native syscall from 470 on with ENOSYS, as an older kernel would, so a setter added by a later kernel fails closed until a release reviews it (470, `listns`, is in no systemd group `@system-service` includes, so nothing the portal uses is lost). x32 numbers stay refused before these checks. RUNTIME_AGENT.md and docs/hosting.md describe the wider filter.
- **Test:** `tests/security/test_m4.py::test_mw5_1_file_setattr_is_refused_under_the_filter`

### NW1-1 Requests with a method no route accepts skip the global per-client limit, and CSRF still parses their 16 MiB body (full re-audit pass 8)
- **Severity:** Low. **Attacker:** anonymous visitor.
- **Repro:** Test client with `GLOBAL_RATE_LIMIT=5`, CSRF on and a session holding a CSRF token: `GET /login` was refused with 429 after 5 requests, while 10 `PROPFIND /login` and a `FOO /no-such`, each with an 8 MiB multipart body, all answered 400 after `FormDataParser` parsed the whole body, and none was counted. Live gunicorn with the shipped configuration: once `GET` answered 429, `PROPFIND` with 8 MiB still answered 400 after the server read all 8,388,816 bytes. Expected: these requests count, and 429 comes before the body is read.
- **Fix:** `bananawiki/wiki/app.py` `_request_limits` treated a request without an endpoint as a static file and skipped the limiter. The custom-page catch-all routes ordinary 404s, so only methods no rule accepts had no endpoint. Requests that match no route now count against the global per-client limit, and only the `static` and `<feature>.static` endpoints stay exempt. This matches the portal's fix in GW2-2. docs/security.md says that unrouted requests count.
- **Test:** `tests/security/test_n1.py::test_nw1_1_unrouted_methods_count_against_the_global_limit`, `::test_nw1_1_static_files_stay_outside_the_global_limit`

### NW1-3 Successful sign-ins have no limit: each costs a scrypt hash (~30x a page view), a session row and an audit entry, also for pending accounts (full re-audit pass 8)
- **Severity:** Low. **Attacker:** any account holder, including an unapproved (pending) account from open sign-up.
- **Repro:** With the production hash (`scrypt:32768:8:1`), 100 `POST /login` with the right password for one account all answered 302. Each cost about 60 ms of CPU, against 2 to 5 ms for `GET /login`, and added one active `user_sessions` row and one `user.login` audit entry. A pending account signed in 40 times (302 to `/account-status`, 40 session rows). Every sign-in limit counted failures only. Expected: a budget for successful sign-ins per account and per network.
- **Fix:** `bananawiki/wiki/features/auth/signin.py` `verify` counts successful sign-ins in two more `rate_limit_hits` buckets, kept in the database and shared by all workers: `login:ok-account` allows 30 per account from every address (`MAX_SIGN_INS_PER_ACCOUNT`) and `login:ok-ip` allows 200 per client network, IPv6 /64 (`MAX_SIGN_INS_PER_IP`), both in the same 15-minute window. They are checked before the password is hashed, and a full bucket answers 429 with the existing "too many sign-in attempts" message. They are recorded only after the password was right, so failures, and strangers' in-flight attempts, never use them up and cannot lock the owner out. The limits are generous so that a class can sign in to a shared account or from behind one NAT address. Session rows and `user.login` entries are now bounded by these limits, so the audit entries are not coalesced. docs/security.md and UPGRADING.md describe the limit. The assertion in `tests/test_core_limits.py::test_successful_login_refunds_only_its_aggregate_reservations` that no limiter row remains after a success now leaves out the new `login:ok-*` rows; its point, that every failure reservation is refunded, is unchanged.
- **Test:** `tests/security/test_n1.py::test_nw1_3_successful_sign_ins_are_limited_per_account`, `::test_nw1_3_successful_sign_ins_are_limited_per_network`

### NW3-1 Pending, denied and suspended accounts see members-only announcements in the banner of /account-status (full re-audit pass 8)
- **Severity:** Low. **Attacker:** an unapproved account (anyone, where open sign-up with approval is on), a denied account or a suspended account.
- **Repro:** An administrator published the announcement "MembersOnlyZq door code 4711" with visibility `logged_in` and audience all. For pending, denied and suspended accounts, `GET /` answered 302 to `/account-status`, and `GET /account-status` answered 200 with the text rendered in the banner. `/announcements/<id>` answered 302, and the `logged_out` banner did not appear. Expected: refused accounts see what anonymous visitors see.
- **Fix:** `bananawiki/wiki/features/announcements/slots.py` `render_bar` treated any signed-in user as a member. A user that `auth.account_block` refuses is now served as an anonymous visitor: only `logged_out`/`both` announcements are shown, and the full text opens only in public mode. This matches the attention banners and the public views (MW2-1). Approved members are unchanged. docs/features.md says so.
- **Test:** `tests/security/test_n1.py::test_nw3_1_refused_accounts_see_no_members_only_announcements`, `::test_nw3_1_approved_members_keep_their_announcements`

### NW1-2 API calls refused for scope (and 429s) each write a year-long audit row without counting against the per-account API limit (full re-audit pass 8)
- **Severity:** Low. **Attacker:** holder of any API token (read-only, plain user with `api_access_enabled`).
- **Repro:** With the per-account limit at 3 and a plain user's read-only `pages` token, 25 x `POST /api/v1/pages` and 25 x `GET /api/v1/users/<600 x y>` all answered 403 and added nothing to `rate_limit_hits`, yet wrote 50 rows to `api_service__audit_log` (paths up to 500 characters, kept 365 days); 30 x `GET /pages` then gave 200 x 3 and 429 x 27, each 429 its own row. Only the per-network, per-worker global limit (300/min) bounded the log. Expected: refused calls use up the budget and calls over it are not each stored for a year.
- **Fix:** `authenticate()` in `bananawiki/wiki/features/api_service/api/__init__.py` now counts the call right after the token is resolved, before the API-access, account, scope, admin-only and feature checks. Past the limit, only the first 429 of each token per 60-second window is logged (a one-hit counter in `rate_limit_hits`, bucket `api_service_refused`, pruned by `api_service.prune` with the other API counters); later ones set `g.api_unrecorded` and `record_call` skips them. The log therefore grows by at most the account's limit plus one row per token a minute. Compatibility: a script that keeps calling outside its scope now gets 429 (noted in `UPGRADING.md` and `docs/api.md`).
- **Test:** `tests/security/test_n2.py::test_nw1_2_calls_refused_for_scope_count_against_the_limit`, `tests/security/test_n2.py::test_nw1_2_a_429_is_logged_again_in_the_next_window`

### NW1-4 Deeply nested JSON sent with an Idempotency-Key to POST /api/v1/users or /users/bulk answers 500 with a traceback (full re-audit pass 8)
- **Severity:** Low. **Attacker:** holder of an administrator's API token with the `users` scope (or a buggy client).
- **Repro:** `POST /api/v1/users` (and `/users/bulk`) with `Idempotency-Key` and the body `'['*200000 + ']'*200000` answered 500 `internal_error` every time, with a full `RecursionError` traceback logged from `prehash_passwords` (stdlib `json.loads`, catching only `TypeError`/`ValueError`). Without the key: 400. Expected: 400 either way, no traceback.
- **Fix:** `prehash_passwords` in `bananawiki/wiki/features/api_service/api/users.py` decodes with `core.json.loads` (bounded depth, `RecursionError` turned into `ValueError`), as every other API body does; an undecodable body leaves nothing to pre-hash and the view refuses it with 400.
- **Test:** `tests/security/test_n2.py::test_nw1_4_deep_json_with_idempotency_key_is_refused_not_a_500`, `tests/security/test_n2.py::test_nw1_4_idempotent_account_creation_still_uses_the_early_hash`

### NW1-5 Site settings changed through PUT /api/v1/settings leave no entry in the site audit log (full re-audit pass 8)
- **Severity:** Low. **Attacker:** plain administrator (accountability) with a `settings`-scope API token.
- **Repro:** `PUT /api/v1/settings {public_mode:1, open_signup:1, approval_required:0, bot_protection_enabled:0, page_builder_enabled:1, session_limit_enabled:1}` answered 200, changed five settings and switched Page Builder on (a `toggle="setting"` feature), and added no `audit_log` row; the web form records `settings.updated` and `docs/security.md` promises settings changes and feature switches are recorded. Expected: a `settings.updated` entry.
- **Fix:** `update_settings` in `bananawiki/wiki/features/api_service/api/settings_api.py` records `settings.updated` with `{"changed": <sorted changed keys>, "via": "api"}` after saving; nothing is recorded when nothing changed or the request is refused. Documented in `docs/api.md` (Settings).
- **Test:** `tests/security/test_n2.py::test_nw1_5_settings_changed_through_the_api_are_audited`, `tests/security/test_n2.py::test_nw1_5_unchanged_or_refused_settings_are_not_audited`

### NW2-1 Nesting categories a few hundred deep breaks the navigation of every page for every reader (category depth and count unbounded) (full re-audit pass 8)
- **Severity:** Medium. **Attacker:** editor (default `category.create`; `category.reorder` through `/category/<id>/move` or `PUT /api/v1/categories/<id>` too).
- **Repro:** An editor nested 400 categories through `POST /category/create`, each under the previous one (all 302), and filed a page at the bottom. Observed for anonymous public-mode visitors, users and administrators: every page answered 200 but logged `Slot sidebar.navigation in feature pages failed ... RecursionError` and had no navigation, and `/navigation` answered 500. Administrators and editors, who see empty categories, were hit below 300 levels even with no page in the chain. Nothing bounded the number of categories either: at 20,000 every page view cost an editor 0.55 s of CPU and 6.5 MB of HTML. Expected: a bounded depth, and a tree that renders.
- **Fix:** `bananawiki/wiki/features/pages/categories.py`: categories nest at most `MAX_DEPTH` = 32 levels. `create` checks the parent's chain. `set_parent` checks the new parent's chain plus the moved subtree's height (`_depth_below`). A wiki holds at most `MAX_CATEGORIES` = 10,000. Both refusals are translated `CategoryError`s (`wiki.error.category_too_deep`, `category_too_many`; the REST API answers 400 with those codes, and the Markdown import, which already stopped at 10 levels, reports them as problems). Trees deeper than that, left by older versions, still render: the new `display_parents` (iterative and memoised, and it ignores looping chains) attaches every category below level 32 to its ancestor at level 31. `navigation._category_nodes` and `categories.tree` build from it, so neither the Python build nor the recursive `_tree.html`/`_sidebar.html` templates go deeper than 32 levels, and those categories show at the 32nd level. `PUT /api/v1/categories/<id>` (`api/categories.py` `update_category`) applies the rename, move and sequential-navigation change in one `db.transaction()`, so a move refused for depth after the rename leaves the category as it was instead of renamed and not moved. The count bound alone did not bound a page view: every editor's page lists each category's full path in the move dialog (`#move-category`), and `/navigation` in the new-category form, so 9,984 categories as 312 chains of 32 levels with 100-character names made each editor or administrator page view 21.0 MB and 0.49 s of CPU and `/navigation` 33.1 MB and 0.78 s (4.7 MB and 16.0 MB for the same categories side by side). `categories.paths()` now shortens a path to `MAX_PATH` = 160 characters (`shorten_path`: the category's own name and its nearest parents after "… / ", and the walk up stops once nothing more fits); `/navigation` builds its parent options in Python from the same helper (`views._parent_choices`) instead of concatenating prefixes in a recursive macro. The create, edit, category-management and page-builder pickers and the lookup APIs' `category_name` use the shortened paths; search still matches every parent's name (`limit=None`). Measured again: 5.1 MB and 0.33 s per page view, 17.2 MB and 0.72 s for `/navigation`. `docs.spawn` (Admin → Documentation `POST /admin/spawn-docs` and the onboarding wizard) creates its category through `categories.create`, so on a full wiki with no current guide both answered 500: `spawn` now checks for room (`categories.ensure_room`, counting the guide it replaces) before it removes the current guide, the documentation page flashes the translated `category_too_many`, and the wizard finishes and shows it on the intro. The other callers of `categories.create` (the category form, the REST API and the bulk Markdown import) already turn it into a translated refusal. Documented in docs/features.md, docs/api.md and UPGRADING.md.
- **Test:** `tests/security/test_n3.py::test_nw2_1_categories_cannot_nest_past_the_depth_bound`, `::test_nw2_1_the_number_of_categories_is_bounded`, `::test_nw2_1_a_refused_api_move_leaves_the_category_unrenamed`, `::test_nw2_1_a_deep_old_tree_does_not_break_the_navigation`, `::test_nw2_1_a_deep_tree_costs_a_page_view_no_more_than_a_flat_one`, `::test_nw2_1_spawning_the_guide_at_the_category_bound_is_refused_not_a_500`

### NW2-2 Page-builder publish has no per-route limit and stores a ~0.4 MB history version on every request (full re-audit pass 8)
- **Severity:** Low. **Attacker:** editor (wherever `page_builder_access` admits editors).
- **Repro:** 90 back-to-back `POST /api/page/<slug>/builder/publish` requests in one limiter window, alternating two 12-block documents of about 19.5 KB a block: all 200 in 0.67 s, adding 90 history rows (42.2 MB, `builder_json` up to 234 KB). On the same account the 31st `POST /page/<slug>/edit` in the window answered 429. Expected: publish limited like the Markdown editor's save.
- **Fix:** `bananawiki/wiki/features/page_builder/routes.py` `publish` counts in a `page_builder.publish` bucket of `PUBLISHES_PER_MINUTE` = 30 per account (the Markdown editor's rate), checked right after `_buildable_page`. Beyond it the route answers 429 with `page_builder.error.publish_slow_down` ("Too many saves", en/it/de), which the editor shows as a status. Drafts are left alone: one row per page and user, CPU only. Documented in docs/features.md and UPGRADING.md.
- **Test:** `tests/security/test_n3.py::test_nw2_2_builder_publish_is_rate_limited`

### NW2-3 A plain administrator who impersonates a protection's controller edits the page and lifts the protection at once, credited to the controller and unaudited (full re-audit pass 8)
- **Severity:** Low. **Attacker:** plain administrator (impersonating a non-administrator needs no superuser).
- **Repro:** Editor `ctrl` protected `Guarded` through the route. The administrator's own edit and unprotect were refused. After `POST /admin/users/<ctrl>/impersonate`, `POST /page/guarded/edit` and `POST /page/guarded/protection action=unprotect` both answered 302. Observed: content 'impersonated edit', `protected_by` NULL, `last_edited_by` and the newest history row credited to `ctrl`, and only `user.impersonation_started` in the audit log. Expected: the protection binds the administrator, and their actions are logged as the impersonation prompt promises.
- **Fix:** `bananawiki/wiki/features/page_governance/protection.py`: the new `_impersonated(user)` is true when *user* is the account an administrator is impersonating in this request. `_describe` no longer counts that account as the controller, so `blocks`, `unprotect`, the reservation guard and the page's buttons treat the administrator like any administrator (unlock request and 72-hour delay). `protect` and `can_protect` refuse protecting in the impersonated account's name (`page_governance.protection.error.impersonating`). `bananawiki/wiki/features/pages/blueprint.py` adds an app-wide after-request hook that records every write request made while impersonating to the pages, page_builder, page_governance, attachments, difficulty_tags and assessments blueprints, and to the page-changing endpoints of other features (approving and denying proposed edits, the administrators' bulk Markdown import `page_export.bulk_import`, reachable by a superuser impersonating an administrator, bulk page and category deletion, restoring and purging pending deletions, forking a federated copy into a local page `federation.fork`, creating the built-in guide `site_admin.spawn_docs`, and scheduling or removing a page's timed deletion or visibility in `temporary_accounts`), as `page.impersonated_action`, with the administrator as actor and `as_user`, endpoint and status in the details, as MW3-1 does for chat (a fork's target is the copy, `federation_copy` `<wiki>/<page>`, since its `page_id` is the remote wiki's). Refused attempts are recorded too. A site migration import is refused while impersonating, so it needs no entry. Previews, highlighting, editing presence, private builder drafts and reading a Markdown file into the editor (`page_export.import_markdown`, which changes no page) are left out. Documented in docs/features.md, docs/security.md and UPGRADING.md.
- **Test:** `tests/security/test_n3.py::test_nw2_3_impersonating_the_controller_neither_edits_nor_unprotects`, `::test_nw2_3_page_changes_while_impersonating_are_audited`, `::test_nw2_3_page_changes_through_other_features_are_audited`, `::test_nw2_3_a_bulk_markdown_import_while_impersonating_is_audited`, `::test_nw2_3_a_federated_copy_forked_while_impersonating_is_audited`

### NW2-4 Quota requests can be filed and cancelled in a loop, so any plain user grows the database without bound (full re-audit pass 8)
- **Severity:** Low. **Attacker:** plain user (`contribution.propose` is a user default); editor for the reservation-quota form.
- **Repro:** With the features on and the default `quota_request_cooldown_hours` = 0, a plain user sent 120 cycles of `POST /my-contributions/quota-request` (2,000-character reason) and `/my-contributions/quota-request/cancel`, all 302. That left 120 rows, 240,000 bytes of reasons kept 90 days, and 120 attention events. An editor did the same 60 times on `/settings/reservation-quota` and left 60 rows. Expected: a bounded number of requests per account and time.
- **Fix:** `bananawiki/wiki/features/contributions/quota.py` and `page_governance/quota.py` `create_request`: an account files at most `MAX_REQUESTS_PER_DAY` = 10 requests in 24 hours, whatever became of them (cancelled and auto-approved ones count). The count comes from the requests table inside the filing transaction, so the bound holds across workers and restarts, unlike the in-memory limiter. Beyond it the user gets `...quota.error.too_many` (en/it/de). This bounds the rows, reasons and attention events to 10 a day per account and feature (about 1.8 MB of reasons over the 90-day retention). Documented in docs/features.md and UPGRADING.md.
- **Test:** `tests/security/test_n3.py::test_nw2_4_contribution_quota_requests_are_bounded_per_day`, `::test_nw2_4_reservation_quota_requests_are_bounded_per_day`

### NW3-2 Kanban ticket attachments (web, REST and board import) bypass the per-user daily upload quota (full re-audit pass 8)
- **Severity:** Low. **Attacker:** anyone with write access to one board (an invited plain user is enough) for uploads; anyone with `kanban.create` for imports; administrators too, since the quota has no exemption.
- **Repro:** with `upload_quota_per_day_count=1` and `upload_quota_per_day_bytes=1 MiB`, an editor who had used up the quota on page attachments (second `POST /api/page/<id>/attachments` answered 429) sent 5 x `POST /api/kanban/tickets/<id>/attachments` with 3 MiB files: all answered 201, 15 MiB were stored and `users.upload_window_count` stayed at 1. `extras.add_attachment` called `storage.save` with no quota check or counter update; the REST v1 route uses the same function, and a board import (`transfer._store_bundled`) unpacked up to 1 GiB of bundled files per import without counting them. Expected: the upload past the quota is refused with 429, as for page attachments.
- **Fix:** `kanban/extras.add_attachment` checks the count limit before reading the file (`pages.uploads.check_room`) and counts the stored file in the attachment's transaction (`count_stored`), removing the file when the byte limit refuses it. The board script's JSON route answers 429 for a quota refusal (`kanban/api._upload_error`); `POST /api/v1/kanban/tickets/<id>/attachments` already maps quota keys to 429 `upload_refused`. A board import checks that the files it would unpack fit the day's file count before unpacking (`transfer._store_bundled`), and counts the stored files inside the transaction that creates the board (`transfer._create`), so an import refused by the byte limit, or refused afterwards (board too large), removes its files and costs no quota; the refusal is flashed like other import errors. Compatibility: on wikis with a configured quota, Kanban uploads and imports past it are refused. docs/features.md lists Kanban attachments and board imports under the quota; UPGRADING.md notes the change.
- **Test:** `tests/security/test_n4.py::test_nw3_2_ticket_attachments_count_against_the_daily_quota`, `::test_nw3_2_ticket_attachments_obey_the_daily_byte_quota`, `::test_nw3_2_rest_ticket_attachments_share_the_quota`, `::test_nw3_2_board_import_counts_its_files_against_the_daily_quota`, `::test_nw3_2_board_import_obeys_the_byte_quota`, `::test_nw3_2_a_board_import_refused_after_storing_its_files_costs_no_quota`

### NW3-3 Unlimited stored attempts per account on multi-attempt assessments, and unpaged results lists (full re-audit pass 8)
- **Severity:** Low. **Attacker:** any signed-in user with `assessment.view` (a plain-user default) on a page whose assessment allows multiple attempts; the cost falls on the database and on the assessment's managers.
- **Repro:** an editor created a quiz with 100 free-text questions allowing multiple attempts; a plain user posted 5,000-character answers, 11 times in each of 3 simulated minutes: each minute gave 10 x 302 and one 429, storing 30 attempts and 3,000 answer rows (200,200 bytes of answers per attempt), about 14,400 attempts and 2.9 GB a day per account. Nothing capped or pruned attempts. With 20,000 attempts inserted, the manager's `GET /page/<slug>/assessment/results` returned 7.2 MB in 0.47 s and grew linearly (no paging); the manage page loaded every attempt only to count them. Expected: bounded storage per person and a paged results list.
- **Fix:** `assessments/service.submit` now prunes, in the same transaction, the person's attempts at that assessment beyond the latest `MAX_KEPT_ATTEMPTS` (20), always keeping their best (first highest-scoring) attempt, from which points are computed, so `total_points` does not change; answers go with their attempt (`ON DELETE CASCADE`). The results view is paged (`RESULTS_PER_PAGE` 100, `all_attempts(limit, offset)` with a stable order), the manage page counts with `count_attempts` (`COUNT(*)`), and the take page of a multi-attempt quiz tells the person which attempts are kept (new string in en, it and de). Storage per person and assessment is now bounded (21 attempts); the per-minute submission limit (10) is unchanged and no separate daily budget was added, since the cap already bounds what is stored. Compatibility: people who retake a quiz more than 20 times lose their oldest non-best attempts on their next submission; documented in docs/features.md and UPGRADING.md.
- **Test:** `tests/security/test_n4.py::test_nw3_3_old_attempts_are_pruned_keeping_the_best`, `::test_nw3_3_results_are_paged_and_the_manage_page_counts`

### NW3-4 Canvas sibling of MW3-3: REST canvas creation has no per-route limit and the /canvas list is unpaged (full re-audit pass 8)
- **Severity:** Medium. **Attacker:** an account that may create canvases (editors under `canvas_write_access=editor`, everyone under `all` or open access, administrators by default) holding an API token with the `canvas` scope; afterwards every viewer of `/canvas` pays, including anonymous visitors when public canvas access is on.
- **Repro:** with `canvas_access=all`, `canvas_write_access=editor`, the API on and an editor token with the `canvas` scope, 12 x `POST /canvas/create` gave 10 x 302 and 2 x 429, and in the same minute 65 x `POST /api/v1/canvas` gave 60 x 201 before the API's global limit answered 429: 70 canvases a minute from one account (about 100,000 a day), in two separate budgets. `GET /canvas` called `access.visible_layouts(user)` with no limit, then `apply_order` over every row, and rendered them all: a plain reader's view was 8.2 MB in 0.33 s at 10,000 canvases and 16.4 MB in about 0.65 s at 20,000, growing linearly. Expected: one shared creation budget and a paged list, as for Kanban (MW3-3).
- **Fix:** `canvas/service.create_allowed` (limiter key `canvas:create:<account>`, `CREATES_PER_MINUTE` 10) is the one creation budget for `POST /canvas/create` and `POST /api/v1/canvas`; past it the API answers 429 `rate_limited` with `Retry-After: 60`, as `kanban_api.create_board` does. Canvas import keeps its own 10 a minute, as Kanban import does. `/canvas` is paged (`LIST_PAGE_SIZE` 100, `forms.pagination`): `canvas/access.ordered_ids` reads only the four columns the access check needs, sorted in SQL in the list's order (saved order of the user, or the shared one under open access, first; then most recently changed), and `access.layouts_in_order` reads in full only the page's canvases. Reordering is offered on the first page only, and `POST /api/canvas/layout-order` stores the canvases sent, then the canvases already in the saved order that the client did not send, in that order (visible ones for a personal order; under open access every shared row, hidden ones in their places); canvases never placed are not stored and keep sorting after the placed ones, most recently changed first, so a reorder on page 1 keeps the order of later pages. A first version appended every visible canvas instead, which review found turned each save by any reader into one row per visible canvas (40,000 rows and 0.17-0.18 s per save at 40,000 public canvases, 30 saves a minute per account) and grew `canvas_user_layout_order` to users x canvases; after the change three readers saving `[1]` there left 3 rows at about 0.10 s a save (the access check that reads the list's ids, the same as viewing `/canvas`). The saved order is also capped at `canvas/routes.ORDER_LIMIT` (5000, the most one save may send, which it was before), so an order grown over several saves cannot bring the per-save cost back; an order longer than that (only possible from before the limit, under open access) keeps its first 5000 on its next save. Membership of the sent, visible and saved ids is checked in sets (a list check made each save quadratic: about 2 s at 40,000 canvases in review). The page builder's embed picker no longer reads every canvas per search: `canvas/access.iter_visible_layouts` yields the visible canvases from batches of 500 (`visible_layouts` builds its lists from it, for `GET /api/v1/canvas` too) and the picker stops once it has its 20 results. Each batch continues after the last canvas of the one before (keyset paging on `updated_at, id`) and walks a new index, `idx_canvas_layouts_updated` (schema version 16, `migrations/v16_canvas_list_index.py`), so a search that matches few or no canvases reads each canvas once, as the single SELECT before did. A first version paged with `LIMIT 500 OFFSET n` over the unindexed order, which review found sorted the whole table again for every batch: at 40,000 public canvases with 2,000-character descriptions, a picker search by an editor that matched nothing took 14 s, against 0.23 s before; after the change it takes 0.17 s, and 0.005 s for a search the most recently changed canvases answer. The Kanban half of the picker still reads every visible board per search, as it did before (only accounts that `page_builder_access` allows, editors by default, at 120 lookups a minute). Compatibility: the HTML canvas list becomes paged; the JSON order endpoint accepts the same body; the first start migrates the database from schema 15 to 16 (the index), and afterwards 1.7.3 cannot open it (it refuses a newer schema), so going back to 1.7.3 needs a restore: UPGRADING.md and the CHANGELOG's Unreleased section say so and ask operators who may want to go back to back up first (`sudo bananawiki backup` on a managed server), as the notes of every earlier migration do. docs/features.md, docs/api.md and UPGRADING.md are updated.
- **Test:** `tests/security/test_n4.py::test_nw3_4_rest_canvas_creation_shares_the_per_account_limit`, `::test_nw3_4_canvas_list_is_paged`, `::test_nw3_4_reordering_the_first_page_keeps_the_order_of_later_pages`, `::test_nw3_4_saving_a_large_shared_order_takes_linear_time`, `::test_nw3_4_saving_the_order_stores_only_placed_canvases`, `::test_nw3_4_embed_picker_stops_reading_canvases_once_it_has_its_results`, `::test_nw3_4_embed_picker_search_costs_one_pass_over_the_canvases`, `::test_nw3_4_upgrade_notes_say_1_7_3_cannot_open_the_migrated_database`; `tests/test_wiki_migrations_frozen.py` pins version 16

### NW4-1 Create-then-terminate loops are unbounded: terminated wikis keep their data (and XFS reservation) for 30 days but count against no limit (full re-audit pass 8)
- **Severity:** Medium. **Attacker:** any approved hosting user, including a new account on probation (wiki limit 1).
- **Repro:** Portal test fixtures, with FakeRuntime reporting each tenant at its plan. A probation account (limit 1) created and terminated wikis in a loop over the owner's HTTP routes, using a few client addresses to stay under the 5-a-minute rate limit. Observed: every creation succeeded, and 9 terminated wikis kept their data directories (about 3.6 GB) for 30 days. `platform_usage_mb` stayed at 0, so the platform storage limit never refused anyone, and each creation scheduled a certificate. Expected: one account keeps no more retained tenant data than its limit, and its creations are bounded.
- **Fix:** In `bananawiki/hosting/instances.py`, `check_account_capacity` now counts a wiki its owner terminated while its data is kept (`holds_place`: reason `manual`, `data_retained_until` in the future, countdown not paused by an administrator). It has its own message (`hosting.instances.account_limit_retained`). New `discard_retained` lets the owner delete that data at once. It claims the row first, then destroys the data, and puts the claim back if that fails. The row stays but can no longer be restored. Owners reach it through the new route `POST /instances/<id>/delete-data` (`routes/dashboard.py`) and a **Delete data now** card on the wiki's page. `dashboard_list` and `instance_for` show such wikis to their owner even when owner downloads are off. `_check_capacity` also limits creations to `CREATIONS_PER_PLACE_PER_DAY` (2) times the account limit per 24 hours (administrators are exempt). It counts them by their creator: the account's `instance.created` entries in the moderation log (new `events.subjects_since`, which reads only the last day's ids), together with its own rows of the day. A first version counted rows by their current owner, so a wiki given to a second account, which then terminated it and deleted its data, no longer counted, and two cooperating accounts could create without limit. `platform_usage_mb` now counts terminated wikis that still keep data. The platform's wiki count and the new accounts' share (`_on_platform`) now also count terminated wikis whose data is kept, unpaused, after their owner terminated them or deleted their account (reason `account_deleted`). Before that, signing up, creating a wiki and deleting the account in a loop kept a tenant directory each time that no count limit saw: the account was gone, and both platform counts looked only at running and stopped wikis. Wikis that expired or that an administrator terminated do not count: expiry bounds them already, and an administrator decided. The command line's `instance terminate` passed reason `manual`, an owner's reason, so its wikis did hold the owner's place. It now passes `admin`, as the administration pages do. **Accepted risk** (what remains): with open sign-up, each new account may still create 2 wikis a day, and so order 2 certificates, before it deletes itself. That is bounded only by `HOSTING_SIGNUPS_PER_IP_PER_DAY` (10 per address), about 20 certificates a day per address. Its kept data is bounded by the new accounts' share. The approval and invite sign-up modes, already recommended for public platforms, remove this. Translations are added in en/it/de, including an event label. Docs updated: `docs/hosting.md`, `docs/configuration.md`, `UPGRADING.md`. Upgrade note: kept data of terminated wikis now takes platform places, so a platform near its *Maximum wikis* may refuse new wikis until those grace periods end. `tests/hosting/test_hosting_instances.py::test_stop_restart_and_terminate` now expects the owner to see the terminated wiki until its data is deleted.
- **Test:** `tests/security/test_n5.py::test_nw4_1_a_terminated_wiki_holds_its_place_until_its_owner_deletes_its_data`, `tests/security/test_n5.py::test_nw4_1_a_paused_countdown_keeps_the_data_from_its_owner`, `tests/security/test_n5.py::test_nw4_1_creating_and_deleting_wikis_in_a_loop_stops_at_the_daily_budget`, `tests/security/test_n5.py::test_nw4_1_wikis_given_away_still_count_towards_their_creators_day`, `tests/security/test_n5.py::test_nw4_1_wikis_kept_after_their_account_was_deleted_fill_the_newcomers_share`, `tests/security/test_n5.py::test_nw4_1_a_wiki_terminated_on_the_command_line_frees_its_owners_place`, `tests/security/test_n5.py::test_nw4_1_platform_storage_counts_the_data_of_terminated_wikis`

### NW4-2 Wiki-host fallback proxy has no rate limit and its slot queue parks a portal worker thread, so one slow wiki lets a single client exhaust every portal thread (full re-audit pass 8)
- **Severity:** Low (reported Medium). **Attacker:** an anonymous visitor sending requests to a wiki host that reaches the portal fallback, where that wiki's upstream is slow.
- **Repro:** A real gunicorn portal ran the shipped `hosting/gunicorn.conf.py`, with a wiki whose upstream sleeps 30 s per request. 40 connections sent requests to that wiki's host. Observed: 56 "busy" 503 answers, each after exactly 10.0 s (threads parked in `slot.acquire(timeout=QUEUE_SECONDS)`), and no 429 ever. Every probe of the portal's own `/login`, and most probes of `/health`, timed out for the whole run. Expected: a request that cannot get a slot does not hold a worker thread, and the proxy path is rate-limited per client.
- **Fix:** In `bananawiki/hosting/wikihosts.py`, `_Slot.acquire` first tries to take a slot at once. It waits up to `QUEUE_SECONDS` only after taking one of `WAITING_PLACES` (4 per process) and `WAITING_PER_WIKI` (3 per wiki). A request that finds no waiting place gets the "busy" page at once. `hosting/gunicorn.conf.py` runs one more thread per waiting place (`HOSTING_THREADS + PORTAL_PLACES + WAITING_PLACES`), so waiting requests never take the `HOSTING_THREADS`. Proxied requests still run on them: at most `HOSTING_THREADS` - 1 in all and 2 per wiki by default. Only one thread (besides the places for large responses) is sure to stay for the portal's pages. `_thread_count` is capped at 32 like Gunicorn's setting, so the pool always stays below the real thread count. `WikiHosts.__call__` counts every wiki-host request against `RATE_LIMIT` (600 a minute per client network and process, its own limiter bucket) before any lookup. Past that limit it answers with a 429 status page (`Retry-After: 60`, new `limited` state, translated in en/it/de). Because busy and limited answers now came at once, the same flood (40 connections to a wiki with a 30 s upstream) brought both workers to `max_requests` (5000) within about 10 s. Gunicorn then stops accepting connections and waits for the requests in progress, up to `graceful_timeout` (120 s), before it starts a replacement. The live portal stopped answering `/health` for long stretches, and some busy answers took 24.7 s. Now `post_worker_init` calls `wikihosts.watch`, whose thread drains the worker's proxy once the worker is no longer alive: no new slots, waits end at once, the upstream sockets of proxied requests are shut down, and the response body loop stops. In-flight proxied requests get the 503 status page, or a cut-off body, while the worker is replaced. Live re-run on the fixed branch: 20 worker restarts in 45 s, and every `/health` probe answered 200 within 0.2 s. Docs updated: `docs/hosting.md` (now also saying which threads proxied requests may hold), `docs/configuration.md`, `UPGRADING.md`. The thread-count assertions in `tests/security/test_iw2.py` and `tests/security/test_jw1b.py` now include the waiting threads.
- **Test:** `tests/security/test_n5.py::test_nw4_2_a_request_for_a_slow_wiki_with_no_waiting_place_is_turned_away_at_once`, `tests/security/test_n5.py::test_nw4_2_waiting_places_are_bounded_per_wiki_and_leave_the_others_a_slot`, `tests/security/test_n5.py::test_nw4_2_the_portal_runs_a_thread_for_each_waiting_place`, `tests/security/test_n5.py::test_nw4_2_wiki_host_requests_are_limited_per_network`, `tests/security/test_n5.py::test_nw4_2_a_retiring_worker_cuts_off_proxied_and_waiting_requests_at_once`, `tests/security/test_n5.py::test_nw4_2_a_retiring_worker_cuts_off_a_trickling_wiki_response`, `tests/security/test_n5.py::test_nw4_2_the_portal_drains_the_proxy_when_gunicorn_retires_a_worker`

### OW1-1 A platform sign-in left at the two-step code across a change of owner completes after it (full re-audit pass 9)
- **Severity:** Low. **Attacker:** The previous owner of a hosted wiki whose connected administrator account has two-step verification on.
- **Repro:** With the repo's platform fixture, the owner connected `admin_team` (and `oldowner`), turned on two-step verification and signed in through the portal up to `/login/two-step`. The instance was transferred (`previous_owner=none`), with and without the sign-out job running in between (the job dropped the link). Posting the TOTP or a recovery code then answered 302 to `/`, the session (`platform_oauth+totp`) survived further job runs and `/admin/users` answered 200. Expected: the pending sign-in fails. `confirm_link` ran only in the callback; `finish_second_step` rechecked only the age, the account and its fingerprint.
- **Fix:** The pending sign-in of `signin.finish` now carries the portal account (`platform_account`, passed by every platform `_finish` in `routes_oauth.py` and by the merge code step). `signin.finish_second_step` requires, for a `platform_oauth` sign-in, that `linked_user(account)` is still that account and that `platform_oauth.confirm_link` confirms it (a portal error fails the sign-in too) before the session starts; otherwise the browser is sent back to sign in. A pending platform sign-in from before the upgrade (no account) fails once. docs/hosting.md says so.
- **Test:** `tests/security/test_o1.py::test_ow1_1_pending_code_after_change_of_owner_does_not_sign_in`, `tests/security/test_o1.py::test_ow1_1_pending_code_completes_while_the_link_holds`

### OW1-2 REST API administration (API switch and limits, per-account API access, webhooks and their secrets) leaves no site audit entry (full re-audit pass 9)
- **Severity:** Low. **Attacker:** Plain administrator (accountability).
- **Repro:** As a plain admin: POST `/admin/api-service/settings` (302, values stored), POST `/admin/api-service/users/<id>/toggle-access` (302), POST `/admin/api-service/webhooks` (201), `.../rotate` (200), POST `/api/v1/admin/webhooks` (201), `.../rotate-secret` (200) and PUT `/api/v1/admin/users/<id>/api-access` (200) added no `audit_log` rows, while PUT `/api/v1/settings` recorded `settings.updated` (control). Expected: an entry for each.
- **Fix:** New `audit.record_settings(previous, values, via=...)` records `settings.updated` with the keys that changed; the REST API settings form uses it (`via: api_service`). The three places that set `api_access_enabled` (web toggle, `PUT /admin/users/<id>/api-access`, `PUT /users/<id>`) go through `tokens.set_api_access`, which records `user.api_access_changed` {username, enabled} when the value changes. `webhooks.create/update/rotate_secret/delete` record `api_service.webhook_created/_updated/_secret_rotated/_deleted` for both the web page and the API, with only the host of the address (webhook addresses often carry a credential). Labels in en/it/de; docs/security.md lists them.
- **Test:** `tests/security/test_o1.py::test_ow1_2_api_settings_form_is_recorded`, `tests/security/test_o1.py::test_ow1_2_api_access_changes_are_recorded`, `tests/security/test_o1.py::test_ow1_2_webhook_lifecycle_is_recorded_without_the_address`

### OW2-2 The W2-9 docs bypass of the deletion grace period can be met by any page through user-chosen edit summaries (full re-audit pass 9)
- **Severity:** Low. **Attacker:** Editor holding history.delete, history.transfer and page.delete (all non-default), with docs_bypass_deletion_slowdown on (the default).
- **Repro:** An editor with those permissions saved an edit of an ordinary page with the summary "Built-in documentation", removed its attribution (`/history/<id>/deattribute`), deleted the older entries (`/history/<id>/delete`), moved the page into the documentation category and deleted it: it was removed at once (no `pending_deletion`, nothing to restore). Expected: `pending_deletion=1`. `bypasses()` trusted the first remaining history entry.
- **Fix:** Schema 17 (`migrations/v17_builtin_doc_pages.py`) adds `builtin_doc_pages (page_id ... REFERENCES pages(id) ON DELETE CASCADE)`; `auth/docs.spawn` lists each page it writes there, and `deletion_slowdown.service.bypasses` checks `docs.is_builtin` instead of the history. The migration lists once the pages that meet the old rule in the current documentation category, so a guide installed earlier keeps skipping the grace period (a page forged before the upgrade would be carried over too; the attack needs three non-default permissions). Frozen-migration test, migration docstring, UPGRADING.md (schema 15 to 17, a *Deletion slowdown* bullet), CHANGELOG.md and docs/features.md updated.
- **Test:** `tests/security/test_o1.py::test_ow2_2_a_page_with_a_forged_installer_history_waits`, `tests/security/test_o1.py::test_ow2_2_migration_lists_the_installed_guide_only`

### OW3-2 The settings pages of the collaboration features leave no audit entry, including a DM retention of one day that then erases every member's private messages (full re-audit pass 9)
- **Severity:** Low. **Attacker:** Plain administrator (accountability).
- **Repro:** As a plain admin, POST `/admin/chats/settings` (cleanup on, DM retention 1 day), `/kanban/settings` (open and public), `/admin/canvas` (open and public) and `/admin/assessments` each answered 302 and changed the settings with 0 audit rows; a backdated DM was then deleted by `retention.run_retention(force=True)` (`{'messages': 1}`) with 0 audit rows (only a log line). Expected: a `settings.updated` entry per form and an entry for a retention run that deleted messages.
- **Fix:** Every feature settings form now records `settings.updated` {changed, via: <feature>} through `audit.record_settings` (only when something changed): chat, kanban, canvas, read aloud, contributions, assessments, page builder, page governance, custom pages and notifications (key names only, never values such as the GPU token or SMTP password). `chat.retention.run_retention` records `chat.retention_applied` with its summary and the rules applied when it deleted anything. Labels in en/it/de; docs/security.md and docs/features.md updated.
- **Test:** `tests/security/test_o1.py::test_ow3_2_feature_settings_forms_are_recorded`, `tests/security/test_o1.py::test_ow3_2_a_retention_run_that_deletes_messages_is_recorded`
### OW2-1 Every page title change, rename, deletion or restore re-parses every canvas holding "page_slug" (full re-audit pass 9)
- **Severity:** Low (Medium where non-administrators may create canvases). **Attacker:** account that may create canvases (canvas_write_access editor/all, open access, `canvas.create`, administrators); every editor's page edits pay.
- **Repro:** an editor saved canvases holding one wiki-page node with an unresolvable `page_slug` (the key also survives in node metadata); another editor's `POST /page/<slug>/edit/title` then parsed every such canvas with `model.clean_document`, each in its own `BEGIN IMMEDIATE`: about 10-12 ms per 1-2 MB canvas, 3.0-3.7 s of CPU per title change with 300 of them (0.2-0.4 s without the marker). Expected: a page event touches only the canvases that link that page.
- **Fix:** schema version 18 (`bananawiki/wiki/migrations/v18_canvas_page_slugs.py`) adds `canvas__page_slugs`, the slugs that each canvas's wiki-page nodes (type `wiki_page`, or 1.4's `page`) store, kept by triggers on every insert, update of `data` and delete of `canvas__layouts` (so any writer, raw SQL included, keeps it in step) and filled for existing canvases by the migration (about a second per gigabyte of canvases). `_rewrite_page_nodes` (`bananawiki/wiki/features/canvas/service.py`) takes the slugs of the event and `_canvases_with_page_slugs` reads the candidates from that index; only canvases with a wiki-page node storing one of those slugs are parsed and rewritten, and a canvas that only mentions a slug (text, metadata, another node type) or holds an unresolvable slug is not read at all. A first version of this fix filtered with two `instr()` checks and SQLite's `json_each`, which review found a writer still satisfied by putting the page's slug in a text node: each such canvas was fully parsed by SQLite on every event for that page, about 3.5 ms per 2 MB canvas (1.75 s of CPU for a title change with 300 of them, inside the rename's write transaction for `page.renamed`). With the index the same 300 planted canvases cost 0.005 s per title change, as without them. The cost moves to saves: the trigger reads the document once more as it is written (about 10 ms for a 5 MB canvas, beside the Python parse a save already does). A canvas whose wiki-page node names a page its writer cannot read keeps that slug and is rewritten once by the page's next event (it then holds the page id and leaves the index); re-planting it costs the writer a rate-limited save each time. Nodes that store a page id are read by id when shown, as before; a 1.6.0 node holding a page id and a slug other than the page's is cleaned on the canvas's next save instead of on the page event. Compatibility: the first start migrates the wiki database to schema 18; UPGRADING.md and the CHANGELOG say so (1.7.3 cannot open it afterwards, as for 16). A site archive imported at schema 18 brings its own index rows; stale rows there only make a page event parse a canvas needlessly or miss following a legacy slug.
- **Test:** `tests/security/test_o2.py::test_ow2_1_page_title_change_skips_canvases_that_only_mention_page_slug`, `tests/security/test_o2.py::test_ow2_1_page_events_do_not_read_canvases_that_only_mention_the_slug`, `tests/security/test_o2.py::test_ow2_1_slug_index_follows_every_write_of_a_canvas`, `tests/security/test_o2.py::test_ow2_1_upgrade_lists_the_slugs_of_existing_canvases`, `tests/security/test_o2.py::test_ow2_1_renames_still_follow_escaped_legacy_slugs`; `tests/test_wiki_migrations_frozen.py` pins version 17

### OW3-4 Canvas history entry pages have no per-route limit and defeat the render cache (full re-audit pass 9)
- **Severity:** Low. **Attacker:** anyone with edit access to one canvas plants the entries; any viewer pays, anonymous visitors included for a public canvas.
- **Repro:** an editor alternated `POST /canvas/<slug>/data` and `POST /canvas/<slug>/revert/<first>` within the revert limit, leaving about 60 distinct 0.5-0.6 MB history entries; a plain reader viewing each `GET /canvas/<slug>/history/<id>` twice got 120 x 200, no 429, 0.9-1.4 s of CPU per view in both rounds, with the 32 MiB render cache full and never hit. Expected: a per-route limit like the other canvas reads and a bounded cost per view.
- **Fix:** `bananawiki/wiki/features/canvas/routes.py`: `history_entry` is limited to 30 views a minute per account or address (`_limit("history-entry", 30)`) and the history list to 60, and a snapshot is presented under `RenderBudget(present.HISTORY_RENDER_LIMIT)` (64K characters, new in `present.py`; `present_document` takes a `budget`); notes past it are shown as plain text, as on the live canvas past its 512K budget. docs/features.md states the limits.
- **Test:** `tests/security/test_o2.py::test_ow3_4_history_entry_renders_a_bounded_amount_of_the_snapshot`, `tests/security/test_o2.py::test_ow3_4_history_entry_views_are_rate_limited`
### OW3-1 The Kanban history entry page rendered every ticket description of the snapshot as Markdown, uncached and unlimited (full re-audit pass 9)
- **Severity:** Medium. **Attacker:** anyone with kanban.create (editors under `kanban_write_access=editor`) or write access to one board plants it; every viewer of the board pays, anonymous visitors included for a public board.
- **Repro:** an editor imported a JSON board of 4,400 tickets, each with a different 1,600-character Markdown description. A plain user loaded `GET /kanban/<id>/history/1` four times: 200, 10 MB of HTML and 6.9 s of CPU each time, with no saving on repeats (descriptions under `RENDER_CACHE_MIN_CHARS` are never cached) and no per-route limit. The board page cost 0.09 s. Expected: bounded work per view and a rate limit.
- **Fix:** `bananawiki/wiki/features/kanban/history.py`: the new `describe_tickets()` renders snapshot descriptions in board order, sharing one highlighting allowance, until `MAX_SNAPSHOT_RENDER_CHARS` (200,000) source characters or `MAX_SNAPSHOT_HTML_CHARS` (2 MiB) of HTML are used. After that, descriptions are shown as typed (`plain_html`, escaped). `kanban/views.py` calls it for `history_entry`, and the template prints the prepared HTML instead of `|markdown`. `board_history` and `history_entry` share a 30-a-minute `history-view` limit. `kanban/guard.py`: Kanban's per-account limits now count signed-out visitors per client network (`client_network()`), not in one `anonymous` bucket that one visitor could use up for everyone. The same repro now costs 0.38 s per view. The user guide (en, it) says that only the first descriptions of a large snapshot are formatted.
- **Test:** `tests/security/test_o3.py::test_ow3_1_history_entry_renders_descriptions_within_a_budget`, `tests/security/test_o3.py::test_ow3_1_history_pages_are_rate_limited`

### OW3-3 Kanban, canvas and profile changes made while impersonating a member were credited to the member with no audit entry (full re-audit pass 9)
- **Severity:** Low. **Attacker:** plain administrator (impersonating a non-administrator needs no superuser).
- **Repro:** a plain admin impersonated bob and sent `POST /api/kanban/tickets/<id>/comments` "I approve the payment. -- bob" (201), `PUT /api/kanban/tickets/<id>` (200) and `POST /settings/profile` (302). The comment's `user_id` was bob's, and the audit log had no entry after `user.impersonation_started`. Canvas ops behaved the same way. Expected: an `*.impersonated_action` entry with the administrator as actor, as MW3-1 and NW2-3 record for chat and pages.
- **Fix:** after-request hooks record each write request made while impersonating (refused attempts too), with the administrator as actor and `as_user`, `endpoint` and `status` in the details. `kanban/views.py` covers the kanban blueprint (browser and JSON routes) as `kanban.impersonated_action`, targeting the board, column, ticket, comment, attachment or checklist item. `canvas/routes.py` covers canvas as `canvas.impersonated_action`, targeting the canvas slug. `users/routes.py` covers the users and user_profiles blueprints (profile, avatar, profile fields, publishing, username, merges, deletion, ownership) as `user.impersonated_action`, targeting the member. Left out: the member's own board-list order (`kanban.board_order`), canvas node previews and the list order (`canvas.render_node`, `canvas.save_order`), and display, accessibility and language preferences. The comment and edits stay credited to the member, as for chat and pages; the audit entry is what identifies the administrator. Audit labels are in en, it and de, and docs/security.md and UPGRADING.md are updated.
- **Test:** `tests/security/test_o3.py::test_ow3_3_kanban_and_profile_changes_while_impersonating_are_audited`, `tests/security/test_o3.py::test_ow3_3_canvas_changes_while_impersonating_are_audited`

### OW3-5 Every Kanban board-order save wrote one row per board the user could see (full re-audit pass 9)
- **Severity:** Low. **Attacker:** any account that can open the board list.
- **Repro:** with 20,000 public boards and `kanban_access=all`, five plain users each sent `POST /api/kanban/board-order {"board_ids": [<one id>]}` three times. Each save returned 200 and cost 0.09-0.13 s in the writer transaction, and `kanban_user_board_order` grew by 20,000 rows per user (100,000 in all) although each user placed one board. Expected: rows only for the boards placed, and a per-save cost that does not grow with the number of boards.
- **Fix:** `kanban/service.py` `save_board_order`: a personal order now stores the visible active boards sent (checked against a set), then the boards already in the user's order that are still visible, at most `MAX_ORDERED_BOARDS` (5,000), as the canvas order does since NW3-4. Boards never placed still follow, newest first (`access.ordered_boards`). The new `access.visible_board_ids()` reads only ids in one query. The shared order used with open access keeps its old behaviour: one row set for the whole wiki, not one per user. Bloated orders from earlier versions shrink to the bound at their next save. The JSON body is unchanged.
- **Test:** `tests/security/test_o3.py::test_ow3_5_board_order_stores_only_the_boards_placed`
### OW4-1 Portal feature requests, merge requests and transfer offers could be filed and cancelled in a loop, and the wiki pages listed every feature request unpaged (full re-audit pass 9)
- **Severity:** Low. **Attacker:** any approved hosting user who owns a wiki (feature requests, transfer offers); any approved account (merge requests).
- **Repro:** An owner sent 30-60 cycles of `POST /instances/<id>/features/public_access/requests` (2,000-character reason) and `/instances/<id>/feature-requests/<rid>/cancel`, all 302: each left an `instance_feature_requests` row, a `hosting_events` row with the reason and an attention event, and `/admin/instances/<id>` printed every reason (149 KB after 30 cycles; 29.6 MB at one network's daily route limit). 20-40 cycles of `/account/merge-request` (4,000-character reason) and `/account/merge/cancel/<id>` left as many rows, and 20-40 `POST /instances/<id>/transfer` left as many offers, each emailing the recipient. Only the per-network route limits applied. Expected: a bounded number of requests per account or wiki and day, as NW2-4 gave the wiki.
- **Fix:** `bananawiki/hosting/features.py` `request`, `merges.py` `request` and `collaborators.py` `start_transfer` count the rows of the last 24 hours, whatever became of them (cancelled and auto-approved included), inside the inserting transaction and refuse past 10 (`MAX_REQUESTS_PER_DAY` per wiki and feature, `MAX_REQUESTS_PER_DAY` per requesting account, `MAX_TRANSFERS_PER_DAY` per wiki) with `hosting.features.too_many`, `hosting.merges.too_many` and `hosting.transfers.too_many` (en/it/de). The count comes from the tables, so the bound holds across workers and restarts. `features.history` lists the latest `HISTORY_LIMIT` = 20 requests plus every waiting one (the pages derive the waiting requests from it), on the owner's and the administrator's wiki pages; the custom domain page asks it for its 5 rows of that feature directly. Compatible; only adds error messages. Documented in docs/hosting.md and UPGRADING.md.
- **Test:** `tests/security/test_o4.py::test_ow4_1_feature_requests_are_bounded_per_day`, `::test_ow4_1_feature_history_is_paged_on_the_wiki_pages`, `::test_ow4_1_merge_requests_are_bounded_per_day`, `::test_ow4_1_transfer_offers_are_bounded_per_day`

### OW4-2 Successful portal sign-ins had no limit, each costing a scrypt hash and a 7-day session, pending accounts included (full re-audit pass 9)
- **Severity:** Low. **Attacker:** any account holder, including an unapproved (pending) account.
- **Repro:** With the production hash (scrypt:32768:8:1), 80-120 `POST /login` with the right password for one account from one address all answered 302, each costing about 58 ms of CPU (GET /login: 2 ms) and leaving an active `hosting_account_sessions` row kept 7 days; a pending account did the same 35-40 times (302 to `/activation-pending`). `login()` reserved only failure buckets and released them on success. Expected: as in the wiki since NW1-3, a budget for successful sign-ins per account and per network.
- **Fix:** `bananawiki/hosting/routes/auth.py` `login` checks `login-ok-account` (`LOGIN_MAX_SIGN_INS_PER_ACCOUNT` = 30, keyed on the account found, `id:<id>`) and `login-ok-ip` (`LOGIN_MAX_SIGN_INS_PER_IP` = 200, the client network) over the 15-minute window before the password is checked, answering 429 `hosting.login.too_many`, and records both only after the password matched, so strangers' failures never use them up. A two-step sign-in is counted at its password step, so completions of `/login/mfa` are bounded too. Documented in docs/hosting.md and UPGRADING.md.
- **Test:** `tests/security/test_o4.py::test_ow4_2_successful_sign_ins_are_bounded_per_account`, `::test_ow4_2_successful_sign_ins_are_bounded_per_network`, `::test_ow4_2_failed_sign_ins_do_not_use_up_the_success_budget`

### PW1-1 GET and HEAD requests keep the 500 MiB body ceiling; GET /settings/merge-request parses and spools the whole body (full re-audit pass 10)
- **Severity:** Low. **Attacker:** any signed-in plain user.
- **Repro:** Plain user bob, signed in, sent a multipart file part larger than `BW_MAX_CONTENT_LENGTH_BYTES` (16 MiB by default). POST /settings/merge-request answered 413. GET /settings/merge-request and GET /settings/merge-request/from/<name> with the same body answered 200, and Werkzeug had parsed the part into a spooled temporary file (200 MiB in one run). Safe methods kept `MAX_CONTENT_LENGTH`, which is 500 MiB with the default site-import size. Expected: 413 for the GET too.
- **Fix:** `bananawiki/wiki/app.py` `_request_limits` no longer returns early for safe methods. GET and HEAD always get `min(MAX_CONTENT_LENGTH, BW_MAX_CONTENT_LENGTH_BYTES)`, and only unsafe `@auth.large_body` views called by a live session or bearer token keep the larger ceiling. This matches the portal's fix in GW2-2. `bananawiki/wiki/features/users/merge_routes.py` `merge_request` now reads `request.form` only on POST. docs/security.md says a GET or HEAD body is never allowed more than `BW_MAX_CONTENT_LENGTH_BYTES`. No GET view expects a body, so nothing is lost for compatibility.
- **Test:** `tests/security/test_p1.py::test_pw1_1_safe_method_bodies_get_the_ordinary_cap`, `tests/security/test_p1.py::test_pw1_1_the_merge_request_page_does_not_parse_a_get_body`

### PW1-2 Gunicorn's default access log writes C1 control characters from the request line, User-Agent and Referer raw (full re-audit pass 10)
- **Severity:** Low. **Attacker:** anonymous visitor (any request).
- **Repro:** The shipped config (`gunicorn -c gunicorn.conf.py wsgi:app`, `BW_ACCESS_LOG` unset, so stdout) was run on localhost. GET /health was sent with User-Agent `Mozilla\x9b2J\x9b31mFAKE\x9d0;all good\x9c` and Referer `http://r/\x9d52;c;aGk=\x9c`, and GET `/health?\x9b31mX\x9d0;t\x9c` was sent too. Both answered 200. The access log held the 8-bit CSI, OSC and ST bytes raw (UTF-8 C2 9B, C2 9D, C2 9C). Gunicorn refuses C0 controls in headers but decodes 0x80-0x9f as Latin-1 and writes every atom unchanged. Expected: the bytes escaped as `\x9b` and so on, as the application's own TextFormatter does since MW1-2.
- **Fix:** New `bananawiki/ops/access_log.py`: `AccessLogger`, a `gunicorn.glogging.Logger` whose atoms wrapper passes every string atom (request line, Referer, User-Agent, `{header}i` and the rest) through `core.logformat.one_line` before Gunicorn's own quote escaping. `bananawiki/ops/gunicorn_conf.py` (and so the root `gunicorn.conf.py` and the tenant entrypoint) and `hosting/gunicorn.conf.py` set `logger_class` to it. The desktop child has no access log. docs/operations.md says the access log escapes the same characters. After the fix, the same live repro logged `"http://r/\x9d52;c;aGk=\x9c" "Mozilla\x9b2J\x9b31mFAKE\x9d0;all good\x9c"` and `"GET /health?\x9b31mX\x9d0;t\x9c HTTP/1.1"`. The only behaviour change is that control characters show up escaped and line breaks as spaces in the access log. A custom `access_log_format` still works.
- **Test:** `tests/security/test_p1.py::test_pw1_2_the_access_log_escapes_control_characters`

### PW1-3 Replaying the onboarding wizard changes site settings with no audit entry (full re-audit pass 10)
- **Severity:** Low. **Attacker:** plain administrator (any administrator can replay /onboarding unless onboarding_replay_disabled is set).
- **Repro:** Plain admin `plainadmin`, signed in, sent POST /onboarding with site_name `Hijacked Name`, language en, default_theme_mode light, mode easy and new_user_intro_enabled 1. The answer was 302 to /intro, and site_settings changed to `Hijacked Name`, light, 1. No audit_log row was added. Expected: a `settings.updated` entry, like the one every other settings page records (docs/security.md).
- **Fix:** In `bananawiki/wiki/features/auth/onboarding.py`, `apply()` now takes `settings.load()` before the update and calls `audit.record_settings(previous, values, via="onboarding")` right after it, in the same transaction. This happens before the feature switches, so an owner who turns the audit log off in the same run still gets the entry. docs/security.md now names the wizard among the settings pages that are recorded. No compatibility impact.
- **Test:** `tests/security/test_p1.py::test_pw1_3_the_onboarding_wizard_records_the_settings_it_changes`
### PW2-1 A builder page's Markdown blocks were rendered uncached on every view, each with a whole document's limits (full re-audit pass 10)
- **Severity:** Medium. **Attacker:** Editor wherever `page_builder_access` admits editors (plain administrator by default); every signed-in reader pays, and anonymous visitors for a page an administrator made public.
- **Repro:** An editor published 120 text blocks of 1,690 characters (`'# h\n' x 422`) through `POST /api/page/target/builder/publish`. A plain user's `GET /page/target` cost 5.48 / 5.38 / 5.35 / 5.33 s of CPU, never cheaper; the editor's `POST .../builder/preview` of the same document 5.1-5.5 s each (120/min allowed). The same text as an ordinary Markdown page: 0.015 s, then 0.007 s (refused by the heading limit, and cached). Each block was a separate `markdown.render` call: sources under 2,048 characters are never cached, and the parse-work, heading (1,000), output (16 MB) and embed (200) limits applied per block. While fixing it we also found that 12 blocks of 20,000 characters repeating a reference link rendered about 147 MB of HTML (2.4 s) per view, against 12 MB for one such document. Expected: a bounded, cached cost like a Markdown page.
- **Fix:** `markdown.render_parts` renders all Markdown (text, columns, FAQ) and code blocks of a builder document, in order, inside the new `markdown.one_document()`. That context extends `shared_highlighting` with a `_DocumentLimits` allowance: parse work, blocks and headings (`_parser_work_exceeded` now reports what a source used), HTML size (`_OutputLimit` takes a limit) and embeds are shared across the blocks, and a block that no longer fits is shown as escaped source, as an over-limit Markdown page is. `render` neither reads nor keeps per-source cache entries inside it. Instead the renderings are kept together in the render cache, keyed by the hash of all parts and stored as their lengths followed by the joined HTML. The shared 16 MB counts all of that HTML: `render_parts` first sets aside what every part would take shown as source (escaped Markdown, plain code), and inside `one_document()` a Markdown rendering or highlighted code (about 25 times its source) is kept only if it fits in what is left (`_DocumentLimits.fits`), else shown as source or plain code, so the whole entry stays under the cache's per-entry cap (half of `RENDER_CACHE_LIMIT`). A first version counted only the Markdown renderings: a review found that 7 reference-link blocks plus 3 highlighted code blocks made a 17.25 MB entry that the cache dropped, so the page was rendered again on every view (0.32 s, 17.3 MB each); that page is now kept (15.0 MB entry, 0.05 s per later view). They are not kept when highlighting ran out of time shared with other documents or when the helpers were busy. `page_builder/document.py` (`_parts`, `_items`) uses it; page lists and embeds are still resolved for each reader. The repro now costs 0.20 s on the first view and 0.015 s afterwards, and the preview 0.007 s. A builder page with more than 1,000 headings, or more than 16 MB of HTML (less the room set aside for showing every block as source), across its Markdown and code blocks shows the later blocks as source or plain code (UPGRADING.md, docs/security.md).
- **Test:** `tests/security/test_p2.py::test_pw2_1_builder_blocks_share_one_heading_limit`, `tests/security/test_p2.py::test_pw2_1_builder_blocks_share_one_output_limit`, `tests/security/test_p2.py::test_pw2_1_builder_page_is_not_rendered_again_on_every_view`, `tests/security/test_p2.py::test_pw2_1_a_full_builder_page_with_highlighted_code_is_still_cached`

### PW2-2 /search highlighted the full path of every matching category before keeping 50 (full re-audit pass 10)
- **Severity:** Low. **Attacker:** Editor(s) with the default `category.create` plant categories up to the NW2-1 bounds; any user with `search.pages`, or an anonymous visitor in public mode, triggers it.
- **Repro:** 312 chains of 32 categories with 100-character names (9,984 in all). A plain user's `GET /search?q=xxxx` (every category matches) cost 3.22 / 3.18 s of CPU, and `q=c1 x` 4.22 s, every time; almost all of it in `highlight()` over unshortened paths of up to 3.2 KB, of which at most 50 are shown. At 30/min that is about 2 CPU-minutes a minute per account or address. Expected: only the shown categories are highlighted.
- **Fix:** In `pages/search.py`, `search_categories` sorts the matching ids by path first. It then checks `categories.listed` and highlights only until `MAX_CATEGORY_RESULTS` (50) are collected. `_category_ids` casefolds each path once and applies the exclusions while matching. The results and their order are unchanged. Now 0.18 s and 0.21 s for the two queries.
- **Test:** `tests/security/test_p2.py::test_pw2_2_search_highlights_only_the_categories_shown`

### PW2-3 Withdrawing a proposed edit freed the quota slot but kept its 1 MB text for 90 days (full re-audit pass 10)
- **Severity:** Low. **Attacker:** Plain user (`contribution.propose` is a user default) while the contributions feature is on.
- **Repro:** Contributions on, 30 pages, a plain user. The user ran 30 cycles of `POST /page/page-<i>/propose-edit` (999,000-character content, 2,000-character reason) and `POST /page/page-<i>/contribution/<id>/withdraw`: every request answered 302, in 0.47 s in all. Afterwards `pending_contributions` held 30 withdrawn rows of 30,030,000 bytes and 0 pending, so the quota was untouched. That is about 1 MB per readable page per account, kept for 90 days, with only the global 300/min limit. Expected: a withdrawn proposal stores nothing that counts, and the loop is rate limited.
- **Fix:** `contributions/service.withdraw` now clears `content` and `reason` when it marks a proposal withdrawn; nothing shows them again, and the row (page, title, status) stays in *My contributions*. The `propose-edit` and `withdraw` views share a per-account `rate_limited("contribution", 30)` bucket (429 past it). Proposals withdrawn automatically after a role change, and denied or expired ones, keep their text: neither loop is in the proposer's hands. The account's data export no longer contains the text of proposals it withdrew. A reviewer opening such a proposal at `/admin/contributions/<id>` sees that the proposer withdrew it and its text is no longer kept, instead of a diff deleting the whole page (UPGRADING.md, docs/features.md).
- **Test:** `tests/security/test_p2.py::test_pw2_3_withdrawn_proposals_keep_no_text`, `tests/security/test_p2.py::test_pw2_3_proposing_and_withdrawing_are_rate_limited`, `tests/security/test_p2.py::test_pw2_3_a_withdrawn_proposal_does_not_show_as_deleting_the_page`
### PW3-1 Account merge requests could be filed and cancelled in a loop, and the merge page listed them all unpaged (full re-audit pass 10)
- **Severity:** Low. **Attacker:** any approved user; the victim is any other account named in the requests.
- **Repro:** alice ran 40 cycles of `POST /settings/merge-request` (`other_username=victim`, a 600-character reason, her password) and `POST /settings/merge/cancel/<id>`: every answer was 302 and a 41st request was still accepted, leaving 40 `account_merge_requests` rows naming the victim. Only the global limit (about 150 cycles a minute per client) bounded the loop. The victim's `/settings/merge/pending` listed every closed request unpaged (42.5 KB at 40 rows against 36.9 KB without, about 140 B a row, roughly 30 MB after a day). Expected: a daily bound as for the portal's merges (OW4-1) and a bounded list.
- **Fix:** `users/merge.create` counts the requests the account filed in the last 24 hours, whatever became of them, inside the inserting transaction and refuses past `MAX_REQUESTS_PER_DAY` (10, `users.merge.error.too_many`, en/it/de). `merge.for_user` reads the active requests and only the newest `HISTORY_LIMIT` (50) closed ones. Documented in `docs/features.md`, the account user guides (en, it) and UPGRADING.md.
- **Test:** `tests/security/test_p3.py::test_pw3_1_merge_requests_are_bounded_per_day`, `tests/security/test_p3.py::test_pw3_1_merge_page_lists_a_bounded_history`

### PW3-2 Group chats had no creation cap, and /groups listed every membership unpaged (full re-audit pass 10)
- **Severity:** Low. **Attacker:** any approved user with `chat.create_group` (on by default); the victim is any account that can join groups.
- **Repro:** through the routes, with the limiter clock moved forward a minute at a time, plain user mal2 created 90 groups with 500-character descriptions in six minutes and added vic2 to each without any consent step (180 x 200). Only the shared 30-a-minute action limit applied, about 21,600 groups a day. With 20,000 such groups inserted, vic2's `GET /groups` took 0.38-0.46 s and returned 6.4 MB on every view, unpaged, with correlated subqueries per row. Expected: a daily creation bound and a paged list.
- **Fix:** `chat/groups.create` counts the groups the account created in the last 24 hours inside its transaction and raises `TooManyGroups` past `MAX_CREATES_PER_DAY` (20); `group_new` answers 429 `chat.groups.error.too_many_created` (en/it/de). A first version counted `group_chats` rows whose `creator_id` was the account, but a review showed that transferring a group (`POST /groups/<id>/transfer`, and an administrator takeover) rewrites `creator_id`: with the cap at 3, 8 cycles of create, add the victim, transfer to the victim all answered 200, leaving the victim in 8 groups and none counted against the creator (a sock puppet as new owner works the same). Each creation now also reserves a hit in `rate_limit_hits` (bucket `chat:group-create`, key `user:<id>`, as the self-service rename limit does) in the same transaction, which no transfer or deletion gives back; the owned-groups count stays as well, for groups made before the update. Moving a wiki to another host drops `rate_limit_hits`, as it does for every limit kept there. `groups.list_for` takes `limit`/`offset`, `groups.count_for` counts memberships, and `/groups` shows `LIST_PAGE_SIZE` (100) groups a page with `forms.pagination`. With 20,000 memberships a view now takes about 0.1 s and 69 KB. Adding a member still needs no consent (a moderator adding people is the feature); with creation bounded whatever becomes of the groups, one account adds a member to at most 20 new groups a day, and the paged list bounds each view. Documented in `docs/features.md` and UPGRADING.md.
- **Test:** `tests/security/test_p3.py::test_pw3_2_group_creation_is_bounded_per_day`, `tests/security/test_p3.py::test_pw3_2_transferring_groups_away_does_not_free_the_creation_budget`, `tests/security/test_p3.py::test_pw3_2_group_list_is_paged`

### PW3-3 The 8 MB Kanban board cap counted UTF-8 bytes, but snapshots were stored ASCII-escaped, so one board's snapshot reached 50 MB (full re-audit pass 10)
- **Severity:** Low. **Attacker:** anyone with write access to one board (a plain user's own board is enough).
- **Repro:** a board filled through `POST /api/kanban/columns/<id>/tickets` with descriptions of 10,000 x `\x01` was refused at ticket 830 (`board_bytes` 8,386,210), the same as a board of `a`, but a forced `history.record` stored 49,932,746 bytes (0.28 s, 62 MB peak) against 8,431,977 bytes (0.05 s, 17 MB); emoji gave 25 MB. `json.dumps` escaped every non-ASCII character and every C0 control (kept in descriptions) as `\uXXXX`. After a revert, `HISTORY_MAX_BYTES` (64 MB) kept only the newest 50 MB entry, so earlier revert points were lost. `update_ticket` checked the cap only when the description or the assignees grew, not the title or the labels. Expected: snapshots near the 8 MB cap.
- **Fix:** `kanban/history.record` stores snapshots with `ensure_ascii=False`, so their size is the UTF-8 size `board_bytes` measures. The new `kanban/fields.plain_text` drops C0 control characters other than tab, newline and carriage return (JSON writes them as six bytes even then). It is used for ticket, column and board titles, descriptions and checklist items, when they are saved, imported (`transfer.py`) or restored by a revert (`history.py`). `service.update_ticket` and `service.update_checklist_item` run `check_room` when the title, the description, the labels or a checklist item grow in UTF-8 bytes (`service._grows`), not only in characters: a first version compared character counts, and a review showed that rewriting five ASCII tickets with the same number of four-byte characters took a board to about 4x the cap (206,480 bytes against 52,095). An assignee change runs it whenever it adds anyone, since a swap to a longer username grows the board at the same count. What remains: quotes, backslashes and line breaks take two bytes in JSON, so a worst-case snapshot is about twice the cap rather than six times (measured: a 200,000-byte cap filled with descriptions of `"` gave a 383,123-byte snapshot). Control characters already stored before the update stay until the text is next saved. The first snapshot after the update differs in encoding, so it adds one entry even when nothing else changed. Documented in `docs/features.md` and UPGRADING.md.
- **Test:** `tests/security/test_p3.py::test_pw3_3_ticket_text_drops_control_characters`, `tests/security/test_p3.py::test_pw3_3_snapshot_is_stored_as_utf8`, `tests/security/test_p3.py::test_pw3_3_title_and_label_growth_counts_against_the_cap`, `tests/security/test_p3.py::test_pw3_3_wider_characters_at_the_same_length_count_against_the_cap`

### PW3-4 While impersonating, edit proposals, quota requests and federation shares were made in the member's name with no audit entry (rest of OW3-3) (full re-audit pass 10)
- **Severity:** Low. **Attacker:** plain administrator (impersonating a non-administrator needs no superuser).
- **Repro:** with the audit and contributions features on and one federation peer, admin ada impersonated bob. `POST /page/payroll/propose-edit` and `POST /my-contributions/quota-request` both answered 302 and stored rows with `user_id` = bob, and no `audit_log` row was written. Impersonating editor eve, `POST /federation/sharing` answered 302 and stored a share with `granted_by` = eve, again with no audit row. Expected: an `*.impersonated_action` entry with the administrator as actor, as for every other page change (NW2-3, OW3-3).
- **Fix:** `pages/blueprint._IMPERSONATION_AUDITED_ENDPOINTS` now lists `contributions.propose`, `contributions.edit`, `contributions.withdraw`, `contributions.quota_request`, `contributions.cancel_quota_request` and `federation.sharing` (share and unshare). Each such request while impersonating gets a `page.impersonated_action` entry with the administrator as actor, the member as `as_user`, the endpoint and the status. Documented in `docs/security.md` and UPGRADING.md.
- **Test:** `tests/security/test_p3.py::test_pw3_4_impersonated_proposals_quota_requests_and_shares_are_audited`
### PW4-1 A wiki's client credentials can link and unlink in a loop, each cycle adding a moderation-log row kept 10 years (full re-audit pass 10)
- **Severity:** Low. **Attacker:** hosting tenant with code in its wiki container (tenant plugins, or a compromised wiki) that can read the wiki's OAuth client secret, after signing in once through the platform.
- **Repro:** with platform sign-in on, wiki credentials from `oauth.rotate_credentials` and a real access token from `/oauth/token`, 300 cycles of `POST /oauth/link` + `POST /oauth/unlink` all answered 200 and `hosting_events` grew by exactly 300 `oauth.linked` rows (retention 10 years), pushing every real entry off `/admin/moderation`. Expected: link churn bounded or identical events coalesced.
- **Fix:** `/oauth/link` (`bananawiki/hosting/routes/oauth.py`) records `oauth.linked` only when the same account was not linked to the same wiki in the last day; the check is the new `events.recorded_since`, which reads only the log since the cutoff (binary search on ids plus the subject index). Linking itself is unchanged. Documented in docs/hosting.md and UPGRADING.md.
- **Test:** `tests/security/test_p4.py::test_pw4_1_link_loop_records_one_event_a_day`

### PW4-2 With platform sign-in off (the default) the /oauth/* server-to-server paths are answered without counting against the global per-client limit (full re-audit pass 10)
- **Severity:** Low. **Attacker:** anonymous visitor.
- **Repro:** `GLOBAL_RATE_LIMIT` patched to 5, platform sign-in off: 50 requests to each of the 8 server-to-server paths all answered 404 and none was counted; a control `GET /no-such` from the same client still had its full budget of 5. Expected: 429 once the budget is spent.
- **Fix:** `_request_limits` (`bananawiki/hosting/app.py`) exempts server-to-server views from the global per-network limit, and gives them the 64 KiB body cap, only while `oauth.enabled()`; while sign-in is off they count like any other path. Docstrings in `limits.py` and `routes/oauth.py`, docs/hosting.md and UPGRADING.md say so.
- **Test:** `tests/security/test_p4.py::test_pw4_2_disabled_oauth_paths_count_against_the_global_limit`

### PW4-3 Personal API tokens can be created and revoked in a loop; revoked rows and their two events are never removed while the account lives (full re-audit pass 10)
- **Severity:** Low. **Attacker:** any approved hosting user, once an administrator has switched the REST API on.
- **Repro:** api_enabled=1, one account, 40 cycles of create (from a new address each time) + revoke: all accepted, leaving 40 revoked `hosting_api_tokens` rows and 80 `api.token.*` events with 0 live tokens; the 10-token cap counts only live tokens and the only bound was `rate_limit(5)` per network. Expected: a per-account daily creation budget.
- **Fix:** `api_tokens.create` (`bananawiki/hosting/api_tokens.py`) also counts, inside its transaction, the tokens the account created in the last 24 hours, revoked or not, and refuses past `MAX_CREATED_PER_DAY` (20) with the new `hosting.api_tokens.too_many` message (en, it, de). The maintenance prune step (`api_tokens.prune`, from `maintenance._prune`) deletes rows of tokens revoked or expired more than 90 days ago; their moderation-log entries stay. Documented in docs/hosting.md and UPGRADING.md.
- **Test:** `tests/security/test_p4.py::test_pw4_3_token_creations_are_capped_per_day`, `tests/security/test_p4.py::test_pw4_3_old_revoked_tokens_are_pruned`

### PW4-4 No per-wiki or per-account bound on pausing/resuming: one client flaps its wiki ~100 times a minute, each operation holding the runtime agent's global lock, re-listing every tenant and reloading Caddy (full re-audit pass 10)
- **Severity:** Medium. **Attacker:** any approved hosting user who owns a wiki (or a collaborator with start_stop).
- **Repro:** api_enabled=1, one owner with one wiki and 10 tokens holding `instances:manage`; from one address, alternating `POST /api/v1/instances/<id>/pause` and `/resume` per token: all answered 200 and FakeRuntime recorded 50 stop and 50 start (`sync_routes` once per change). The web stop/restart routes were limited only per network. The agent-side cost (agent-wide lock held through `docker stop --time 15`, tenant listing and Caddy reload per change) was traced in `bananawiki/ops/runtime_agent.py`, not run (no Docker on the audit host). Expected: a per-wiki budget of a few state changes a minute.
- **Fix:** new `instances.count_state_change` (`bananawiki/hosting/instances.py`) counts each pause, resume, restart or factory reset of a wiki in the shared SQL limiter (`hosting_wiki_state`, keyed by the wiki) and refuses past 6 a minute (`hosting.instances.too_many_changes`) or 100 a day (`hosting.instances.too_many_changes_today`, with the hours left; en, it, de). It is called inside `instances.stop`, `start`, `restart`, `reset_content` and `set_easy_wiki` (switching a running wiki between easy (BM) and full mode restarts it through `apply_policy`, the same lock-holding stop and start; an independent review found this path uncounted) through a new `charge=` argument (the asking account), only after their own status checks pass, so a refused or no-op request (pausing a paused wiki) costs nothing. A resume is held to the per-minute budget only, so whoever spends the day's budget (a collaborator with start_stop, or repeated clicks) cannot keep the wiki down: the owner can always resume within a minute. Administrators and system callers (no `charge`: policy changes, unsuspension, restore, admin routes) are not counted. The web stop, restart, bulk stop/resume, reset-wiki and toggle-easy-wiki routes (`routes/dashboard.py`) pass the account and flash the error; the REST API `_change_state` (`routes/api.py`) answers 429 with `retry_after` 60 for the minute limit and, for the daily one, the seconds until the day's oldest counted change is 24 hours old. The agent's lock scope is unchanged: each tenant now has a per-wiki bound of 6 lock-holding stop/start cycles a minute and about 100 a day (plus resumes) across the paths an owner or collaborator can trigger (pages, bulk, API, reset, easy/full mode switch), not counting administrators. Two other owner-triggered `apply_policy` restarts are not counted: a feature request the platform approves automatically (`features.request`), which cannot be repeated (each feature is granted once per wiki, `already_granted`, and only an administrator can revoke it); and an accepted ownership transfer (`collaborators.accept_transfer` -> `instances.move_to_owner`), which an owner with a cooperating second account can repeat. **Accepted:** transfer offers are already capped at `MAX_TRANSFERS_PER_DAY` (10 per wiki in 24 hours, OW4-1), so at most about ten extra restarts a day per wiki get past the budget (found in review). Documented in docs/hosting.md and UPGRADING.md.
- **Test:** `tests/security/test_p4.py::test_pw4_4_api_pause_resume_is_bounded_per_wiki`, `tests/security/test_p4.py::test_pw4_4_web_pause_resume_is_bounded_per_wiki_but_not_for_admins`, `tests/security/test_p4.py::test_pw4_4_refused_changes_do_not_spend_the_budget`, `tests/security/test_p4.py::test_pw4_4_a_spent_day_still_lets_the_wiki_resume`, `tests/security/test_p4.py::test_pw4_4_resetting_the_wiki_counts_against_the_same_budget`, `tests/security/test_p4.py::test_pw4_4_a_spent_day_reports_its_real_wait`, `tests/security/test_p4.py::test_pw4_4_easy_wiki_toggle_restarts_count_against_the_budget`

### QW1-1 Wiki API tokens and userbot keys can be minted and revoked in a loop; revoked rows are never removed (wiki sibling of PW4-3) (full re-audit pass 11)
- **Severity:** Low. **Attacker:** any account the REST API is open to (administrators always; members given `api_access_enabled`), once the API is switched on.
- **Repro:** API on, plain user with API access: 60 x (`POST /settings/api-tokens/create` + `POST /settings/api-tokens/<id>/revoke`) all 201/302, leaving 60 rows and 0 live; 100 x `POST /settings/api-tokens/userbot` `enable=1` all 200, leaving 100 rows and 1 live (each re-issue revokes the old key and inserts a new row); 25 x API `POST /tokens` + `DELETE /tokens/<id>` left 26 rows; `api_service._prune()` left a revoked row dated 2000-01-01. `check_quota` counts only active tokens and nothing deleted `api_service__tokens` rows. Expected: a per-account daily creation budget as on the portal, and old revoked rows pruned.
- **Fix:** `tokens.create` (`bananawiki/wiki/features/api_service/tokens.py`) counts, inside its transaction, the account's tokens created in the last 24 hours, revoked ones and userbot keys included, and refuses past `daily_creation_limit()` (`MAX_CREATED_PER_DAY` = 20, or the account's active-token limit `api_service_max_tokens_per_user` if higher, so a wiki allowing 100 tokens can still create them) with 429 `too_many_tokens_today` (new message in en, it, de). It covers all three paths: the token page (`web.create_token`, now flashing the error), the userbot switch (`web.userbot_mode`; the refused re-issue rolls back inside `userbot.enable`'s transaction, so the current key keeps working) and `POST /tokens`. New `tokens.prune`, run by the hourly `api_service.prune` job, deletes rows of tokens revoked and created, or expired, more than 90 days ago, with the revoked tokens they issued; their calls stay in the API audit log. The table records no revocation time, so a revoked token's row goes once the token was created 90 days ago (no migration needed; a revoked token is shown nowhere and can never authenticate again). Compatibility: a script re-issuing userbot keys or tokens more than 20 times a day is refused until the day moves on. Documented in docs/api.md and UPGRADING.md.
- **Test:** `tests/security/test_q1.py::test_qw1_1_token_creations_are_capped_per_day`, `tests/security/test_q1.py::test_qw1_1_old_revoked_tokens_are_pruned`

### QW1-2 Plugin manager (and site migration) password confirmations are checked outside the shared per-account password budget (full re-audit pass 11)
- **Severity:** Low. **Attacker:** someone holding an owner's, superuser's or administrator's signed-in session without knowing the password (unattended browser, stolen cookie).
- **Repro:** hello_plugin installed, `BW_ALLOW_EXTERNAL_PLUGINS=1`, owner signed in: 11 wrong passwords on `POST /settings/username` spent the shared `account:password` budget (10 in 15 minutes; the right password was then refused there). 19 further wrong passwords on `POST /admin/plugins/hello_plugin/enable` were each still checked ('The password is not correct.') and the right one then answered 302 and enabled the plugin; `POST /admin/plugins/import` with a junk archive answered 'not a valid plugin archive' for the right password; `POST /admin/migration/export` returned the whole-site ZIP for the right password after 3 wrong ones. `account:password` stayed at 10 rows: these checks called `verify_password` directly, bounded only by per-worker in-memory route limits (about 35 guesses a minute per worker). Expected: they share the account's budget and refuse once it is spent.
- **Fix:** `plugin_manager.service.password_ok` (`bananawiki/wiki/features/plugin_manager/service.py`) now calls `users.service.password_ok(auth.real_user(), password)`, and `plugin_manager.routes._password` flashes its `ProfileError` (`auth.error.too_many_attempts`) and refuses (`limited`), for enable, import and delete alike. `site_admin.routes_migration._confirmed` does the same for export and import, recording `site.<action>_refused` with reason `too_many_attempts`. A missing password is still not counted; the existing per-worker route limits stay. Documented in docs/security.md and UPGRADING.md.
- **Test:** `tests/security/test_q1.py::test_qw1_2_plugin_enable_refuses_the_right_password_once_the_budget_is_spent`, `tests/security/test_q1.py::test_qw1_2_plugin_password_guesses_spend_the_shared_budget`, `tests/security/test_q1.py::test_qw1_2_site_export_refuses_the_right_password_once_the_budget_is_spent`

### QW1-3 'Replace the guide' (and the onboarding wizard's guide option) deletes every page of the guide's category at once, protected and checked-out pages included (full re-audit pass 11)
- **Severity:** Low. **Attacker:** plain administrator (Admin -> Documentation `POST /admin/spawn-docs`, or replaying `/onboarding` with `spawn_docs`, open to any admin unless `onboarding_replay_disabled`).
- **Repro:** page governance, page protection and deletion slowdown on; the owner creates the guide; an editor creates 'Team runbook' in the guide category and protects it. A plain administrator's `POST /page/<slug>/delete` and `POST /category/<id>/delete` (`page_action=delete`) leave it unchanged and not pending (protection holds). `POST /admin/spawn-docs` `variant=simplified` then answered 302 and the page row and all its history were gone at once, no pending deletion (audit: `page.deleted` per page, `category.deleted`, `docs.spawned`): `docs._remove_existing` called `categories.delete(page_action="delete")`, which skips `access.edit_blocked`, `can_delete` and the `page.delete` interceptors. Expected: pages the installer did not write, and protected or checked-out pages, survive.
- **Fix:** `docs.spawn` (`bananawiki/wiki/features/auth/docs.py`) first checks every non-home page of the current guide's category and refuses the replacement with `CategoryError("auth.docs.replace_refused")` (en, it, de), naming up to 10 of them (only those the administrator can view), while any page was not written by the installer (`builtin_doc_pages`) or is blocked for the acting administrator (`access.edit_blocked`: protected, checked out, pending deletion), before anything changes. `_remove_existing` then deletes only installer pages that are still not blocked (checked again page by page), and deletes the old category, without pages, only when none is left, so a page protected in between keeps its category instead of being moved or deleted. Both callers already turn a `CategoryError` into a flash (Admin -> Documentation; the onboarding wizard, whose other choices are already applied). Installer pages that are not blocked are still deleted at once, as `docs_bypass_deletion_slowdown` already allows for them. Chose refusal over moving the pages elsewhere: moving them to the top level or into a new category could change who may see them. Compatibility: a guide category people added pages to must be emptied of them before it is replaced. Documented in docs/features.md and UPGRADING.md.
- **Test:** `tests/security/test_q1.py::test_qw1_3_replacing_the_guide_keeps_a_protected_page_people_wrote`, `tests/security/test_q1.py::test_qw1_3_replacing_the_guide_keeps_an_unprotected_page_people_wrote`, `tests/security/test_q1.py::test_qw1_3_replacing_the_guide_keeps_a_protected_guide_page`, `tests/security/test_q1.py::test_qw1_3_replacing_an_untouched_guide_still_works`
### QW2-1 Without FTS5, /search runs full-length LIKE terms with the clock read every 10,000 steps, far past the 5 s backstop (full re-audit pass 11)
- **Severity:** Low. **Attacker:** An editor plants large pages; any user with search.pages (or an anonymous visitor in public mode) triggers it, 30 a minute per client; only on a SQLite without FTS5.
- **Repro:** With 5-6 pages of about 1,000,000 emoji, `app.extensions['bananawiki.fts'] = False` and SEARCH_SECONDS lowered to 0.5, `GET /search?q=<emoji x199-250 + letter>` (and the same with `content:`) answered 200 after 13.5-14.2 s of CPU, while `/api/sidebar/search` with the same query (fixed in MW2-4) took 0.01 s. MW2-4 bounded only `service.search`: the non-FTS branch of `search.search_pages` still matched each term (up to 300 characters) at full length and ran under `time_limit()` with the clock read every 10,000 steps, while one page's LIKE is a single step. Expected: bounded work per page and a 503 at the deadline.
- **Fix:** `bananawiki/wiki/features/pages/search.py` `search_pages`: without FTS5, each term and exclusion matches only its first `service.LIKE_CHARS` (32) characters, and the statements run under `service.time_limit(service._LIKE_PROGRESS_STEPS)` (the clock read every 10 steps), as `service.search` does. With FTS5 nothing changes. Measured: 12 pages of 400,000 emoji, deadline 0.2 s: every query answers within 0.8 s (503 for a near miss), against 8-10 s before. Documented in docs/features.md and UPGRADING.md.
- **Test:** `tests/security/test_q2.py::test_qw2_1_search_like_fallback_is_short_and_stops_at_the_deadline`

### QW2-2 One PDF export of a planted page costs ~70 s of CPU, unbounded, and the Host header or any history entry defeats the PDF cache (full re-audit pass 11)
- **Severity:** Medium. **Attacker:** An account holding page.export_pdf (off by default) with pdf_export_enabled on; an editor plants the page or its history versions.
- **Repro:** A page of 12,700-15,000 paragraphs of 12 words (about 1 MB, accepted by the editor) exported with `GET /page/para/export-pdf` took 50-69 s of CPU (200); the same with another `Host` header took as long again (a cache miss), and only the same Host was served from the cache. Meanwhile the process's single PDF slot was held, so other exports got Busy after 10 s. Profiling showed the cause: fpdf2's `write_html` keeps the whole page in one text region and, at the end of every paragraph, rebuilds lines for every earlier paragraph (80,000 `build_lines` calls for 400 paragraphs, 80 M for 12,700). The cache key included `request.url_root`, and each history entry has its own key. Expected: a bounded cost per export, made once per page version.
- **Fix:** `bananawiki/wiki/features/page_export/pdf.py`: `_Document.text_columns` drops laid-out paragraphs from fpdf2's region after each render (they have nothing left to lay out; the output is byte-identical on a mixed test document), which makes the layout linear: the 992,188-character page now takes about 5 s. The layout is also bounded: after `MAX_PAGES` (500) pages or `LAYOUT_SECONDS` (8 s) the PDF ends with a translated note (`page_export.pdf.cut`, en/it/de, part of the cache key). The budget is checked at the end of each paragraph, after each block of the plain-text fallback, and before every page break (`_Document._perform_page_break`), because fpdf2 lays out one code block, one table, or a page the Markdown budget renders as a single plain-text block in one go: checked at paragraph ends only, a 1 MB fence of `x` lines still made 6,266 pages and one of newlines 12,532 pages in about 10 s, with the note after all of them. The plain-text fallback keeps the first layout's deadline instead of starting a new one. Wherever the layout is cut, `_render` first closes the graphics states fpdf2 left open (`_Document.close_graphics_states`, as fpdf2 does before a new page) and then writes the note: a cut at the end of a paragraph after a table otherwise left one page stream with an unbalanced `q` (11 `q`, 10 `Q`). A PDF cut by `LAYOUT_SECONDS` depends on how busy the CPU was, so it is cached for `TIME_CUT_CACHE_SECONDS` (5 minutes) only and then laid out again; one cut by `MAX_PAGES` is cached like a complete one. Measured on 1 MB pages: every variant (paragraphs, tiny paragraphs, code blocks, tables, lists, quotes, headings past the Markdown budget, plain text) now stops within about 8 s, Markdown included. `routes.py`: links, and so the cache key, use the wiki's public address (`BW_BASE_URL`, else the address in Admin → Notifications) when one is set, so the Host header neither changes the links nor misses the cache. Without a public address the request's address is used as before, and each Host or history entry still costs a layout, now bounded and limited to 5 a minute per account. Documented in docs/features.md, docs/configuration.md and UPGRADING.md. Compatibility: a page longer than 500 PDF pages gets a cut PDF.
- **Test:** `tests/security/test_q2.py::test_qw2_2_layout_does_not_revisit_earlier_paragraphs`, `tests/security/test_q2.py::test_qw2_2_a_long_pdf_is_cut_with_a_note`, `tests/security/test_q2.py::test_qw2_2_one_long_block_is_cut_at_a_page_break`, `tests/security/test_q2.py::test_qw2_2_plain_text_fallback_keeps_the_layout_deadline`, `tests/security/test_q2.py::test_qw2_2_host_header_does_not_defeat_the_pdf_cache`, `tests/security/test_q2.py::test_qw2_2_a_cut_at_a_paragraph_end_closes_the_graphics_states`, `tests/security/test_q2.py::test_qw2_2_a_pdf_cut_by_time_is_laid_out_again_later`
### QW2-3 Every history-list view measured the full text of every version of the page (full re-audit pass 11)
- **Severity:** Low. **Attacker:** Editor who plants many large versions; any reader with `history.view` (a plain-user default) paid on every view.
- **Repro:** After 300 saves of a 999,000-character page, a plain reader's `GET /page/big/history` cost 0.7-1.0 s CPU on every request and on every list page (`?page=12` too): `_history_rows` ran `length(content)` and `LAG(length(content)) OVER (ORDER BY id)` over every version before `LIMIT 50`. The same rows measured alone took 0.04 s. 80 back-to-back list views by one reader all answered 200. Expected: cost bounded by the 50 shown versions, and a per-route limit like the single-version view's.
- **Fix:** `page_history/routes.py` `_history_rows` selects the shown ids first (`id IN (SELECT id ... ORDER BY id DESC LIMIT 51 OFFSET ?)`), measures only those rows and computes each change in size against the next older row in Python (the 51st row serves the oldest shown one; the first version still shows its size). `history_list` is now `rate_limited("history-list", 30)`: 30 list views a minute per account or address, in a bucket of its own so browsing versions does not use it up. `docs/features.md` and `UPGRADING.md` give the limit.
- **Test:** `tests/security/test_q3.py::test_qw2_3_history_list_measures_only_the_shown_versions`, `::test_qw2_3_history_list_still_shows_each_change_in_size`, `::test_qw2_3_history_list_without_versions_still_answers`, `::test_qw2_3_history_list_has_a_per_route_limit`

### QW2-4 Attachments per page were unbounded and every page view listed them all (full re-audit pass 11)
- **Severity:** Low. **Attacker:** Editor (default `attachment.upload`); every reader of the page paid on every view.
- **Repro:** 20 upload requests of 20 one-byte files all answered 302 (the 21st 429): 400 rows a minute on one page, with nothing bounding the total (the daily upload quota is off by default). At 24,000 rows a plain reader's `GET /page/files` was 6 MB and 0.5 s CPU, an editor's 13.7 MB and 1.3 s (the editor list ran `can_delete`, so edit rights and edit interceptors, once per file), `GET /page/files/edit` 12 MB and 0.8 s. Expected: a bounded count.
- **Fix:** `attachments/service.py`: `MAX_PER_PAGE = 500`; `add` refuses the file with `attachments.error.page_full` (en, it, de) once the page holds that many, checked before the file is stored and again inside the insert transaction, so the browser upload flashes the reason and the editor/REST upload answers 400 `upload_refused`. The page and editor panels (`slots.py`) list at most `MAX_PER_PAGE` files (pages that held more before the limit keep them, the heading counts them all and *Download all* still includes them), and the new `service.deletable` checks edit rights once per list and only the uploader per row; `can_delete` uses it. `docs/features.md` and `UPGRADING.md` describe the limit.
- **Test:** `tests/security/test_q3.py::test_qw2_4_uploads_stop_at_the_per_page_limit`, `::test_qw2_4_page_view_lists_a_bounded_number_of_attachments`

### QW2-5 Creating, changing and deleting custom pages left no audit entry (full re-audit pass 11)
- **Severity:** Low. **Attacker:** Plain administrator holding `custom_page.manage`, or a superuser impersonating one.
- **Repro:** With `custom_pages` on and the wiki private, a plain admin created `/sign-in-help` as a published redirect to `https://attacker.example/a` (302), an anonymous visitor was sent there, the admin changed it to `/b` and deleted it (302 each): no new `audit_log` row, only `log.info` lines. The video-size setting on the same page was audited, so auditing worked. File uploads and deletions and builder saves were not audited either. Expected: each change recorded, as page, category and attachment changes are.
- **Fix:** `custom_pages/service.py` records, in `create`, `update` (so the form and the builder's `save_builder_document`), `delete`, `add_file` and `delete_file`: `custom_page.created`, `custom_page.updated`, `custom_page.deleted`, `custom_page.file_added`, `custom_page.file_deleted`, target `custom_page`/id, with the path, content type, `is_published`, for a redirect the host it leads to, on an update the columns changed and the previous values of those fields, the file name (and size), and `as_user` while impersonating (the actor is the real account, the impersonator). Action labels in the audit catalogues (en, it, de); `docs/security.md` lists the entries. Recording in the service covers every route, so the custom-pages blueprint was not added to the generic impersonation list (its `page_id` would be logged as a wiki page).
- **Test:** `tests/security/test_q3.py::test_qw2_5_custom_page_changes_are_audited`, `::test_qw2_5_custom_page_builder_saves_are_audited`, `::test_qw2_5_an_impersonated_custom_page_change_names_both_accounts`
### QW3-1 A Kanban board kept just under 1 MB stored a full ~1 MB history snapshot on every one-field edit (full re-audit pass 11)
- **Severity:** Low. **Attacker:** an account that may create or import boards (`kanban.create` with global write access; administrators by default), or a plain user with a write share on one board (bounded at 64 MB of history per board).
- **Repro:** editor eve (`kanban_write_access=editor`) imported a board of 98-99 tickets with 9,800-9,900-character descriptions (`board_bytes` about 980 KB, under `LARGE_BOARD_BYTES` 1,048,576), then sent `PUT /api/kanban/tickets/<id>` `{"title": "zN"}` 40 times: every edit answered 200 (0.29 s in all) and the history went from 1 row (985,828 bytes) to 41 rows (40.4 MB), one ~1 MB snapshot per one-field edit. Three such boards gave 201 MB of history from 213 requests in 1.5 s. The once-a-minute throttle (W4-1) applied only to boards over 1 MB, and ticket edits are neither coalesced nor rate-limited. Expected: history growth per request bounded.
- **Fix:** `kanban/history.record` now skips a new snapshot within `snapshot_interval(board_bytes)` seconds of the latest entry, for every board: `HISTORY_MIN_SECONDS` (60) times the board's share of `LARGE_BOARD_BYTES`, up to 60 s from 1 MB on, and 0 (an entry per change, as before) when that is under `MIN_THROTTLE_SECONDS` (2 s, boards under about 35 KB). So one board's history grows by about 1 MB a minute at most, whatever its size; the changes in between are part of the next entry, `force` and reverts still record at once, and a revert first records the board as it is (as for large boards before). The REST route goes through the same `history.record`. Boards between 35 KB and 1 MB get coarser history; documented in `docs/features.md` and UPGRADING.md. The throttle bounds one board only: boards are free to create (10 a minute) and the ticket API fills one to about 1 MB in 98 requests, so across many boards history still grew by about 1 MB per request (found in review). `history.record` therefore also counts what each entry adds to its board's history, after the per-board pruning, against its author's daily budget (`STORED_MIB_PER_DAY`, 128 MiB in 24 hours, bucket `kanban:stored`, shared with imports, see QW3-4), to the byte. Past the budget the change is still made and recorded, but the board's history may not grow: its oldest entries are pruned until the history is no larger than before the change (the newest entry always stays). A first version charged each entry's whole snapshot in whole 64 KiB units, rounded down, and refused the change past the budget; review found that (a) entries under 64 KiB were free, so edits of boards under ~35 KB (no throttle) stored 9.1 MB of history in a minute with nothing charged (about 13 GB a day per account); (b) one person editing a 7.5 MB board once a minute was refused (429) after 15 edits and then locked out of every board of 64 KiB or more for 24 h; (c) account deletions and merges by an administrator who had used up the budget failed, because the hand-over's history entry, written as that administrator inside the deletion, raised an error the deletion does not catch. Now (a) amounts are exact (schema 19, `rate_limit_hits.amount`, `SqlLimiter.reserve_amount`, one row a minute per account), (b) a board whose history is full (200 entries or 64 MB) costs about nothing more and nothing is refused (measured with the real constants: 200 once-a-minute edits of the 7.5 MB board all answered 200), and (c) hand-over entries are not charged (`charged=False`). Pruning a board's oldest entries is something any writer could already cause (200 one-field edits on a small board, the 64 MB cap on a large one), so out-of-budget rotation gives a writer nothing new. What remains: past the budget an account's boards keep at most their history from before plus their newest entry, which is about the board's own text; that ticket text (each board at most 8 MB, filled by ordinary requests of at most one ticket each, about 10 KB) is not under a daily budget, like wiki pages. Boards between 35 KB and 1 MB get coarser history. Documented in `docs/features.md`, `docs/api.md`, UPGRADING.md and CHANGELOG.md.
- **Test:** `tests/security/test_q4.py::test_qw3_1_a_board_under_the_large_size_is_not_snapshotted_on_every_edit`, `tests/security/test_q4.py::test_qw3_1_editing_a_large_board_all_day_is_never_refused`, `tests/security/test_q4.py::test_qw3_4_many_boards_filled_and_edited_through_the_api_share_the_daily_budget`, `tests/security/test_q4.py::test_qw3_4_entries_of_small_boards_are_charged_by_the_byte`, `tests/security/test_q4.py::test_qw3_4_an_administrator_out_of_budget_can_still_delete_an_account`

### QW3-2 Administrator edits of a member's public profile left no audit entry, while the same change made while impersonating was recorded (full re-audit pass 11)
- **Severity:** Low. **Attacker:** a plain administrator (accountability), limited by `can_moderate` to accounts they may change.
- **Repro:** admin ada posted `/admin/users/<bob>/profile` with `action=edit_profile` and bio `I resign. -- bob`, then `action=publish_profile`, then `/admin/users/<bob>/profile-fields` (`value_<id>=Basement`, visible) and `/admin/users/<bob>/tags` `action=add_tag`: all 302, and a signed-in viewer's `GET /users/bob` showed the forged bio and field, but no audit row named ada or targeted bob. The same edit through `/settings/profile` while impersonating bob recorded `user.impersonated_action` (OW3-3). Expected: an audit entry for the direct administrator route too.
- **Fix:** `user_profiles/routes.py` records `user.profile_moderated` (actor: the administrator, target: the member) after each successful change through `admin_profile` (edit, avatar removal, publish, unpublish, disable, enable, delete), `admin_fields` and `admin_tags`, with `action` and, for profile and field edits, `changed`: the names of the profile columns or the labels of the profile fields that changed, never their values. The entry is written when the action succeeded or the profile changed: `edit_profile` saves the real name, biography and birth date before it stores the avatar, so a refused avatar (found in review: bio `I resign. -- bob` with a non-image avatar answered 302, saved the bio and recorded nothing) is recorded with the parts that did change. Labels in en/it/de; documented in `docs/security.md` (Audit log) and UPGRADING.md.
- **Test:** `tests/security/test_q4.py::test_qw3_2_admin_profile_edits_are_audited_without_their_values`, `tests/security/test_q4.py::test_qw3_2_a_profile_edit_with_a_refused_avatar_is_audited`

### QW3-3 The admin error-log viewer (level=all) showed plain administrators the sign-in addresses of owners and superusers (full re-audit pass 11)
- **Severity:** Low. **Attacker:** a plain administrator; the victims are owners and superusers (and other administrators).
- **Repro:** with the default `BW_LOGGING_LEVEL=medium` and the production file handler installed (`_configure_logging(replace(cfg, testing=False))`), owners ownerzed and superzed each failed one sign-in from 203.0.113.45 and 203.0.113.46. Plain admin plainada's `GET /admin/error-log?level=all` answered 200 with `INFO [bananawiki.wiki] Failed sign-in for 'ownerzed' from 203.0.113.45` (and the second line), while `/admin/users/<owner>` and `/admin/audit` hid the address as `docs/security.md` promises. `signin.py` also logs wrong two-step codes with the address. Expected: the addresses of accounts above plain administrators hidden from them in the log view too.
- **Fix:** `site_admin/server.log_tail` takes `addresses`; `/admin/error-log` passes `is_top(auth.real_user())`, and for other viewers `server.hide_addresses` replaces every IPv4 and IPv6 address (validated with `ipaddress`, including IPv4 with a port and IPv4-mapped IPv6; a possessive regex keeps it linear over the 256 KB tail) with `[address]`, in both views, and the page says so (`site_admin.log.addresses_hidden`, en/it/de). The log cannot tell whose address a line holds, so plain administrators lose all addresses there, members' included (they still see members' addresses on the user and session pages). Owners and superusers see the log as before. The log file itself is unchanged. Documented in `docs/operations.md`, `docs/security.md` and UPGRADING.md.
- **Test:** `tests/security/test_q4.py::test_qw3_3_the_error_log_hides_addresses_from_plain_administrators`

### QW3-4 Kanban and canvas imports stored up to 16 MB / 10 MB each, 10 a minute per account, with no daily or total bound (full re-audit pass 11)
- **Severity:** Low. **Attacker:** an account that may create boards and canvases (`kanban.create` / `canvas.create` with global write access; administrators by default).
- **Repro:** editor eve, Kanban and canvas write access `editor`, limiter clock moved forward 61 s between rounds; each round sent 11 x `POST /kanban/import` of a 7.6 MB board (800 tickets of 9,500 characters) and 11 x `POST /canvas/import` of a 4.7 MB canvas (2,000 text nodes): every round 10 x 302 then 429 for both, and the database grew by 255.8, 511.5 and 767.3 MB after rounds 0-2 (5.9 s in all), with no daily or total refusal. Expected: a daily bound per account.
- **Fix:** a daily byte budget per account where content is stored, not only at the import (a first version charged only imports and canvases created with a document; review found that an empty canvas followed by `POST /canvas/<slug>/data` of 1.6 MB, ten of each a round, stored 34 MB a round with nothing charged, and that Kanban boards filled through the ticket API escaped it too). New schema version 19 (`migrations/v19_stored_amounts.py`) adds `rate_limit_hits.amount` (default 1, so every other counter is unchanged), and `SqlLimiter.reserve_amount` (core/ratelimit.py) adds an amount atomically unless that passes the limit, sharing one row a minute per account. Kanban: `transfer._create` charges the imported board's text (`history.board_bytes`) and refuses the import past the budget (429 `kanban.error.daily_storage_limit`); `history.record` charges what each entry adds to its board's history (QW3-1); both against `history.STORED_MIB_PER_DAY` (128 MiB) in 24 hours, bucket `kanban:stored`. Canvas: `service._store` charges what an operation batch, a save or a restore adds to the document and `create` the document a canvas starts with (import, template, `POST /api/v1/canvas` with `data`), refused past `STORED_MIB_PER_DAY` (128 MiB, bucket `canvas:stored`) with `TooMuchStored` (`canvas.error.daily_limit`: 429 from the editor's save and ops routes and from the REST API, `daily_limit`; a message on import, create and restore); `record_history` charges what a new or rewritten entry adds to the canvas's history after trimming. A history entry is never refused: past the budget the oldest entries of that board or canvas make room for it (the newest always stays), so editing is never locked out and a board or canvas whose history is full (Kanban 200 entries / 64 MB, canvas 200 / 32 MB) costs about nothing more. Review of the first version found three problems, fixed here: amounts were counted in whole 64 KiB units rounded down, so every entry or document change under 64 KiB was free (3 small boards and 296 edits stored 9.1 MB in a minute, 10 canvases with a 60 KB save 1.26 MB, with nothing charged: GBs a day); history entries were charged whole and refused past the budget, which locked one person editing one large board out of all boards of 64 KiB or more within 15 minutes to 2 hours; and the account-deletion hand-over charged the deleting administrator, so an administrator out of budget could not delete or merge accounts (now not charged). Amounts are reserved inside the change's transaction, so a refused change costs nothing and is rolled back; deleting boards or canvases, or making a canvas smaller, gives nothing back. So in 24 hours one account adds at most 128 MB of Kanban history and imported text and 128 MB of canvas content and history, and beyond that only the newest entry of a board or canvas it changes (about that board's or canvas's own size). Not covered: Kanban ticket text added by ordinary requests (at most about 10 KB each, each board at most 8 MB), like wiki pages. Moving a wiki to another host drops `rate_limit_hits`, as for every limit kept there. The budgets apply to administrators too. Messages in en/it/de; documented in `docs/features.md`, `docs/api.md`, UPGRADING.md (schema 15 to 19) and CHANGELOG.md.
- **Test:** `tests/security/test_q4.py::test_qw3_4_kanban_imports_are_bounded_per_day_by_their_size`, `tests/security/test_q4.py::test_qw3_4_many_boards_filled_and_edited_through_the_api_share_the_daily_budget`, `tests/security/test_q4.py::test_qw3_4_canvas_imports_are_bounded_per_day_by_their_size`, `tests/security/test_q4.py::test_qw3_4_empty_canvases_filled_by_saves_and_ops_share_the_daily_budget`, `tests/security/test_q4.py::test_qw3_4_rest_canvas_creation_with_data_shares_the_daily_budget`, `tests/security/test_q4.py::test_qw3_4_small_canvases_are_charged_by_the_byte`, `tests/security/test_q4.py::test_qw3_4_entries_of_small_boards_are_charged_by_the_byte`, `tests/security/test_q4.py::test_qw3_4_an_administrator_out_of_budget_can_still_delete_an_account`
### QW4-1 Two accounts could fill a victim's daily email with collaborator notices and block the victim's password reset (full re-audit pass 11)
- **Severity:** Low (reported Medium). **Attacker:** two approved hosting accounts that each own a wiki (one when `HOSTING_EMAIL_ACCOUNT_DAILY_LIMIT` is 0).
- **Repro:** `bananawiki/hosting/email.refusal` counted every message to an address toward its 15 a day and checked that before the recovery caps. Adding someone as a collaborator emails them without their consent, charged only to the acting account, and removing them sends nothing, so it can be repeated. Two accounts ran add and remove cycles on `victim` from several networks. The victim received 15 `collaborator_added` notices. A later anonymous `POST /forgot-password` with the right user name and verified address sent nothing (`recipient_limit`). Verification links and the password and address change notices were refused too. Expected: other accounts cannot use up an address's day, and recovery mail always goes out.
- **Fix:** `bananawiki/hosting/email.py`: password resets, user-name reminders and the `password_changed` and `email_changed` notices are counted apart from the 15, each with its own cap (3, 3, 5 and 5), so other mail can never hold them back. Notices another account causes (`collaborator_added`, `transfer_offered`, `FROM_OTHERS_KINDS`) may use at most 5 of an address's 15. The addition or offer itself still happens. Documented in docs/configuration.md, UPGRADING.md and the *Email budgets* table in docs/hosting.md (its *Per recipient* row had kept the old "of which at most 3 password resets and 3 user-name reminders").
- **Test:** `tests/security/test_q5.py::test_qw4_1_collaborator_notices_cannot_hold_back_a_password_reset`, `tests/security/test_q5.py::test_qw4_1_a_full_inbox_still_gets_recovery_and_credential_notices`, `tests/security/test_q5.py::test_qw4_1_hosting_docs_describe_the_per_recipient_budgets`

### QW4-2 Easy Wiki switches of a paused wiki and administrator password resets had no per-wiki budget (full re-audit pass 11)
- **Severity:** Low. **Attacker:** an approved hosting account that owns a wiki, or a collaborator with `toggle_mode` or `reset_password`.
- **Repro:** with FakeRuntime and the wiki stopped, 12 `POST /instances/<id>/toggle-easy-wiki` requests from 12 networks produced 12 `apply_limits` calls (a quota prepare and a one-shot task container under the runtime agent's global lock). 8 `POST /instances/<id>/reset-password` requests produced 8 `reset_admin_password` runtime calls and 8 events. The only bound was `rate_limit(5)` per client network, so a client with many networks could keep the agent's global lock busy for every tenant. The cost on the agent side was traced in the code (`ops/runtime_agent.py`), not run. Expected: a per-wiki budget like the one PW4-4 gave pauses and restarts.
- **Fix:** `bananawiki/hosting/instances.py`: `set_easy_wiki` now counts as one state change (`count_state_change`) whether or not the wiki is running. `reset_admin_password` takes `charge=` and, for non-administrators, is held to a per-wiki budget of its own (`ADMIN_RESETS_PER_MINUTE` 3 and `ADMIN_RESETS_PER_DAY` 20, bucket `hosting_wiki_admin_reset`), so pausing and resuming cannot use up the owner's way back in. The wiki's current owner is counted on a share of their own (keyed on the wiki and the owner's account); collaborators with `reset_password` share the wiki's other share, and resets a previous owner made before a transfer stay on that account's key. A first version kept one bucket per wiki: the independent review showed a collaborator (or a previous owner just before a transfer) could make 20 resets in under 10 minutes, each giving them a working admin password, and then the owner got `too_many_password_resets_today` for up to 24 hours and could not rotate the password that person still knew. That is fixed (`_count(..., owner_apart=True)`). `routes/dashboard.reset_password` passes the account. Both budgets share a new `_count` helper. New messages `hosting.instances.too_many_password_resets[_today]` are added in en, it and de. Documented in docs/hosting.md and UPGRADING.md.
- **Test:** `tests/security/test_q5.py::test_qw4_2_easy_wiki_switches_of_a_paused_wiki_are_counted`, `tests/security/test_q5.py::test_qw4_2_administrator_password_resets_have_a_per_wiki_budget`, `tests/security/test_q5.py::test_qw4_2_a_collaborator_cannot_spend_the_owners_password_resets`, `tests/security/test_q5.py::test_qw4_2_a_previous_owner_cannot_spend_the_new_owners_password_resets`

### QW5-1 GPU speech server dropped the connection with a traceback on a Content-Length that isdigit() accepts but int() refuses (full re-audit pass 11)
- **Severity:** Low. **Attacker:** anyone holding a valid speech token (a hosted wiki's tenant token, or the master token).
- **Repro:** reproduced again before the fix. `POST /synthesize` with a valid tenant token and `Content-Length: \xb2`, `\xb9\xb2` or `1\xb2` got no answer: http.server decodes headers as Latin-1, `"²".isdigit()` is True and `int("²")` raises ValueError. A run of 5000 digits did the same (int() max-str-digits limit). In each case `socketserver.handle_error` wrote a full traceback to stderr, outside the server's one-line, tenant-tagged log format. Expected: an HTTP error answer and no traceback.
- **Fix:** `contrib/tts-gpu-server/server.py` `_read_request`: the length must match `[0-9]+` (ASCII), otherwise 411. A length of more than 12 significant digits is answered 413 without calling int() on it, and leading zeros are stripped before parsing. The body is read with the parsed size.
- **Test:** `tests/security/test_q5.py::test_qw5_1_speech_server_refuses_a_content_length_int_cannot_read`

### RW2-1 Page-history deletion, clearing and re-attribution leave no audit entry (full re-audit pass 12)
- **Severity:** Low. **Attacker:** Plain administrator, or an editor granted `history.delete` / `history.transfer`.
- **Repro:** An editor saves versions of a page. A plain admin (not impersonating) posts `/page/<slug>/history/<id>/transfer` (to an innocent member), `/history/bulk-transfer`, `/history/<id>/deattribute`, `/history/<id>/delete` and `/history/clear`. Every request answered 302: the edit was credited to the innocent account, then the whole history was gone, and no `audit_log` row was written. A page deletion right after was audited, so the log was working. The same operations done account-wide from Admin -> Users -> Attributions are audited (`user.attributions_moved`, `user.attributions_removed`), so this was an inconsistency. Expected: one entry per action.
- **Fix:** `page_history/routes.py` now records `page.history_entry_deleted` (version id, its author, its date), `page.history_cleared` (count), `page.history_reattributed` (one version or in bulk: previous author, new author, count) and `page.history_deattributed` (version id, previous author). The target is `page`/id, and each entry carries the page title. While impersonating, the entry also names the account acted as (`as_user`); the generic `page.impersonated_action` entry is still written too. An action that changes nothing (unknown user, empty history) records nothing. The labels are in the audit catalogues (en, it, de), and `docs/security.md` (Audit log, Content) lists them. No compatibility impact.
- **Test:** `tests/security/test_r1.py::test_rw2_1_history_deletion_and_reattribution_are_audited`, `tests/security/test_r1.py::test_rw2_1_a_refused_history_change_records_nothing`

### RW2-2 Unlock requests, forced unlocks, check-out releases and assignments, and reservation-quota changes leave no audit entry (full re-audit pass 12)
- **Severity:** Low. **Attacker:** Plain administrator.
- **Repro:** With governance on, an editor protects one page and another editor reserves a second. A plain admin posts `/admin/governance/protection/<id>/request-unlock`. With the request back-dated 73 h, the admin posts `/force-unlock` (`protected_by` became NULL), `/admin/checkouts/<id>/release`, `/admin/checkouts/<id>/assign` with `username=<controller>` (the check-out then named that editor) and `/admin/users/<id>/reservation-quota` with `set_quota unlimited=1` (quota became -1). Every request answered 302 and wrote 0 audit rows, although the HW1-3 text says the unlock request is audited. Expected: an entry for each action.
- **Fix:** The governance services now record the actions, so every route that calls them is covered:
  - `protection.request_unlock` records `page_governance.unlock_requested`, naming the controller. A repeated request, which keeps the first one, records nothing. When an admin who is the controller releases the page, it records `page_governance.protection_removed`.
  - `protection.force_unlock` records `page_governance.protection_removed`, naming the controller, who asked and when.
  - `reservations.force_release` records `page_governance.checkout_released`, naming the holder.
  - `reservations.assign` records `page_governance.checkout_assigned`, with the new holder, any previous holder and the expiry.
  - `reservations.clear_cooldowns` records `page_governance.cooldowns_cleared`.
  - `quota.set_quota` records `page_governance.quota_set` with the old and new stored value (`-1` = unlimited) when the value changes. This covers the admin form, an admin's own quota, and approved requests (`via: request`). Automatic approvals by the auto-approve bound are recorded too (`via: automatic`, with the editor as actor).

  The labels are in en, it and de, and `docs/security.md` (Roles and access) lists them. The contribution quota (`contributions/quota.py`, `/admin/contributions/<id>/set-quota`) is a separate setting and is not covered.
- **Test:** `tests/security/test_r1.py::test_rw2_2_governance_admin_actions_are_audited`, `tests/security/test_r1.py::test_rw2_2_an_unchanged_quota_records_nothing`

### RW3-4 Site-wide announcements created, changed and deleted by a plain administrator leave no audit entry (full re-audit pass 12)
- **Severity:** Low. **Attacker:** A plain administrator (careless or compromised account).
- **Repro:** With audit and announcements on, a plain admin posts `/admin/announcements/create` with `[sign in again here](https://evil.example/login)` and visibility `logged_in`, and the owner's `GET /` shows the link in the banner. The admin then posts `/admin/announcements/<id>/edit` and `/delete`. All three answered 302, no `audit_log` row was written, and the deleted announcement left no row at all. Expected: one entry per change with the administrator as actor, as QW2-5 gave custom pages.
- **Fix:** `announcements/service.py` `create`, `update` and `delete` record `announcement.created`, `announcement.updated` and `announcement.deleted`, with target `announcement`/id. The details are the visibility, the audience mode and the number of named accounts, whether it is active, the expiry, the hosts its links lead to, the first 200 characters, and on an update the columns changed. The hosts are read from the `href`/`src` values of the sanitised HTML the banner shows, the way a browser reads them (entities decoded, tabs and newlines dropped, `\` taken as `/`, any slashes after `http:`/`https:`), plus bare `//` addresses in the source. So `/\evil.example`, `https:\\evil.example` or `&#47;&#9;&#47;evil.example` placed after the excerpt still name their host. The delete entry keeps what the deleted banner said. A form refused by validation records nothing. Pruning of long-expired announcements is not recorded. The same gap in the other admin screens the finding names is closed in their routes too. `badges/routes.py` records `badge.type_created`, `badge.type_updated` (columns changed, previous name), `badge.type_deleted` (holders), `badge.defaults_added` (count), `badge.awarded`, `badge.revoked` (member, permanent or not) and `badge.revoked_all` (count, permanent or not), with target `badge`/id. `user_profiles/routes.py` `admin_definitions` records `profile_field.created`, `profile_field.updated` (previous label and type) and `profile_field.deleted` (how many members' answers went with it), with target `profile_field`/id. Actions that change nothing (unknown member, badge not held, unchanged form) record nothing. Automatic badge awards and reordering profile fields are not recorded. The action and target labels are in en, it and de, and `docs/security.md` (Content) lists them.
- **Test:** `tests/security/test_r1.py::test_rw3_4_announcement_changes_are_audited`, `tests/security/test_r1.py::test_rw3_4_a_refused_announcement_records_nothing`, `tests/security/test_r1.py::test_rw3_4_backslash_and_entity_links_name_their_host`, `tests/security/test_r1.py::test_rw3_4_badge_administration_is_audited`, `tests/security/test_r1.py::test_rw3_4_a_badge_action_that_changes_nothing_records_nothing`, `tests/security/test_r1.py::test_rw3_4_profile_field_definitions_are_audited`
### RW2-3 Bulk Markdown import bounded the unpacked size per archive, not per request (full re-audit pass 12)
- **Severity:** Low. **Attacker:** Plain administrator (bulk Markdown import is admin-only).
- **Repro:** Several ZIPs of 1 MiB members (each archive under the 256 MiB unpacked bound, members of exactly 1 MiB escape the ratio check) in one POST /admin/bulk-markdown: about 0.8 MB of upload held 720 MiB of decoded text at once before import_files started (peak RSS 129 MB to 851 MB; 1.1 MB of upload reached 1 GB). Expected: the request's total unpacked size bounded by MAX_TOTAL_BYTES.
- **Fix:** `read_uploads` in `bananawiki/wiki/features/page_export/bulk.py` keeps a running total over all archives (declared sizes of their Markdown members, which a member never unpacks past) and plain `.md` files, and passes what is left of `MAX_TOTAL_BYTES` to `_check_archive`; the archive or file that would take the total past 256 MiB is refused with the existing "{name} is too large once unpacked." message, the ones before it are imported. docs/features.md states the per-import bound.
- **Test:** `tests/security/test_r2.py::test_rw2_3_bulk_import_bounds_the_unpacked_total_of_the_request`

### RW2-4 The check-out directory checked edit rights for every visible page on each view (full re-audit pass 12)
- **Severity:** Low. **Attacker:** Any editor, with page governance and reservations on (off by default).
- **Repro:** With 40,000 pages, GET /reservations (any `?page=`) ran `pages.can_edit` 40,000 times and took about 0.44 s of CPU against 0.08 s for /, then showed 100 rows; back-to-back requests were only held by the global 300 a minute limit.
- **Fix:** `directory()` in `bananawiki/wiki/features/page_governance/routes.py` applies `pages.can_edit` in SQL (`visible_filter`, not pending deletion, not the home page, `page.edit_all` for editors and their writable categories), counts with `COUNT(*)` and reads one window with `LIMIT/OFFSET` in the same title order; the route also gets `rate_limited("reservations", 30)`.
- **Test:** `tests/security/test_r2.py::test_rw2_4_directory_lists_editable_pages_one_window_at_a_time`, `tests/security/test_r2.py::test_rw2_4_directory_has_its_own_rate_limit`

### RW3-1 Every page view of a reviewer read the full text of every pending proposal (full re-audit pass 12)
- **Severity:** Low. **Attacker:** Approved plain users with `contribution.propose` (on by default), using several accounts; victims are reviewers' page views and server memory.
- **Repro:** 6 users x 5 proposals of 990,000 characters: a non-admin reviewer's GET /settings loaded 29.7 MB of proposal text for the nav badge (`pending_for_reviewer` selected `c.*` of every pending proposal), and an administrator's view of a page loaded the 5.9 MB of that page's proposals for the notice (`pending_on_page`), though no text is shown. Expected: the badge and the notice never read proposal texts.
- **Fix:** `bananawiki/wiki/features/contributions/service.py`: `pending_on_page` and `pending_for_reviewer` select a summary (ids, user, status, reason, dates, user name, page title and slug) without the title and content; `review_count` and `oldest_for_reviewer` count and date per page in SQL (`GROUP BY page_id`) and check each page once. The review list and the notices show the same as before.
- **Test:** `tests/security/test_r2.py::test_rw3_1_page_views_of_reviewers_never_load_proposed_texts`
### RW3-3 Deleting an account leaves the files of all its direct conversations on disk for up to two days (full re-audit pass 12)
- **Severity:** Low. **Attacker:** n/a (privacy and deletion flow; affects both participants of every direct conversation of a deleted account).
- **Repro:** alice and bob exchange direct messages with attachments (`POST /chats/<id>/send`), then alice deletes her account (`POST /settings/delete`; the same through `accounts.delete`, used by administrator and scheduled deletion). Observed: the `chats`, `chat_messages` and `chat_attachments` rows were gone (`ON DELETE CASCADE`), but both files, bob's included, were still in `chat_attachments/` with their contents, as were legacy `file_blobs` copies; `chat.housekeeping` only removes orphans older than 24 hours and runs daily, so they stayed for up to about 48 hours and went into every backup and whole-site export made meanwhile. An account merge deleted the pair's conversation the same way, and so did the hosting portal's "remove wiki user" task (`tenant_task.remove_user`, from `hosting/admin.py cmd_instance_remove_user`), which deletes the account without the wiki application or `registry.prepare`. Expected: the files go once the deletion commits, as for wiping, clearing, retention and group deletion.
- **Fix:** the chat feature's `user.delete` interceptor now also calls `dms.forget_conversations`, which erases each of the account's conversations with `store.clear` (messages, attachment rows and blob rows inside the deleting transaction) and hands the removal of the files to the new `Session.after_commit` (`bananawiki/core/sqlite.py`): the callback runs once the outermost transaction commits and is dropped when it, or the savepoint it was registered in, rolls back. Because `registry.prepare` runs the interceptors of built-in features whether they are switched on or not, the files also go while chat is switched off or hidden by BM mode (a first version removed them from a `user.deleted` handler, which `registry.emit` skips for a disabled feature, and the orphan job does not run then either, so those files would have stayed indefinitely). `users/merge._prepare_special_tables` erases the pair's conversation with the new `dms.erase_between` and the merge removes its files after its transaction, whether the source is deleted or locked. `tenant_task.remove_user` does the same itself, as it already copies the kanban, canvas and group-address handling: `_erase_conversations` deletes the account's conversations and their `file_blobs` rows in its transaction, and `_remove_chat_files` removes the files from `cfg.folders.chat_attachments` (names leading outside the folder skipped) through `Session.after_commit`. So every deletion path (own, administrator, scheduled, merge, hosting portal) removes the files when it commits. `docs/features.md` says the files go as soon as the deletion is done, whoever deletes the account, the hosting portal included; `ARCHITECTURE.md` documents `db.after_commit`.
- **Test:** `tests/security/test_r3.py::test_rw3_3_own_deletion_removes_both_participants_files`, `tests/security/test_r3.py::test_rw3_3_shared_deletion_path_removes_the_files`, `tests/security/test_r3.py::test_rw3_3_merge_removes_the_pairs_conversation_files`, `tests/security/test_r3.py::test_rw3_3_files_go_while_chat_is_switched_off_or_hidden_by_bm_mode`, `tests/security/test_r3.py::test_rw3_3_a_rolled_back_deletion_keeps_the_files`, `tests/security/test_r3.py::test_rw3_3_a_rolled_back_savepoint_keeps_its_files_when_the_outer_transaction_commits`, `tests/security/test_r3.py::test_rw3_3_removing_a_wiki_user_from_the_hosting_portal_removes_the_files`
### RW4-1 A previous owner keeps full control of a transferred wiki through a full-access collaborator added before the offer (full re-audit pass 12)
- **Severity:** Low (reported Medium). **Attacker:** Approved hosting user who owns a wiki and controls a second approved account, offering the wiki to someone else.
- **Repro:** oldowner added sockpuppet as full_access and offered the wiki to newowner. newowner accepted with previous_owner=none (302, owner changed). The sockpuppet row survived, and the accept form had not listed it. sockpuppet then added oldowner as a custom collaborator with all ten permissions (302, allowed because full_access counts as holding every permission). oldowner's `POST /instances/<id>/reset-password` answered 200 with a new administrator password. Expected: after "No access", the previous owner gets no access, directly or through someone they added.
- **Fix:** `collaborators.incoming_transfers` now lists the wiki's other collaborators on each offer, each with a `keep` value (`keep_token`: a digest of account, role and permissions). The accept form (`hosting/_transfers.html`) shows each one with their access as an unticked checkbox. `accept_transfer(..., keep=...)` removes every collaborator the recipient did not tick, inside the transaction that changes the owner (the `guard` of `instances.move_to_owner`), and records `collaborator.removed` for each one. So nobody left over can act between the move and the clean-up. A tick names the access that was shown, so a collaborator whose access the previous owner raised after the offer was seen is removed too. The route passes `request.form.getlist("keep")`. New string `hosting.transfers.team` in en/it/de. docs/hosting.md and UPGRADING.md say that collaborators who are not ticked are removed. Compatibility: a recipient who wants to keep the team ticks them, or adds them again afterwards. A client that posts the accept form itself keeps nobody unless it sends `keep`. An administrator's **Move now** is left as it was (a deliberate administrator action).
- **Test:** `tests/security/test_r4.py::test_rw4_1_previous_owners_collaborators_do_not_survive_a_transfer`, `tests/security/test_r4.py::test_rw4_1_recipient_keeps_the_collaborators_they_tick`, `tests/security/test_r4.py::test_rw4_1_a_tick_does_not_keep_access_raised_after_the_offer_was_shown`

### RW4-2 Custom-domain Verify runs the DNS check with no overall deadline and no per-wiki budget (full re-audit pass 12)
- **Severity:** Low. **Attacker:** Approved owner of a running wiki allowed a custom domain, who controls the claimed domain's nameserver.
- **Repro:** A local nameserver dropped each first query and answered the retry just inside the 4 s lifetime, with a TXT proof that never matches. `dnscheck.check` took 11-23 s (six lookups, each with its own lifetime) and returned no DNS error. Eight `action=verify` POSTs from eight addresses each held a portal thread for that long, and none was refused per wiki. `action=check` was refused after 6. Expected: Verify counts in the per-wiki check budget, and one deadline bounds all its lookups.
- **Fix:** `routes/dashboard.py` `instance_domain`: `action=verify` now spends `limits.hit("hosting_domain_check", DOMAIN_CHECKS_PER_MINUTE, 60, key=inst["id"])`, as `action=check` and the page's poll do, and flashes `hosting.domains.check_wait` past it. `runtime/dnscheck.check` gives all its lookups one `CHECK_SECONDS` (6 s) deadline, as `inspect` does. Each lookup's lifetime is `min(LIFETIME_SECONDS, time left)`, and running out counts as a DNS error ("not visible yet"), so the maintenance pass's `domains.refresh` is bounded too. docs/hosting.md and UPGRADING.md are updated.
- **Test:** `tests/security/test_r4.py::test_rw4_2_lookups_of_one_check_share_one_deadline`, `tests/security/test_r4.py::test_rw4_2_verify_counts_against_the_wikis_dns_check_budget`

### RW4-3 A non-ASCII request target on a wiki host that falls through to the portal raises out of the proxy (full re-audit pass 12)
- **Severity:** Low. **Attacker:** Anonymous visitor of a wiki host whose requests reach the portal's fallback proxy.
- **Repro:** Gunicorn accepts `GET /\xe9x HTTP/1.1` and sets RAW_URI to `'/éx'` (Latin-1). `wikihosts._target` returned it unchanged. `http.client.putrequest` raised UnicodeEncodeError, which only the `except BaseException: ... raise` branch caught, so each such request got a 500 and a full traceback. `/plain` was proxied normally. Expected: the target percent-encoded, or the status page.
- **Fix:** `bananawiki/hosting/wikihosts.py`: the new `_ascii` percent-encodes every character outside printable ASCII in the forwarded RAW_URI, and in the fallback query string, as the bytes the client sent (Latin-1, or UTF-8 beyond it). So `/caf\xe9` reaches the wiki as `/caf%E9`, and ASCII targets are forwarded unchanged. `_proxy` also treats a `ValueError` from http.client like the other upstream failures: it logs a warning and shows the "bad gateway" status page.
- **Test:** `tests/security/test_r4.py::test_rw4_3_non_ascii_request_target_is_percent_encoded`, `tests/security/test_r4.py::test_rw4_3_a_target_http_client_refuses_gets_the_status_page`

### SW1-1 Maintenance mode kept members out but let anonymous visitors read the public wiki (full re-audit pass 13)
- **Severity:** Low. **Attacker:** Anonymous visitor (or any member after signing out) of a wiki in public mode.
- **Repro:** With public mode and maintenance mode on, a signed-in plain user got 302 to /maintenance, but anonymous GET /, /page/<slug>, /search?q=<word> and JSON requests answered 200 with the page content, because `_pipeline` returned early for anonymous `public_read` views before the maintenance gate. docs/security.md and docs/operations.md promise that maintenance lets only administrators in. Expected: anonymous public readers also get the maintenance page.
- **Fix:** `bananawiki/wiki/app.py` `_pipeline`: the anonymous `public_read` path now applies the maintenance gate (redirect to `auth.maintenance`, JSON 503 `maintenance`) unless the view is exempt from it; the gate's response is shared with the signed-in path (`_maintenance`). `public` views (sign-in pages, published custom pages, the API guide, the maintenance page) stay open, but the shell around them listed the page tree (titles, slugs, categories) and a builder page's page lists to anonymous public-mode visitors and to signed-in members (found in review). While maintenance is on, the pipeline now serves a non-administrator on a public view that is not exempt from maintenance as an anonymous visitor of a private wiki: `settings.close_public_mode()` makes `public_mode_active()` false for the request and a signed-in member is not signed in for it (as refused accounts already are); error pages do the same. Administrators keep the whole shell. docs/operations.md and UPGRADING.md say so.
- **Test:** `tests/security/test_s1.py::test_sw1_1_maintenance_keeps_anonymous_public_readers_out`, `tests/security/test_s1.py::test_sw1_1_public_views_show_no_wiki_content_during_maintenance`

### SW3-1 A group's custom invite-code form revealed other groups' codes past the join-guess limit (full re-audit pass 13)
- **Severity:** Low. **Attacker:** Any approved user who may create a group chat.
- **Repro:** alice set the custom code smith2024 on her private group. mallory used up her join budget (10 x 404, then 429), created her own group and sent custom codes to POST /groups/<hers>/regenerate_code: free codes answered 200 and smith2024 answered 409 "code taken", limited only by the per-worker action throttle (30 a minute) and spending no join budget. A second account then joined with smith2024 and read the group history. Expected: checking codes this way costs as much as joining.
- **Fix:** `bananawiki/wiki/features/chat/routes_groups.py`: each custom code tried on `group_regenerate_code` spends one hit from the same `SqlLimiter` budgets as `group_join` (`group_join` per account, `group_join_ip` per network, shared helper `_spend_code_guess`); when they are used up it answers 429 `join_rate_limited`. Random regeneration is not charged. Compatibility: an owner trying many custom codes is limited to 10 per 15 minutes. docs/security.md and UPGRADING.md say so.
- **Test:** `tests/security/test_s1.py::test_sw3_1_custom_invite_code_checks_spend_the_join_budget`
### SW2-1 A page rename loads the full text of every matching editor draft into memory at once (full re-audit pass 13)
- **Severity:** Medium. **Attacker:** Editor (default `draft.create`).
- **Repro:** One editor saved drafts of about 1 MB containing `/page/t` on 700 pages (`POST /api/draft/save` answered 200 `saved`; drafts have no count bound and do not expire by default). One `POST /page/t/rename` with `new_slug=t2` answered 302 and grew the worker's RSS by 690 MB in that request, because `_rewrite_links` selected the content of every matching draft in one `db.all()`. The rename rewrote the drafts to `/page/t2`, so renaming back matched all of them again. With 150 drafts of 0.5 MB, the independent check saw one 75 MB result list (tracemalloc peak 76 MB). About 700 editable pages are enough to get a 768 MB hosted container OOM-killed on each rename. `rewrite_mentions` (account merges, drafts containing `@name`) had the same pattern. Expected: memory bounded by one draft.
- **Fix:** `pages/service.py`: `_rewrite_links` and `rewrite_mentions` now select only the ids (and page ids) of the matching drafts. The new `_rewrite_draft` reads, rewrites and writes each draft by id, one at a time. The per-page `_may_rewrite` check is unchanged. Draft count and size per account stay unbounded, as before; with this fix and SW2-3 that no longer makes one request hold them all. No compatibility impact.
- **Test:** `tests/security/test_s2.py::test_sw2_1_rename_rewrites_drafts_one_at_a_time`, `tests/security/test_s2.py::test_sw2_1_account_merge_rewrites_drafts_one_at_a_time`

### SW2-2 A refused category deletion first loads every page of the category with its full text (full re-audit pass 13)
- **Severity:** Low. **Attacker:** Editor (defaults: `category.delete` yes, `page.delete` no). The same code is behind REST `DELETE /api/v1/categories/<id>` and bulk deletion.
- **Repro:** A default editor filled the category "Mine" with 700 pages of 998,000 characters. `POST /category/<id>/delete` with `page_action=delete` answered 302 with the flash "The category was not deleted. You may not delete these pages: ...". Nothing changed, but the worker's RSS grew by 683 MB during the attempt, because `delete_with_pages` ran `SELECT *` (content and `builder_json`) on every page before its permission checks. The refusal can be repeated. Independent check: 200 pages of 1 MB gave a tracemalloc peak of 201 MB on each refused attempt, and about 0 MB for a category of one-character pages. Expected: the refusal costs ids and flags, not texts.
- **Fix:** `pages/categories.py`: `delete_with_pages` checks the pages using `service.PAGE_COLUMNS`, which leaves out the text (both branches). A page that is then deleted is read in full right before its `page.delete` interceptors and `page.deleted` handlers run, one page at a time, so handlers and 1.4 plugins still get the whole page. The system's own `delete(page_action="delete")` also reads one page at a time. No compatibility impact.
- **Test:** `tests/security/test_s2.py::test_sw2_2_refused_category_deletion_does_not_load_the_texts`, `tests/security/test_s2.py::test_sw2_2_category_deletion_with_pages_still_deletes_them`

### SW2-3 'My drafts' measures the full text of every draft on each view and lists them all unpaged (full re-audit pass 13)
- **Severity:** Low. **Attacker:** Editor who plants the drafts (see SW2-1). They pay the cost on each of their own `GET /drafts` (which had no per-route limit) and `GET /api/draft/mine` (60/min).
- **Repro:** An editor had 700 drafts of about 1 MB, not ASCII. Each `GET /drafts` answered 200 in 0.29-0.37 s of CPU, against 0.05-0.10 s for 700 two-character drafts. `/api/draft/mine` took 0.26 s, against 0.014 s. The extra cost came from `length(d.content)` in `drafts.service.of_user`, which made SQLite read and decode every draft in full, and the list had no limit. 30 to 40 back-to-back views all answered 200, which is about 90 s of CPU a minute from one account. Expected: the cost bounded by the drafts shown.
- **Fix:** `drafts/service.py`: `of_user` no longer computes the size, which nothing displayed, and takes `limit`/`offset`. `drafts/routes.py`: `/drafts` shows 50 drafts a page with the shared pagination macro and is limited to 30 views a minute per account (`drafts-list`). `/api/draft/mine` is a 1.4 API that returns a plain list, so it still lists every draft, but no longer reads their text. `docs/features.md` mentions the paging. No compatibility impact.
- **Test:** `tests/security/test_s2.py::test_sw2_3_my_drafts_is_paged_and_does_not_measure_the_texts`, `tests/security/test_s2.py::test_sw2_3_my_drafts_is_rate_limited`

### SW2-5 Taking over another account's private draft leaves no audit entry, also while impersonating (full re-audit pass 13)
- **Severity:** Low. **Attacker:** Plain administrator (always holds `draft.transfer`), or an editor granted `draft.transfer`.
- **Repro:** An editor saved the draft "PRIVATE unsaved: salary list" on Notes. A plain admin posted `/api/draft/transfer` with `{page_id, from_user_id}` and got 200. The admin's `/api/draft/load/<id>` then returned the editor's text, and the editor's own draft was gone. The audit log gained 0 rows, and the form endpoint `/drafts/<page_id>/transfer` behaved the same. While impersonating an editor granted `draft.transfer`, the transfer also wrote nothing, because the drafts endpoints were not in the impersonation audit lists. Expected: an entry for each take-over.
- **Fix:** `drafts.service.transfer`, which serves both routes, now records `draft.taken_over`. The target is `page`/id. The entry names the page title, the previous holder (`from`), the new holder (`to`) and the draft's size, never its text. While impersonating, it also names the account acted as (`as_user`). A refused take-over (no draft, or the caller's own draft) records nothing. `drafts.api_transfer` and `drafts.transfer` were added to `_IMPERSONATION_AUDITED_ENDPOINTS`, so `page.impersonated_action` is written too. The label is in the audit catalogues (en, it, de), and `docs/security.md` (Audit log, Content) and `docs/features.md` say so. The previous holder is not notified. No compatibility impact.
- **Test:** `tests/security/test_s2.py::test_sw2_5_draft_takeover_is_audited`, `tests/security/test_s2.py::test_sw2_5_draft_takeover_while_impersonating_is_audited`
### SW2-4 Bulk Markdown import's 256 MiB bound counted bytes while the texts were held as str (full re-audit pass 13)
- **Severity:** Low. **Attacker:** plain administrator (bulk Markdown import is admin-only).
- **Repro:** An admin POSTed /admin/bulk-markdown with plain `.md` files of 4,000,000 bytes (the per-file maximum), each starting with U+1F600. `read_uploads` counted 256,000,000 bytes for 64 files, under `MAX_TOTAL_BYTES`. CPython stores a str with any character above U+FFFF at 4 bytes per character, so the decoded texts took 1,024,003,072 bytes (about 1 GiB) at once, and the request was accepted (302). ASCII-only files held the same as they counted. Expected: the bound applies to what the request holds.
- **Fix:** `bananawiki/wiki/features/page_export/bulk.py`: each file now counts as `max(len(raw), sys.getsizeof(text))` (`_held`) against the request's bound. A loose file whose decoded text would pass the bound is refused with `too_large_unpacked`, and the files before it are kept. In an archive, the declared-size check still runs first. The decoded members are then counted as they are read, and the whole archive is refused once they pass what is left of the bound. The peak while reading is the bound plus one file (at most 16 MB). docs/features.md says how the bound is counted. No compatibility impact: ASCII and BMP text counts as before.
- **Test:** `tests/security/test_s3.py::test_sw2_4_bulk_import_counts_decoded_text_against_the_bound`

### SW3-4 An administrator deleting a member's Kanban board or canvas, or deleting or clearing its history, left no audit entry (full re-audit pass 13)
- **Severity:** Low. **Attacker:** plain administrator (by design, administrators have full access to every board and canvas).
- **Repro:** `owner1` created a private board with columns and a canvas with history. A plain admin POSTed `/kanban/<id>/history/<eid>/delete`, `/kanban/<id>/history/clear`, `/canvas/<slug>/history/<eid>/delete`, `/canvas/<slug>/history/clear`, `/kanban/<id>/delete` and `/canvas/<slug>/delete`. All six answered 302, and the history, the board and the canvas were gone. No audit_log row was written. Bulk management deletions of the same objects were audited (`bulk.deleted`). Expected: entries naming the administrator, as `page.history_*` and `page.deleted` give.
- **Fix:** `bananawiki/wiki/features/audit/handlers.py` subscribes to the existing `kanban.board.deleted` and `canvas.deleted` events and records `kanban.board_deleted` and `canvas.deleted` with the title, creator and visibility. This covers the object's own page, the canvas REST API and bulk management (where it is recorded next to `bulk.deleted`). `kanban/views.py` and `canvas/routes.py` record `kanban.history_entry_deleted`, `kanban.history_cleared`, `canvas.history_entry_deleted` and `canvas.history_cleared`. These carry the entry, its editor and its time, or the count removed, plus the account acted as while impersonating, like `page.history_*`. `canvas.service.clear_history` now returns the count. Deletions are recorded whoever deletes, creators included, as page deletions are. Labels are in en, it and de, and docs/security.md lists the entries. No compatibility impact, and no migration.
- **Test:** `tests/security/test_s3.py::test_sw3_4_admin_deleting_a_board_or_canvas_or_its_history_is_audited`
### SW3-2 Temporary-item schedules are never audited, so the log names nobody for account deletions, demotions and page hiding (full re-audit pass 13)
- **Severity:** Low. **Attacker:** Plain administrator (careless or compromised), with the `temporary_accounts` feature on.
- **Repro:** A plain admin posted `/admin/temporary/user`, `/admin/temporary/role`, `/admin/temporary/visibility` and `/admin/temporary/user/<id>/remove`. Every request answered 302 and the page was hidden at once, but no audit rows were written. With the expiry dates moved into the past, the `temporary_accounts.expire` job wrote `user.deleted` and `user.role_changed` with `actor_id` NULL, and it deleted the schedule rows that held `set_by`. No entry named the admin. Expected: the same attribution as the manual change, which records the acting account.
- **Fix:** `temporary_accounts/service.py` now records three actions. `temporary.scheduled` is written when an administrator sets a schedule, with the kind, target account or page, expiry, roles or visibility. `temporary.removed` is written when an administrator removes one; the job's own removals are not recorded. `temporary.carried_out` is written just before the job deletes, demotes or switches visibility back, inside the same transaction. Its actor is the schedule's `set_by`, with `via: temporary_accounts.expire`. The job's `user.deleted`/`user.role_changed`/`page.deleted` events are unchanged. Their `deleted_by` would change who inherits kanban boards and canvases. The job's `role_history` row keeps `changed_by` NULL, because `_changed_by_hand_since` relies on it. Labels are in en/it/de, and docs/security.md and docs/features.md are updated.
- **Test:** `tests/security/test_s4.py::test_sw3_2_schedules_and_their_removal_name_the_administrator`, `tests/security/test_s4.py::test_sw3_2_the_job_names_whoever_set_the_schedule`

### SW3-3 Federation pairing, sync, disconnect and page sharing leave no audit entry, unlike webhooks (full re-audit pass 13)
- **Severity:** Low. **Attacker:** Plain administrator (pairing, sync, disconnect, fork); editor-level account (sharing); only where `BW_FEDERATION_ENABLED=1`.
- **Repro:** A plain admin, with federation and audit on, sent `POST /admin/federation`, `POST /federation/sharing` (share, unshare, share again), `POST /admin/federation/<peer>/sync` and `POST /admin/federation/<peer>/delete`. All answered 302 and the peer and share rows were created and then removed, but no audit rows were written. Expected: auditing like webhooks (OW1-2), since a pairing plus shares is a standing outbound channel for the full text of the shared pages.
- **Fix:** `federation/store.py` now records `federation.paired` with the wiki id, name, host only (never the pairing key), readers' category and local-network switch. It also records `federation.disconnected` with the shares and copies removed, and `federation.page_shared`, `federation.page_unshared` and `federation.forked`. The *Sync now* route records `federation.synced` with whether a snapshot was stored. The poll job's runs and the grants a snapshot withdraws by itself are not recorded. Labels are in en/it/de, and docs/federation.md and docs/security.md are updated. No compatibility impact.
- **Test:** `tests/security/test_s4.py::test_sw3_3_federation_actions_are_audited`
### SW4-1 Refused codes sent with a wiki's own credentials still open a write transaction (BEGIN IMMEDIATE) once over the BAD_GRANT limit (full re-audit pass 13)
- **Severity:** Low (lock contention by volume). **Attacker:** hosting tenant whose code can read the wiki's OAuth client secret (tenant plugins, or a compromised wiki).
- **Repro:** `BAD_GRANT_PER_MINUTE` patched to 3. 200 `POST /oauth/token` with the wiki's Basic credentials and `code=bogus-code` gave 400 x3, then 429 x197, and opened 200 top-level `BEGIN IMMEDIATE` transactions (`redeem_code` took the portal-wide write lock before looking the code up, and before `limits.failure`). While another connection held the write lock, an over-limit bogus code waited 1.5 s for it. A first fix (skip the transaction when no code row matches) left real codes open, as the fix review showed: one real, unused code (the tenant gets one whenever anyone signs in to its wiki) sent 50 times with `redirect_uri=https://evil.example/cb` (never consumed, so reusable until it expires after 300 s) gave 400 x3, 429 x47 with 50 transactions; after a valid exchange, 50 replays gave 429 x50 with 50 transactions, each also running a `DELETE` on `hosting_oauth_access_tokens`. These requests are exempt from the global per-network limit and from `client_call`, so nothing bounded them. Expected (as IW2-2 promised): over the limit a refusal costs a read only.
- **Fix:** `bananawiki/hosting/oauth.py`: `redeem_code` runs every refusal check as plain reads first (`_code_row`, `_refused`): no such code, the wrong redirect URI or PKCE verifier, an account that may not sign in, or a replayed code whose wiki tokens for that account are already gone. Only a code that will be exchanged, or a replay that still has tokens to revoke, opens the transaction, where everything is checked again. Now the same 50 wrong-redirect requests open no transaction and 50 replays open one (the first, which revokes the token). Valid codes are always exchanged (HW2-1 unchanged) and a replayed code still revokes the wiki's tokens for that account (RFC 6749 4.1.2). A replay can take the lock again only once per new token, and each new token needs a real sign-in. No protocol change. The route-level `limits.exceeded` check was not used: it would refuse valid codes over the limit, or need the same lookup anyway.
- **Test:** `tests/security/test_s5.py::test_sw4_1_made_up_codes_take_no_write_lock`, `tests/security/test_s5.py::test_sw4_1_refused_and_replayed_real_codes_take_no_write_lock`

### SW4-2 Every Verify of an already verified custom domain republishes the routing table, with no daily bound (full re-audit pass 13)
- **Severity:** Low (extra work under the routes lock and the runtime agent's global lock; no reload, no integrity issue). **Attacker:** approved owner of a running wiki allowed a custom domain, with its DNS records in place.
- **Repro:** policy *everyone*, FakeRuntime answering the DNS check as correct. After claiming and verifying `docs.example.org`, 6 `POST /instances/<id>/domain action=verify` a minute from 6 addresses for 30 simulated minutes made 179 `sync_routes` calls, all with one identical table. Each takes the host-wide `.routes.lock` and sends `proxy.routes`, which lists every tenant under the agent's global lock (traced, no Docker): about 8,600 a day per wiki. Expected: a renewal that changes no route publishes nothing.
- **Fix:** `bananawiki/hosting/domains.py` `verify`: reads `active_domain` before the update and calls `instances.sync_routes()` only when the domain was not served before (a first verification, or a lapsed proof coming back). A renewal only moves `verified_until` on; the maintenance pass publishes the table at its end anyway. The takeover path is unchanged.
- **Test:** `tests/security/test_s5.py::TestSW42::test_sw4_2_reverifying_an_active_domain_publishes_no_routes`

### SW4-3 Opening a paused wiki's page (or its analytics) starts a one-shot task container under the agent's global lock, 26 times a minute per wiki with no daily budget (full re-audit pass 13)
- **Severity:** Low (load on the runtime agent's global lock). **Attacker:** approved hosting user who owns a wiki (account list on the wiki page); for analytics, any collaborator with `analytics`.
- **Repro:** the owner paused the wiki, then each simulated minute made 20 `GET /instances/<id>` and 6 `GET /instances/<id>/analytics?days=365` (limiter rows cleared between minutes). All answered 200; after 60 minutes FakeRuntime recorded 1,200 `list_users` and 360 `analytics` tasks against the paused wiki. On a stopped wiki each is a one-shot container started under the agent's global lock (traced in `ops/runtime_agent.py`), about 37,000 a day per wiki. Expected: a per-wiki daily budget, as QW4-2 gave Easy Wiki switches of a paused wiki.
- **Fix:** `bananawiki/hosting/routes/dashboard.py`: a new `_wiki_task` helper keeps the per-minute buckets and, while the wiki's container is down (the wiki not running, or held offline in port and onion mode, see SW4-4), also counts the account list and analytics in one per-wiki daily bucket (`PAUSED_TASKS_PER_DAY` 100, `hosting_wiki_paused_tasks`). Past it the wiki page shows without the account list and analytics say "unavailable", as the per-minute limits already do (no new strings). Running wikis that are not held (a `docker exec`, no global lock) keep the per-minute limits only. A wiki marked running whose container crashed is not detected per view (that would cost a status call each time); maintenance restarts it. Documented in docs/hosting.md and UPGRADING.md.
- **Test:** `tests/security/test_s5.py::test_sw4_3_tasks_on_a_paused_wiki_have_a_daily_budget`, `tests/security/test_s5.py::TestSW44::test_sw4_3_tasks_on_a_held_wiki_have_the_daily_budget`

### SW4-4 In port mode, a suspended account's wikis (or those of an account scheduled for deletion) stay online (full re-audit pass 13)
- **Severity:** Low (moderation does not take content offline in port and onion mode). **Attacker:** owner of a moderated account, and every visitor of their wikis meanwhile.
- **Repro:** `HOSTING_MODE=port`, FakeRuntime, owner with a running wiki on port 6001. An administrator's `POST /admin/accounts/<id>/suspend` (permanent, without *Also suspend their wikis*) or `/schedule-deletion` left the wiki row `running` and the container running (its port published); after the test stopped it, `maintenance.run_once` started it again. Only routing took such wikis offline, and outside subdomain mode the agent's `sync_routes` does nothing. Expected: as in subdomain mode, the wikis are not served.
- **Fix:** `bananawiki/hosting/instances.py`: new `held(inst)`: outside subdomain mode a wiki that `domains.may_serve` refuses (account suspended, denied or scheduled for deletion, wiki expired) is not run. `sync_routes` (called at once by account suspension, scheduled deletion and their reversal, and by every maintenance pass) now stops the containers of such wikis that are up (running, starting or unhealthy; a `missing` or `unknown` one is not sent a stop on every route publication); `_keep_only_if_running` (after every start, restart, policy change and content reset) stops one that was just brought up; `maintenance._start_running` skips them. Nothing brings a held wiki's container up even for the length of a restart (found in the fix review: a restart, Easy Wiki switch or content reset by the owner of an account scheduled for deletion, or a collaborator of a suspended owner's wiki, put it online for that moment, up to the state-change budget of 100 a day): `restart` refuses a held wiki (`hosting.instances.cannot_modify`, an existing message), `apply_policy` applies the limits without restarting it, `reset_content` resets without starting it, and `start` only marks it running. The row stays `running`, so after an unsuspension or a cancelled deletion the next maintenance pass starts the wiki again. Subdomain mode is unchanged (the container stays up, unrouted). Documented in docs/hosting.md and UPGRADING.md.
- **Test:** `tests/security/test_s5.py::TestSW44::test_sw4_4_a_moderated_accounts_wikis_go_offline_in_port_mode` (suspend and schedule-deletion), `tests/security/test_s5.py::TestSW44::test_sw4_4_no_operation_brings_a_held_wiki_up`, `tests/security/test_s5.py::TestSW44::test_sw4_4_route_publication_sends_no_stop_to_a_wiki_that_is_not_up`

### SW4-5 Collaborator add, update and remove can run in a loop, each request adding a moderation-log row kept 10 years (full re-audit pass 13)
- **Severity:** Low (unbounded growth of `hosting_events`, pushing real entries off /admin/moderation). **Attacker:** approved hosting user who owns a wiki (or a collaborator with `manage_collaborators`) and a second account to add.
- **Repro:** 150 cycles of `POST /instances/<id>/collaborators/add` (custom, view), `.../<friend>/update` (same permissions) and `.../<friend>/remove` across 30 addresses: all 302, and `hosting_events` grew by exactly 450 rows. The only brake was `rate_limit(15)` per network and route (about 64,800 rows a day per network). The traced sibling, domain claim and remove, wrote `domain.claimed`/`domain.removed` the same way with no DNS needed. Expected: churn bounded per wiki, no entry for an unchanged update.
- **Fix:** `bananawiki/hosting/collaborators.py`: `update` writes and records nothing when the role and the effective access are unchanged (compared as permission sets, `view` always included, so `[]` and `["view"]` are the same). Adding a collaborator, and an update that widens access (more permissions, or full access), count against a per-wiki budget of `MAX_GRANTS_PER_DAY` (100) in 24 hours (`hosting_collaborator_grants`, administrators not counted), inside the add's transaction so a refused add costs nothing. `bananawiki/hosting/domains.py` `claim`: a wiki claims at most `MAX_CLAIMS_PER_DAY` (20) domains in 24 hours (`hosting_domain_claims`); re-claiming the wiki's own domain is not counted. `remove` records `domain.removed` only when it deleted a binding or claim. Taking access away, removing a collaborator and removing a domain are never refused: each collaborator or domain removal entry follows a counted add or claim, and each recorded narrowing takes away at least one permission (or full access) that a counted add or widening gave, so at most about ten narrowings follow each counted grant. New messages `hosting.collaborators.too_many` and `hosting.domains.too_many_claims` in en, it and de. Documented in docs/hosting.md and UPGRADING.md.
- **Test:** `tests/security/test_s5.py::test_sw4_5_collaborator_churn_is_bounded_per_wiki`, `tests/security/test_s5.py::test_sw4_5_collaborator_update_compares_effective_access`, `tests/security/test_s5.py::TestSW45Domains::test_sw4_5_domain_claim_churn_is_bounded_per_wiki`, `tests/security/test_s5.py::TestSW45Domains::test_sw4_5_removing_no_domain_records_nothing`
### SW5-1 The desktop waitress server spools every request body up to 1 GiB to a temporary file before the wiki can refuse it (full re-audit pass 13)
- **Severity:** Medium. **Attacker:** anonymous device on the teacher's LAN once "share on LAN" is on (no account, no CSRF token).
- **Repro:** the real `WikiServer(backend="waitress")` with a scratch data folder and `TMPDIR`; three anonymous `POST /login` with `Content-Length: 300 MiB` sent at full speed. Observed: no answer after the headers, 750 MiB of unlinked temporary files held after 250 MiB per connection, then `400 BAD REQUEST` (CSRF) only once every byte had arrived. Waitress reads a whole body (spooling past `inbuf_overflow`, 512 KiB) before the application runs, and the launcher kept its 1 GiB `max_request_body_size`, so one address with 16 connections could hold 16 GiB in the system temporary folder (tmpfs on many Linux systems). Expected: 413 right after the headers.
- **Fix:** `bananawiki/desktop/server.py` passes `max_request_body_size` = the wiki's largest upload (`MAX_CONTENT_LENGTH`, 500 MiB by default: a site import) + 1, so a larger body is refused with 413 from its headers alone. `bananawiki/desktop/waitress_server.py` adds a per-address budget of body bytes arriving or waiting to be served (`BODY_BUDGET_UPLOADS` = 2 x `MAX_CONTENT_LENGTH`): a `_Parser` charges Content-Length when the headers end (a chunked body as it arrives) and answers 413 past the budget; the charge is given back when the request is closed after serving or its connection closes. A request refused this way (or past `max_request_body_size`) has its `expect_continue` cleared: waitress 3.0.2 otherwise sends `100 Continue` to it once its headers end, which marks it incomplete again, so a client sending `Expect: 100-continue` was invited to send the refused body and waitress spooled it, uncharged, up to `max_request_body_size` before answering 413 (found in review: six 1 MiB requests from one address held 6 MiB against a 2 MiB budget; with the fix the refused ones get 413 at once and 2 MiB is held). One device now holds at most 1 GiB (2 x `MAX_CONTENT_LENGTH`) of bodies instead of 16 GiB, with or without `Expect`. docs/desktop.md describes the limit. Residual: several addresses each get their own budget, and waitress still spools to the system temporary folder (moving it would mean changing `tempfile.tempdir` process-wide).
- **Test:** `tests/security/test_s6.py::test_sw5_1_waitress_refuses_a_body_past_the_largest_upload_after_the_headers`, `tests/security/test_s6.py::test_sw5_1_one_address_holds_at_most_its_body_budget`, `tests/security/test_s6.py::test_sw5_1_a_refused_request_expecting_100_continue_is_not_invited_to_send_its_body`

### SW5-2 A tenant's deeply nested .banana-maintenance directory makes every backup and update fail and leaves the whole hosting portal in maintenance mode (full re-audit pass 13)
- **Severity:** Medium. **Attacker:** hosting tenant running plugin code (`HOSTING_ALLOW_TENANT_PLUGINS=1`), writing its own `/data`.
- **Repro:** the hosting fixture of tests/ops/test_ops_recovery.py with `data/instances/acme/.banana-maintenance/x/x/...` 1,200 levels deep. Observed: `backup()` raised "The previous state was put back, but it did not start: maximum recursion depth exceeded Maintenance mode remains active", `data/.banana-maintenance` (the portal's 503 gate) stayed, a second backup failed the same way, `start` raised RecursionError and `recover`/`update` reported success but left the marker. `_remove_entry_at` recursed once per level with no bound, and `tenant_maintenance` caught only OSError. Expected: a warning for that tenant, and the operation carries on.
- **Fix:** `bananawiki/ops/files.py`: `_remove_entry_at` is iterative (an explicit stack, one descriptor per level) and stops at `TENANT_DEPTH` (64, the snapshot walk's bound) with `_TooDeep`; `untrusted_marker` then renames the tree aside in the tenant's own directory (`.banana-maintenance.too-deep-<random>`, left to the tenant, logged) and writes the marker as before. The top directory's descriptor is on the stack before it is listed, so a listing error (an unreadable directory) closes it instead of leaking it. `bananawiki/ops/manager.py`: `tenant_maintenance` logs any per-tenant exception (escaped) instead of only OSError, so one tenant's entry can never block the platform.
- **Test:** `tests/security/test_s6.py::test_sw5_2_a_deep_tree_under_the_marker_name_is_set_aside`, `tests/security/test_s6.py::test_sw5_2_a_marker_directory_that_cannot_be_listed_leaks_no_descriptor`, `tests/security/test_s6.py::test_sw5_2_a_deep_tenant_marker_tree_does_not_keep_the_platform_in_maintenance`

### SW5-3 Any client of the GPU speech server makes it print a full traceback for each connection it resets, outside the tagged log format (full re-audit pass 13)
- **Severity:** Low. **Attacker:** anyone who can reach the speech server's port, no token needed (on a hosting platform, tenant plugin code on the shared network).
- **Repro:** the real server (`make_server` with the fake engine, default limits); 20 connections without a token, half with a partial request line, each closed with `SO_LINGER 0`. Observed: 21 "Exception occurred during processing of request" blocks with tracebacks (32 KB) on stderr ending in `ConnectionResetError` in `_RequestReader._fill`; `BaseHTTPRequestHandler.handle_one_request` catches only TimeoutError, so socketserver's `handle_error` printed each. Expected: a reset treated as a client gone, at most one tagged line.
- **Fix:** `contrib/tts-gpu-server/server.py`: `Handler.handle_one_request` catches `ConnectionError` (reset, broken pipe, aborted), sets `close_connection` and logs one debug line (address and tenant field, exception type only).
- **Test:** `tests/security/test_s6.py::test_sw5_3_speech_server_treats_a_reset_connection_as_a_client_gone`

### TW2-1 After a transfer, the admin-password reset left the previous owner's other administrator accounts working (full re-audit pass 14)
- **Severity:** Low. **Attacker:** the previous owner of a transferred wiki (accepted with `previous_owner=none`, or an administrator's Move now).
- **Repro:** the wiki had the named owner `wikiadmin` and an admin-role `backdoor` signed in elsewhere.
  `tenant_task.reset_admin` (what the portal's reset sends) reset only `wikiadmin`: `backdoor`'s session
  still reached `/admin/users`, and a fresh sign-in with its old password worked, after the step
  docs/hosting.md gives the new owner to lock the previous one out.
- **Fix:** `reset_admin` takes `every_admin` and then resets every admin and owner account with the named
  one (new password to change, sessions and API tokens ended, two-step off, platform links dropped); the
  portal's `instances.reset_admin_password` sets it once the wiki has changed owner (an accepted transfer,
  or an `instance.owner_changed` event), through `runtime.reset_admins(..., every_admin=)`. Wikis that never
  changed owner keep resetting only the named account. docs/hosting.md says so.
- **Test:** `tests/security/test_tw2.py::test_tw2_1_a_reset_after_a_change_of_owner_covers_every_administrator`,
  `::test_tw2_1_the_portal_resets_every_administrator_once_the_wiki_changed_owner`.

### UW1-1 The invite form's "code taken" answer reveals other accounts' custom codes (full re-audit pass 15)
- **Severity:** Medium. **Attacker:** Editor with the delegated `invite.generate` permission.
- **Repro:** The editor posted `/admin/codes/generate` with custom codes STAFF1990 and up, deactivating and purging each probe. On the 37th probe no row was created, which revealed the admin's STAFF2026 (assigned role admin). Signing up with it created an approved administrator. Expected: the check does not reveal codes the caller cannot list.
- **Fix:** Each custom code a non-administrator tries now spends a try from a budget of 10 per account and 30 per address an hour (`invites.spend_custom_code_guess`, `SqlLimiter` buckets `invite_custom` and `invite_custom_ip`). Once it is used up, the form answers 429 with `admin.codes.error.too_many_custom` (en, it, de). Random codes and administrators are not limited. Documented in docs/security.md under Rate limits. Compatibility: a delegated inviter can try only 10 custom codes an hour.
- **Test:** `tests/security/test_u1.py::test_uw1_1_custom_code_tries_of_a_delegated_inviter_are_limited`

### UW1-2 Deleting an assessment or resetting a member's attempts leaves no audit entry (full re-audit pass 15)
- **Severity:** Low. **Attacker:** Editor with `assessment.manage` and edit rights on the page, or an administrator.
- **Repro:** editor1 reset reader1's attempts and then deleted the quiz. Both answered 302, attempts went from 2 to 0, and no assessment entry was written to the audit log. Deleting the page was logged as `page.deleted`. Expected: the log names who erased whose results.
- **Fix:** `assessments.service.delete` and `reset_attempts` now record `assessment.deleted` (page, assessment title, attempts removed) and `assessment.attempts_reset` (page, member name and id, attempts removed). While impersonating, `as_user` is added. Labels added in en, it and de, and docs/security.md lists them under Audit log, Content.
- **Test:** `tests/security/test_u1.py::test_uw1_2_deleting_an_assessment_or_resetting_attempts_is_audited`

### UW2-1 An admin-role invite code of the previous owner survives the reset after a change of owner (full re-audit pass 15)
- **Severity:** Medium. **Attacker:** Previous owner of a transferred hosted wiki.
- **Repro:** backdoor (admin) made code KEEPMEIN42 with assigned role admin, unlimited uses and no expiry. `tenant_task.reset_admin(..., every_admin=True)` then reset both administrators. A fresh client signed up with KEEPMEIN42 and got an approved admin account, which could open `/admin/users`. Expected: the previous owner has no administrator access after the reset.
- **Fix:** With `every_admin`, `tenant_task.reset_admin` now also deactivates (`deleted = 1`) two kinds of live invite codes: those the reset accounts made, and any code that grants more than a plain member account (assigned role editor or admin, or a custom role). It records `invite.deactivated` (via hosting portal, target type `invite`, as the wiki's own deactivation does) for each in the wiki's audit log and returns the count as `invites_deactivated`. A reset without a change of owner is unchanged. Documented in docs/hosting.md.
- **Test:** `tests/security/test_u1.py::test_uw2_1_a_reset_after_a_change_of_owner_deactivates_the_previous_owners_invite_codes`

### UW2-2 A duplicate into another account keeps the source owner's admin accounts working after the new owner's reset (full re-audit pass 15)
- **Severity:** Low. **Attacker:** Owner of the source wiki, once a platform administrator duplicates it for another account.
- **Repro:** Portal fixtures: `instances.duplicate(wiki, ownery, 'ycopy', ...)`, then `reset_admin_password(copy)`. `_owner_changed(copy)` was False and the runtime got `reset_admins_named`. So the source owner's other admin accounts, with their copied password hashes, kept working on the copy. Expected: `reset_admins_every`, as for a moved wiki.
- **Fix:** `hosting.instances.duplicate` now records `instance.owner_changed` on the copy when its owner differs from the source's, or when the source itself had changed owner (`_owner_changed(source)`), so `_owner_changed` holds and the reset covers every admin and owner account, and with UW2-1 their invite codes too. Review follow-up: a wiki of W moved to X and then duplicated for X had given a copy whose reset was `reset_admins_named`, leaving W's admin accounts and codes working on it. A same-owner copy of a wiki that never changed owner is unchanged. Documented in docs/hosting.md.
- **Test:** `tests/security/test_u1.py::test_uw2_2_a_duplicate_for_another_account_counts_as_a_change_of_owner`, `tests/security/test_u1.py::test_uw2_2_a_same_owner_copy_of_a_wiki_that_changed_owner_inherits_the_change`

### VW1-1 Deleting a category moved its pages for an editor who may not move them (full re-audit pass 16)
- **Severity:** Low. **Attacker:** Editor with `category.delete` and write access to a category, whose role or per-user permissions leave out `page.edit_metadata` or `page.edit_all`.
- **Repro:** editor1 had the editor defaults minus `page.edit_metadata` and `page.edit_all`. Their `POST /page/board-minutes/move` answered 403. Then `POST /category/<id>/delete` with `page_action=uncategorize` answered 302, and the page's `category_id` became NULL. Expected: the deletion is refused, as moving the page on its own is.
- **Fix:** `categories.delete_with_pages` now refuses an `uncategorize` or `move` deletion with `PagesRefused("immovable", pages)` when any page fails `access.can_edit_metadata`. This covers the web form, `DELETE /api/v1/categories/<id>` (403 `category_pages_immovable`) and bulk management. The message is added in en, it and de, and docs/api.md is updated. Default editors hold both permissions, so they are unaffected.
- **Test:** `tests/security/test_vw.py::test_vw1_1_deleting_a_category_does_not_move_pages_the_editor_may_not_move`

### VW1-2 Re-saving an assessment erased every member's recorded answers with no audit entry (full re-audit pass 16)
- **Severity:** Low. **Attacker:** Editor with `assessment.manage` and edit rights on the page, or an administrator (accountability).
- **Repro:** editor1 created a quiz and two readers submitted it, giving 8 answer rows. editor1 then posted the same form, without question ids, to `/page/fruit-facts/assessment/manage`. The answer was 302 and the answers went from 8 to 0. The only new audit row was `user.login`. Expected: the log names who erased the answers.
- **Fix:** `assessments.service.save` records `assessment.saved` when it saves an existing assessment and that save does any of these:
  - removes questions, with their answers;
  - changes the type, options, answer key or points of a question;
  - changes the attempt rule or the banned users and roles.

  The entry gives `questions_removed`, `answers_removed`, `questions_regraded` and `rules_changed`. While impersonating, `as_user` is added (UW1-2's `_audit` helper). Labels are added in en, it and de, and docs/security.md is updated. Creating an assessment and edits that change only text are not logged, as before.
- **Test:** `tests/security/test_vw.py::test_vw1_2_resaving_an_assessment_that_erases_answers_is_audited`

### VW2-1 The reset after a change of owner left the previous owner's webhooks sending the new owner's wiki events (full re-audit pass 16)
- **Severity:** Low. **Attacker:** Previous owner of a transferred hosted wiki, or of one duplicated for another account.
- **Repro:** backdoor (admin) made a webhook to `https://previous-owner.example/hook` for `page.created`. Then `tenant_task.reset_admin(every_admin=True)` reset backdoor, and a page titled "New owner's secret plan" was created. A pending delivery with that title was queued to the old address, and the hook was still active. Expected: the reset stops the wiki's events going to an address the previous owner chose.
- **Fix:** With `every_admin`, `tenant_task.reset_admin` now switches off every active webhook (`active = 0`; `disabled_reason` stays NULL, as for an administrator's switch-off). Each one is recorded as `api_service.webhook_updated` (changed `active`, via hosting portal, host only), and the result reports `webhooks_disabled`. Deliveries need an active hook, so nothing more is queued or sent. The new owner can review the webhooks, rotate their secrets and switch them back on. A reset without a change of owner is unchanged. Documented in docs/hosting.md.
- **Test:** `tests/security/test_vw.py::test_vw2_1_a_reset_after_a_change_of_owner_switches_off_webhooks`

### VW2-2 Pruning the moderation log deleted the record that a wiki changed owner, so a later reset covered only the named admin (full re-audit pass 16)
- **Severity:** Low. **Attacker:** Previous owner of a wiki that an administrator moved (Move now, or the wikis of a deleted account moved on) or duplicated for another account, on a platform with a short `HOSTING_EVENT_RETENTION_DAYS`.
- **Repro:** Portal fixtures:
  1. `move_to_owner` from ownerold to ownernew, then `reset_admin_password`, which sent `reset_admins_every`.
  2. The events were aged to 40 days and `events.prune(30)` was run.
  3. The next `reset_admin_password` sent `reset_admins_named`.

  So the previous owner's other admin accounts and admin-role invite codes were no longer covered.
- **Fix:** `hosting.events.prune` now keeps the `instance.owner_changed` rows of wikis that are not terminated. Once they are past the retention period, it clears their `actor_id` and `reason` (who moved the wiki and for whom), so only the fact that the wiki changed owner outlives the retention period. `_owner_changed` keeps holding, and accepted transfers already had their own table, which is never pruned. Rows of terminated wikis are pruned as before.
- **Test:** `tests/security/test_vw2_hosting.py::test_vw2_2_pruning_the_moderation_log_keeps_the_fact_that_a_wiki_changed_owner`

### XW1-1 Invite codes could be guessed through /platform-oauth/signup at about 18 times the /signup limit (full re-audit pass 17)
- **Severity:** Low. **Attacker:** Anyone with a hosting-portal account, on a wiki with platform sign-in configured and sign-up by invite code.
- **Repro:** Fixtures: `OAUTH_ENV` and `FakePortal`, with `open_signup` off and an owner's code STAFF2026 that assigns the admin role. `/signup` refused the 11th wrong code with 429. From the same network, one portal callback was followed by `POST /platform-oauth/signup` with STAFF2000..STAFF2026. It answered 400 26 times, then 302, and mallory became an approved admin. Only `platform_oauth:ip` (30 per 10 minutes) was spent.
- **Fix:** `platform_oauth_signup` now spends one `signup:ip` hit (10 per hour per network, as on `/signup`) for every attempt that posts an invite code. Once the budget is spent, the connect page answers 429 with the sign-up "too many attempts" message. Sign-ups without a code are not affected.
- **Test:** `tests/security/test_xw.py::test_xw1_1_invite_codes_tried_through_platform_sign_up_share_the_signup_budget`

### XW2-1 The reset after a change of owner left pages shared with federation peers sending the new owner's text to the previous owner's wiki (full re-audit pass 17)
- **Severity:** Low. **Attacker:** Previous owner of a transferred hosted wiki with federation enabled.
- **Repro:** Steps:
  1. The wiki was paired with an "attacker" peer, and a page was shared with it as the named owner (`store.share`).
  2. `tenant_task.reset_admin(..., every_admin=True)` ran, then `force_password_change` was cleared on the named account, as its first sign-in does.
  3. The page was edited to "NEW OWNER SECRET".
  4. A signed snapshot `GET /federation` with the pairing key answered 200 with that text.

  Expected: nothing is sent to a peer the previous owner set up.
- **Fix:** With `every_admin`, `tenant_task.reset_admin` now deletes every `federation_shares` row and records `federation.page_unshared` for each (page, peer, host, via hosting portal). The result reports `federation_shares_withdrawn`. The pairings, and the pages received from them, are kept, so no content is lost; the new owner can share again, or disconnect. A reset without a change of owner is unchanged. Documented in docs/hosting.md.
- **Test:** `tests/security/test_xw.py::test_xw2_1_a_reset_after_a_change_of_owner_withdraws_federation_shares`

### XW2-2 When tenant plugins are allowed, external plugins the previous owner installed stayed enabled after a transfer and the every_admin reset (full re-audit pass 17)
- **Severity:** Low. **Attacker:** Previous owner of a transferred hosted wiki, on a platform that allows tenant plugins.
- **Repro:** Traced in the code. `reset_admin(every_admin=True)` changed users, sessions, tokens, two-step, platform links, invite codes and webhooks, but `plugins.enabled` stayed 1. Neither `move_to_owner` nor `reset_admin_password` ran the plugin quarantine. Plugin code the previous owner chose kept running with the wiki's database and files. It could recreate an admin, or read and send out the new owner's content.
- **Fix:** With `every_admin`, `tenant_task.reset_admin` now disables every external plugin. It uses the same rule as the quarantine task, which now shares `_disable_external_plugins`: rows found in the plugin folder, and every `builtin = 0` row. The result reports `plugins_disabled`. When any were disabled, `AgentRuntime.reset_admins` restarts a running wiki, so code already loaded stops. No quarantine marker is set, because lifting one needs a platform administrator. The new owner can review the plugins and switch them back on in the wiki. A reset without a change of owner is unchanged. Documented in docs/hosting.md.
- **Test:** `tests/hosting/test_runtime_agent.py::test_xw2_2_a_reset_after_a_change_of_owner_restarts_a_wiki_whose_plugins_it_disabled`

### YW1-1 The reset after a change of owner kept the reset accounts' saved original passwords, so "Restore original password" gave the previous owner's admin account back (full re-audit pass 18)
- **Severity:** Low. **Attacker:** Previous owner of a transferred hosted wiki, or of one duplicated for another account. The attack needs one click from the new owner.
- **Repro:**
  1. Before the transfer, wikiadmin gave backdoor a temporary password (`action=set_temp_password`). That copies the known hash into `original_password_backup`.
  2. After `tenant_task.reset_admin(every_admin=True)`, the old password answered 401, and the backup was still set.
  3. The new owner then used "Restore original password" (`action=revert_password`). After that the old password signed in (302), and `/admin/users` answered 200.
- **Fix:** `tenant_task.reset_admin` now clears `original_password_backup` with the new password, for every account it resets. This matches what `accounts.set_password` does whenever a new password is set. With no backup, the restore button no longer appears.
- **Test:** `tests/security/test_xw.py::test_yw1_1_the_reset_drops_saved_original_passwords`

### YW2-1 Portal password confirmations, and the second-factor check that turns two-step sign-in off, had no per-account guess budget (full re-audit pass 18)
- **Severity:** Low. **Attacker:** Someone holding a portal account's signed-in session but not its password: an unattended browser or a stolen cookie. In port mode every wiki receives the portal cookie.
- **Repro:**
  - Password: signed in as victim, 200 wrong `POST /account` (username change) from 10 addresses plus 50 wrong `POST /account/change-password` were all checked, with no 429. The right password then renamed the account. On `/login` the same pattern was refused after the per-account budget.
  - Second factor: with two-step on, 100 wrong codes with the right password from 10 addresses on `POST /account/mfa` were all checked. The right code then turned two-step off.
- **Fix:**
  - Password: every portal page that asks for the password again now goes through `hosting.limits.confirm_password`. That covers `/account` (identity), change-password, delete, contact email, flagged email, two-step settings, API tokens and the admin backup-key export. It reserves one hit of a per-account `confirm-password` bucket (20 failures per 15 minutes, all addresses together) before the hash is checked, releases it on success, and answers 429 once the bucket is spent.
  - Second factor: `/account/mfa` now reserves the `/login/mfa` second-step budgets (`reserve_code_attempt`, per browser and per account, per window and per day) before checking a code, and releases them when the code is right (`release_code_attempt`, now shared with `/login/mfa`).

  Normal use is unaffected. Documented in docs/hosting.md.
- **Test:** `tests/security/test_yw2_hosting.py::test_yw2_1_password_confirmations_have_a_per_account_guess_budget`, `tests/security/test_yw2_hosting.py::test_yw2_1_turning_two_step_off_spends_the_second_step_budget`

### ZW2-1 The reset after a change of owner ran inside the live container, so the previous owner's plugin could undo it before the restart (full re-audit pass 19)
- **Severity:** Low. **Attacker:** Previous owner of a transferred or duplicated hosted wiki, on a platform with `HOSTING_ALLOW_TENANT_PLUGINS=1`, who installed a plugin before the transfer.
- **Repro:** On a running wiki, `AgentRuntime.reset_admins(every_admin=True)` ran the reset as a `docker exec` into the container where the plugin was loaded, and stopped it only afterwards. A thread polling the database, standing in for that plugin, re-enabled its own row (with `builtin=1`) and put back backdoor's password hash within the window. The reset reported `plugins_disabled=1`, yet backdoor's old password still worked, and the restart loaded the plugin again.
- **Fix:** With `every_admin`, a running wiki is now stopped before the reset task. The task then runs in a one-shot task container, where `tenant_task` loads no plugin code. The wiki is started again afterwards, also when the task fails. This means one restart per reset after a change of owner, which XW2-2 already caused whenever a plugin was disabled. Resets without a change of owner still run in the live wiki without a restart. Documented in docs/hosting.md.
- **Test:** `tests/hosting/test_runtime_agent.py::test_xw2_2_a_reset_after_a_change_of_owner_runs_with_the_wiki_stopped` (asserts stop, task, start in that order)

### AW1-1 Administrators could rewrite or delete any member's Kanban comment, an owner's included, with no audit entry (full re-audit pass 20)
- **Severity:** Low. **Attacker:** Plain administrator: a matter of accountability, and it crossed the owner/superuser hierarchy.
- **Repro:** Owner olivia commented "Do NOT pay invoice 42." Admin ada sent `PUT /api/kanban/comments/<id>` with "Approved. -- olivia", which answered 200. The row kept olivia's `user_id` with the new text, only an "edited" mark showed, and no audit entry was written. Deleting a comment was the same. OW3-3 had audited this only while impersonating.
- **Fix:**
  - Editing a comment is now for its author only (`access.can_edit_comment`), because the text stays credited to them.
  - Deleting stays open to administrators, but only for authors whose account they may change (`admin_service.protection_error`), so owners' and superusers' comments are kept. Each such deletion is recorded as `kanban.comment_deleted` (ticket, board, author).
  - This applies to the web API (`/api/kanban/comments/<id>`) and REST (`/api/v1/kanban/comments/<id>`), and `can_edit`/`can_delete` in the payload follow the same rules.

  Labels are added in en, it and de, and docs/api.md is updated. **Compatibility:** administrators can no longer rewrite another member's comment; they delete it instead. This is the secure option, because a rewritten comment still carries the author's name.
- **Test:** `tests/security/test_aw.py::test_aw1_1_administrators_cannot_rewrite_others_kanban_comments_and_deletions_are_audited`

### AW2-1 The previous owner's custom domain stayed bound after a transfer, so platform sign-in codes of the new owner's users could be sent to a host the previous owner controls (full re-audit pass 20)
- **Severity:** Low. **Attacker:** Previous owner of a transferred hosted wiki (an accepted offer, or an administrator's Move now) who had verified a custom domain whose DNS they control, on a platform with platform sign-in.
- **Repro:** oldowner's wiki had the verified domain docs.oldowner.example. After newowner accepted the wiki, `domains.active_domain()` was still that domain, and the offer had not mentioned it. With consent remembered, a `GET /oauth/authorize` with `redirect_uri=https://docs.oldowner.example/auth/platform/callback` answered 302 with a code to that host. The code redeemed at `/oauth/token`, and `/oauth/userinfo` named newowner. The previous owner could also serve the wiki's visitors at that address from their own server.
- **Fix:** `instances.move_to_owner` now drops the wiki's custom domain and any claim of one, in the same transaction that changes the owner, when the owner actually changes. It records `domain.removed` ("owner changed"), returns the note `domain_removed`, and `sync_routes` then stops routing the domain. A wiki moving to the account that already owns it keeps its domain. **Compatibility:** after a change of owner the new owner claims and proves a domain again. That is the secure option, because the DNS stays with the previous owner. Documented in docs/hosting.md.
- **Test:** `tests/security/test_aw2_hosting.py::test_aw2_1_a_change_of_owner_drops_the_previous_owners_custom_domain`

### BW2-1 A wiki terminated when the moderation log was pruned lost its owner-change record, so after a restore the reset covered only the named admin (full re-audit pass 21, rest of VW2-2)
- **Severity:** Low. **Attacker:** Previous owner of a wiki an administrator moved, on a platform with a short `HOSTING_EVENT_RETENTION_DAYS`.
- **Repro:**
  1. `move_to_owner` from ownerold to ownernew, then `terminate`.
  2. The events were aged 40 days and `events.prune(30)` was run. VW2-2 kept `instance.owner_changed` rows only for wikis not terminated, so the record was deleted.
  3. An administrator restored the wiki under the same id within its data-retention window.
  4. `reset_admin_password` then sent `reset_admins_named`.
- **Fix:** `events.prune` now also keeps the anonymised `instance.owner_changed` rows of terminated wikis whose data is kept (`data_retained_until IS NOT NULL`), since those can still be restored. Rows of wikis whose data is gone are pruned as before.
- **Test:** `tests/security/test_vw2_hosting.py::test_bw2_1_a_restorable_terminated_wiki_keeps_the_fact_that_it_changed_owner`

### DW2-1 An account merge changed wiki owners without the change-of-owner protections (full re-audit pass 22)
- **Severity:** Low. **Attacker:** Source account (previous owner) of an approved or administrator-run merge.
- **Repro:** A wiki with a verified custom domain; `merges.admin_merge("oldowner", "newowner", admin)`. The owner changed, but the custom domain stayed bound (its DNS belongs to the source) and no `instance.owner_changed` event was recorded, so a later password reset covered only the named admin.
- **Fix:** `merges._merge` now records `instance.owner_changed` for every moved wiki and drops its custom domain (`domain.removed`), as `instances.move_to_owner` does. The source's wiki sign-in links still move to the target, as before (same person by design).
- **Test:** `tests/security/test_dw2_hosting.py::test_dw2_1_an_account_merge_is_a_change_of_owner`

## Static analysis

* **ruff** (0.17.1): clean.
* **bandit** (`bandit -c pyproject.toml -r bananawiki -ll`): 0 issues. Skipped with
  the reason in `pyproject.toml`: B608 (189 hits: every SQL string interpolates
  column or table names written in the code, `?` placeholder lists or fragments
  made of them; values always go through parameters) and B704 (37 `Markup` uses,
  each traced to constants, escaped values or sanitised Markdown). Intentional
  hits (binds inside containers, container `/tmp` mounts, the agent socket's
  mode, fixed HTTPS URLs, the highlighter's pickle between a process and its own
  helpers) carry an inline `# nosec <id> - <reason>`.
* **pip-audit**: no known vulnerabilities in `requirements.txt` or `requirements.lock`.
* **Headless browser XSS** (Playwright/Chromium against the running app, 46
  surfaces: pages, titles, categories, search, preview, history and diffs,
  contributions, profiles and custom fields, announcements, chat (DMs, groups),
  kanban tickets, comments and checklists, canvases, attachments and their
  downloads, audit log, admin user list): nothing executed, no dangerous markup
  survived, no CSP violation.
* **Role x endpoint matrix**: `tests/security/test_authz_matrix.py` probes all 661
  routes and every method as anonymous, pending, suspended, user, editor and
  admin; breaking the role check makes it report 256 administrator views.

## Summary

State of branch `security-audit` at the end of the audit (328 commits on top of `main`, nothing pushed).

**Found and fixed.** 220 findings: 4 High, 49 Medium, 167 Low (plus informational entries). All are fixed and verified (each with a
regression test that fails before its fix and passes after), except 8 recorded as accepted risk with their
reason (W1-5 second half, V1-1, Y1-2 to Y1-5, the OW2-2 migration carry-over, the PW4-4 transfer restart).
The serious classes were found and fixed in the early passes: authorization and protection bypasses,
other users' data through listings and exports, Markdown/upload/import XSS and zip handling, SSRF,
sign-in, session and suspension flows, and site-wide denial of service (slow readers and the Gunicorn 26
close bug, JW1-2 / IW2-4 / JW2-1).

**Passes.** A first audit, then repeated fresh full re-audits of both repos (by area, with every finding
reproduced independently and then attacked by a reviewer trying to refute it; every fix reviewed
independently). The later passes confirmed 18, 22, 13, 15, 17, 14, 21, then (lean passes 14 to 20, both repos) 2, 5, 6, 4, 3, 2 and 5 findings, almost all Low:
audit-log gaps, per-account volume bounds and costs of crafted content.

**Done criterion 3 is met.** After pass 21 (BW2-1), pass 22 found one Low item here (DW2-1, an account merge did
not count as a change of owner), which is fixed; passes 23 (both Wiki areas) and 24 (the code changed since: the
administration overview, the canvas double-click handler, the read-aloud player and the merge fix) then came back
with no new issues. The four Wiki areas of pass 22 and the two of pass 23 covered the whole product; pass 24 was
limited to the changed code. Further narrow follow-ups of recent fixes remain possible, as in every earlier round.

**Tests changed.** 10 low-value test files removed and 62 added: the role x endpoint matrix
(`tests/security/test_authz_matrix.py`), the end-to-end flows (`tests/security/test_flows.py`), rate limits,
and one regression test per finding (`tests/security/test_*.py`). Full suite: 5112 passed, 30 skipped
(node or root-only tests). ruff and bandit clean; pip-audit clean on `requirements.lock`,
`requirements-desktop.lock` and `requirements-build.lock`.

**Residual risks.**
* Volume attacks by many accounts or addresses at once can still fill shared places (download places,
  search, long requests); each is bounded per client, and the affected entries say how many clients it takes.
* Plugins are trusted code by design; a malicious plugin gets what plugins get.
* The fixes were written and reviewed by AI agents. Review the riskiest changes by hand before deploying:
  the schema migrations (16 to 18), the sign-in, session and platform-link changes, the request pipeline
  (`wiki/app.py`, `hosting/app.py`), and the Gunicorn configuration patch.
* Slow request bodies are not bounded by Gunicorn itself in the Wiki; the shipped Caddy configuration bounds
  header and body read time. The Chat app now also cuts stalled or crawling bodies itself (Chat entry LC1-1);
  the Wiki has no such guard, so keep it behind the proxy.

**Needs a human decision (deploy-time).**
* Keep-alive is off (`keepalive = 0`) and `gunicorn.util.close_graceful` is replaced (JW1-2): re-enabling
  keep-alive (`--keep-alive`, `GUNICORN_CMD_ARGS`) brings back the accept-loop stall. Re-check when Gunicorn
  is upgraded past 26.2.0.
* Default limits chosen in the audit: `BW_DOWNLOAD_THREADS` 8 (threads per worker 4 to 12, tenants 8),
  sign-ins 30 per account and 200 per network per 15 minutes, 10,000 categories and 32 levels, 20 Kanban
  assignees, API token creations per day; raise them where a site needs more.
* Compatibility changes listed in UPGRADING.md: schema 18 (1.7.3 cannot open it afterwards: back up first),
  API calls refused for scope count against the rate limit, admin downloads and exports are POST,
  maintenance mode now also closes public reading, refused accounts see public views as anonymous.
* Behaviour changes from the late passes: administrators can delete but no longer rewrite other members' Kanban
  comments (AW1-1); a wiki that changes owner loses its custom domain, which the new owner proves again (AW2-1);
  the reset after a change of owner restarts a running wiki and disables its external plugins (XW2-2, ZW2-1).
* Interface changes made after the audit, on request (not security fixes): the administration side menu is
  replaced by an overview page at `/admin/home` (the account menu, `/admin` and the administrator sign-in now
  lead there instead of to the dashboard); a double click or double tap on a canvas element edits it; the
  read-aloud player has its own controls. They were re-audited in pass 24.
* The desktop launcher's LAN sharing is bounded per device, but share it only on trusted networks.
