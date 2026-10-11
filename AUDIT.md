# Audit

Source of truth for the production-readiness pass. Findings are ranked
security > data loss > broken features > UX > code quality.

## Baseline (2026-10-08)

| Check | Command | Result |
|---|---|---|
| Lint | `ruff check .` | pass |
| Tests | `python -m pytest tests -q -n 4 -p no:cacheprovider -o addopts=""` | 2657 passed, 28 skipped. Plain `pytest` fails at collection (`No module named 'tests'`); CI uses `python -m pytest`, which the README should say |
| Dependency audit | `pip-audit` | no vulnerabilities in runtime dependencies |
| Markers | `git grep -E "TODO\|FIXME\|XXX\|HACK"` outside tests and docs | none |
| Secrets | pattern scan for private keys and common token formats | none; `tests/fixtures/v1_instance/secret_key` is a fixture |

## Findings

| # | Severity | Area | Finding | Status |
|---|---|---|---|---|
| 1 | low | docs | No example environment file although about 160 variables are documented in `docs/configuration.md` | fixed, `.env.example` generated from the reference, `tests/test_env_example.py` keeps the two in step |
| 2 | low | docs | `HOSTING_EMAIL_SMTP_PORT`, `_USERNAME`, `_PASSWORD` were only documented as `_PORT`, `_USERNAME`, `_PASSWORD` shorthand | fixed, full names in `docs/configuration.md` |
| 3 | medium | broken page | `isdigit()` followed by `int()` accepts characters such as `²` that `int()` rejects. A crafted video shortcode margin made every view, excerpt, export and kanban comment list of that text return 500; the same pattern broke `/api/page/<slug>/sync?since=`, PDF export of `<ol start>`, canvas save and several admin pages | fixed, `core.numbers.ascii_int` at every site, `tests/test_core_numbers.py` |
| 4 | medium | data loss | Deleting an account also deleted every board and canvas it created, and every ticket it added to other people's boards (`NOT NULL ... ON DELETE CASCADE`); the dialog only mentioned private data | fixed, shared and public boards and canvases, and tickets on boards that stay, pass to the deleting administrator (or the longest-standing active one); private ones are deleted as before; dialog text updated in English and Italian; `tests/features/test_account_deletion_handover.py` |
| 5 | low | permissions | A moderator could lift their own group timeout, or another moderator's | fixed, the same self and rank checks as setting one, `test_a_timed_out_moderator_cannot_lift_a_timeout` |
| 6 | low | data integrity | A chunked request body over 16 MiB through the hosting proxy was cut to 16 MiB and forwarded as if complete | fixed, answers 413, `tests/hosting/test_wikihosts_body.py` |
| 7 | low | security | NAT64 addresses (`64:ff9b::/96`) count as global, so `64:ff9b::a9fe:a9fe` passed the outbound address filter | fixed, the embedded IPv4 address is checked instead, cases added to `test_address_policy` |
| 8 | low | data integrity | Passwords set through the API were stripped, so the stored value differed from the one sent and typed at sign-in | fixed, stored as sent, blank still refused, `test_api_keeps_password_whitespace_so_the_sign_in_form_accepts_it` |
| 9 | low | privacy | Failed sign-ins logged the typed username, which is sometimes a password typed into the wrong field | fixed, only existing accounts are named, `test_failed_sign_in_logs_known_accounts_but_not_unknown_names` |
| 10 | low | code quality | `canvas.service.referenced_upload_names` had no caller outside a test; the upload cleanup scans every text column | fixed, removed; the test now checks `uploads.referenced_names` |
| 11 | low | permissions | After a page rename, links in other pages are rewritten without the renamer's edit rights or a revision check | accepted: rewriting links everywhere is the documented behavior, and skipping pages the renamer cannot edit would leave broken links; the window for overwriting a concurrent edit is milliseconds inside one request |
| 12 | low | data integrity | A page edit POST without a `revision` field skips the conflict check | accepted: browsers always send it, and requiring it would change the form contract for scripts |
| 13 | low | concurrency | A scheduled job that runs longer than its lease (5 minutes or its interval) can be started again by another worker | accepted: no job is known to run that long; a renewal thread would add a second database connection for a hypothetical case |
| 14 | low | security | The hosting sign-in form token is signed but can be replayed for up to four hours | accepted: per-address and per-account limits still apply |
| 15 | low | accessibility | axe reported WCAG AA failures: links inside text distinguished by color only, `<mark>` contrast, scrolling code blocks and tables not keyboard focusable, controls nested in a `<summary>`, an unnamed ARIA label on the traffic chart bars and the editor preview | fixed in the wiki and the hosting portal, `tests/features/test_accessibility_markup.py`; the CSS and script changes are verified by the browser pass in READINESS.md |
| 16 | low | concurrency | Two simultaneous requests for one chunked upload id in the hosting admin can both append to the partial file | accepted: same account only, the final size check fails the upload safely |
| 17 | low | hygiene | OAuth credential rotation leaves the old client's authorization codes in the table | not a defect: a rotation issues a new client id and codes are bound to the old one, which no longer resolves; they expire in 5 minutes |

Second audit round (ops, tenant filesystem, archives, runtime agent, federation, plugin manager, desktop, hosting routes): no finding above low. The tenant filesystem, archive import and export, backup and restore, Caddy gating and federation code were read adversarially and held up.


## Third audit round (2026-10-08)

Four reviewers read the wiki, the hosting service and operations code, and the
code hygiene. Each finding was reproduced, and each regression test was run
against the code before its fix and failed there. mypy (not configured in the
project) was run once as a bug finder: of 274 reports one was a real defect
(#24); see the note at the end.

Upstream `main` gained 16 commits during this round (pull request #20). They
were merged, not rebased. Where both sides fixed the same problem, upstream's
version was kept: kanban imports stream attachments from the archive
(upstream R-20, covers #21), platform backups skip a failing wiki and record
it in a manifest (upstream R-23, part of #18), and kanban hands every board of
a deleted account, private ones included, to an administrator (upstream R-15,
replaces the kanban half of #4; the dialog text now says so). The canvas half
of #4 and the depth bound of #18 stay, since upstream does not cover them.

| # | Severity | Area | Finding | Status |
|---|---|---|---|---|
| 18 | medium | availability | One tenant could stop backups, updates and restores for the whole platform with a deep folder tree under its data: a staging path over PATH_MAX or ~1,100 nested folders (one open file per level) aborted the snapshot | fixed, a tenant entry that cannot be captured is skipped with a warning (a full or failing staging disk still fails), tenant walks stop 64 levels down, `tenantfs.usage` tolerates errors; `tests/ops/test_ops_components.py`, `tests/hosting/test_runtime_archives.py`. Accepted consequence: files a tenant nests past 64 levels or PATH_MAX are not in snapshots, so a rollback would not bring them back |
| 19 | low | permissions | History delete, clear, transfer and de-attribution, and page reordering, ignored page protection, reservations and the deletion freeze | fixed, `test_history_changes_are_refused_while_the_page_cannot_be_edited`, `test_reorder_pages_refuses_a_page_that_cannot_be_edited_right_now` |
| 20 | low | permissions | A scheduled temporary page deletion skipped page protection and Deletion Slowdown | fixed, refused when scheduled on a blocked page; at expiry the `page.delete` interceptors run first, `test_scheduled_deletion_respects_page_protection`, `test_scheduled_deletion_goes_through_deletion_slowdown` |
| 21 | low | availability | A 1 MB kanban import zip could make a worker hold about 1 GiB: every attachment member was read into memory | fixed (upstream's streaming import kept in the merge), `test_import_keeps_attachments_that_together_exceed_one_attachments_limit` |
| 22 | low | limits | Tokens named `userbot` were not counted against the per-user token limit and were hidden from the token page | fixed, the name is reserved for the automation key, `test_the_userbot_token_name_is_reserved_for_the_automation_key` |
| 23 | low | data integrity | A partial `POST /userbot/profile` and the SDK's `update_profile` erased the name and bio they left out | fixed in the API and the SDK, `test_userbot_profile_update_keeps_the_fields_it_leaves_out`, SDK case in `test_api_service.py` |
| 24 | low | broken page | A language file with a non-string or too long value answered 500 (`LanguageError` got `key` twice) | fixed, `test_bad_values_are_refused_with_a_message_naming_the_key` |
| 25 | low | security | With "administrators must use two-step verification" on, an administrator without it could still use `admin:read` tokens and was reported `is_admin` to wikis through OAuth | fixed, `tests/hosting/test_hosting_api.py`, `test_hosting_oauth.py` |
| 26 | low | privacy | The hosting suspension page (reason, end date) was shown after the password and before the second factor | fixed, `test_suspension_details_are_shown_only_after_the_second_factor` |
| 27 | low | concurrency | A wiki suspended while its owner was starting it could keep running | fixed, the start rechecks the status and stops the container, `tests/hosting/test_hosting_instances.py` |
| 28 | low | availability | One slow wiki could hold every proxy slot of the portal fallback | fixed, one slot always stays for other wikis and a response is cut after 300 seconds, `tests/hosting/test_hosting_hardening.py` |
| 29 | low | auth | About 13 addresses could lock a known username out of hosting sign-in for 15 minutes | fixed, a browser that signed in before has a signed device cookie and its own failure count, `tests/hosting/test_hosting_security.py` |
| 30 | low | auth | Someone holding a hosting password could guess the second factor with about a 0.9% chance a day | fixed, 50 attempts a day per account, apart for a known browser, `test_second_factor_attempts_are_capped_per_day_except_for_a_known_browser` |
| 31 | low | observability | Logs were text lines only | fixed, `BW_LOG_FORMAT=json` and `HOSTING_LOG_FORMAT=json`, `tests/test_core_logformat.py` |
| 32 | low | code quality | 16 unused functions and 6 unused CSS rules; emoji used as icons on kanban cards; the plugin example printed instead of logging | fixed |
| 33 | low | permissions | A token with only the `categories` scope sees the titles of the pages in a category it can read; the page builder's link check accepts `/\host` | accepted: both show only what the token's owner can already read or link to |
| 34 | low | docs | Upstream documented `BW_TTS_MAX_JOB_SECONDS` and `BW_TTS_PIPER_MEMORY_MB` without adding them to `.env.example` | fixed in the merge |
| 35 | low | accessibility | In the wiki's user list and the portal's administration dashboard, a user link next to a badge was told apart from the text by color alone (1.73:1, axe `link-in-text-block`) | fixed, underlined (`text-link`), `test_user_links_beside_badges_are_underlined`, `test_account_links_beside_the_admin_badge_are_underlined` |

mypy: besides #24, the 273 reports are false positives (Optional values
already checked, rows typed as dicts, stub quirks). Adopting a type checker is
a team decision recorded in BLOCKERS.md.


## Fourth round (independent audit, October 2026)

An independent audit read the wiki (administration, REST API, sign-in,
content), the hosting service, the operations code and the release tooling of
both projects, and reported 52 findings: 2 high, 18 medium, 32 low. Each fix
started with a regression test that failed on the code before it; the tests
named below pass on the 1.7.0 tree (checks at the end of this section).
Result: 46 fixed, 3 partly fixed, 3 open for the owner (#77, #81, #86).

| # | Severity | Area | Finding | Status |
|---|---|---|---|---|
| 36 | medium | permissions | A plain administrator could make themselves owner and superuser, demote the owner and read every private message and password hash through whole-site export and import | fixed, export and import are owner/superuser-only (refusals audited); imports drop triggers and views this release does not create and keep the live audit log; `test_plain_administrators_cannot_export_or_import`, `test_import_drops_triggers_and_views_this_release_does_not_create`, `test_import_keeps_the_live_audit_log` |
| 37 | medium | audit | The audit log missed most privilege changes, and any administrator could switch it off or prune it without a trace | fixed, grants, overrides, roles, approvals, impersonation, session revocations, invites, bulk deletions and feature and plugin switches are recorded; only owners and superusers switch the log off or change its retention, and both leave an entry; `test_account_administration_is_recorded`, `test_only_owners_and_superusers_switch_the_audit_log_off`, `test_plugin_switches_are_recorded`, `test_only_owners_and_superusers_shorten_the_retention` |
| 38 | medium | upgrade | The 1.4 takeover repair deleted orphaned rows without cascading, so some 1.4 databases could never be upgraded | fixed, the repair repeats until `foreign_key_check` is clean (the hosting v3 repair too). The migration was edited in place: it only runs on databases it never finished upgrading, and the resulting schema is unchanged; `test_orphan_chains_are_removed_and_the_upgrade_completes`, `tests/hosting/test_hosting_migration_repair.py` |
| 39 | low | data loss | The sign-up clean-up job could delete an account an administrator had just approved | fixed, each candidate is re-read in the deleting transaction; `test_expired_signup_cleanup_spares_an_account_approved_meanwhile` |
| 40 | low | permissions | Page protection was bypassed by bulk Markdown import and by demoting the controller | fixed, the import skips pages someone else protected or reserved; a demotion still ends a protection, by design, and is now recorded; `test_bulk_import_update_respects_page_protection`, `test_demoting_a_controller_records_the_protections_that_lapse` |
| 41 | low | secrets | The GPU speech token from the environment could be sent to a URL an administrator saved | fixed, an environment token is used only with an environment URL; `test_gpu_settings_never_send_the_environment_token_to_a_saved_url` |
| 42 | low | availability | The contrib GPU speech server had no socket timeouts and one thread per connection | fixed, `TTS_SOCKET_TIMEOUT` (30 s) and `TTS_MAX_CONNECTIONS` (64); `test_gpu_server_drops_connections_that_send_too_slowly`, `test_gpu_server_caps_open_connections` |
| 43 | low | data loss | Bulk deletes of pages, canvases and boards were immediate, permanent and partly unaudited | fixed, a typed confirmation word checked on the server and every bulk deletion audited (pages still go through deletion slowdown); `test_bulk_delete_needs_the_typed_confirmation`, `test_italian_confirmation_word_is_accepted`, `test_bulk_deletions_are_audited` |
| 44 | medium | code execution | Any wiki administrator could run code on the server by uploading a plugin, allowed by default | fixed (owner decision), third-party plugins are off on new installations, kept with a warning at every start on upgraded installations that already hold plugins, and only owners and superusers install, update or enable one; `tests/features/test_plugins_trust.py` |
| 45 | low | auth | A token with the `users` scope could change its owner's own password without the current one | fixed, `current_password` is required for your own account; `test_a_token_cannot_set_its_own_accounts_password_without_the_current_one`, `test_administrators_still_reset_other_passwords` |
| 46 | low | auth | Tokens issued with the `tokens` scope survived the revocation of the token that issued them | fixed, schema 8 records the issuing token, revocation cascades and issued tokens cannot issue tokens; the token page shows who issued a token and how many it issued; `test_revoking_a_token_revokes_the_tokens_it_minted`, `test_minted_tokens_cannot_mint_tokens`, `test_the_token_page_shows_which_tokens_a_token_issued` |
| 47 | low | permissions | An ordinary token with the `userbot` scope bypassed the administrator's userbot lock | fixed, only the automation key carries the scope, and `/userbot/*` answers only while automation mode is on; `test_an_ordinary_token_cannot_bypass_the_userbot_lock`, `test_the_userbot_scope_is_not_offered_on_ordinary_tokens` |
| 48 | low | security | With private networks allowed, the outbound filter let through IPv6, 6to4 and shared-space cloud metadata addresses | fixed, metadata networks and Teredo are always refused and 6to4 is judged by the IPv4 address inside; `test_metadata_and_tunnelled_addresses_are_refused_even_on_private_networks`, `test_tunnelled_addresses_are_judged_by_the_address_inside` |
| 49 | low | chat | Chat had no block or report, either participant could erase the shared history, and administrators reading private chats were weakly audited | fixed, per-participant clearing (schema 8), message reports (**Admin → Chats → Reports**), administrator reads in the audit log, and blocking (schema 10); `tests/features/test_chat_safety.py`, `tests/features/test_chat_blocks.py` |
| 50 | medium | auth | Anyone could lock any account, the owner's included, out of password sign-in, and per-address limits were bypassed with IPv6 | fixed, a browser that signed in before has its own budget through a signed device cookie, and every per-address limit of the wiki and the portal counts an IPv6 client as its /64. The suggested progressive delay was not added: it would hold a worker during each delayed attempt; `test_strangers_cannot_lock_an_account_out_of_a_browser_that_signed_in_before`, `test_one_ipv6_64_shares_the_per_source_sign_in_budget`, `tests/features/test_rate_limit_networks.py`, `tests/hosting/test_hosting_ipv6_limits.py` |
| 51 | medium | permissions | An editor allowed to generate invites could create accounts with more rights than their own | fixed, invites stay within the inviter's rights and are checked again at sign-up; `test_delegated_invites_cannot_grant_more_than_the_inviter_holds`, `test_invites_follow_their_creator_rights_at_sign_up` |
| 52 | low | sessions | "Remember me" ended after 7 idle days because the cookie signature lifetime was the ordinary session length | fixed; `test_remember_me_session_outlives_the_ordinary_session_length` |
| 53 | low | permissions | A temporary-role revert could demote an administrator on behalf of a plain administrator who could never do that | fixed, the revert is dropped when its scheduler no longer outranks the account or the role was changed by hand; `test_revert_scheduled_by_a_plain_admin_skips_a_since_promoted_admin`, `test_manual_role_change_while_paused_drops_the_revert`, `test_revert_drops_a_custom_role_of_the_higher_base_role` |
| 54 | low | privacy | The sign-in timing equaliser ignored `BW_PASSWORD_HASH_METHOD`, so user names could be enumerated | fixed, and hashes of an older method or cost are upgraded at sign-in; `test_unknown_accounts_burn_the_configured_hash_method`, `test_legacy_hash_is_upgraded_to_the_configured_method_on_sign_in` |
| 55 | low | permissions | Plain administrators could edit other administrators' profiles and sign out owners and superusers | fixed, together with hidden sign-in addresses and a session lookup by id; `test_admin_profiles_are_moderated_only_by_owners_and_superusers`, `test_mass_logout_by_a_plain_admin_spares_accounts_it_may_not_manage`, `test_sessions_page_hides_addresses_and_revocation_of_protected_accounts`, `test_revoking_one_session_finds_it_beyond_the_listed_ones` |
| 56 | low | auth | "Restore original password" could restore a password the user had since replaced | fixed, any later password change drops the saved original; `test_a_password_the_user_chose_later_is_never_rolled_back`, `test_a_later_permanent_reset_drops_the_saved_original` |
| 57 | low | auth | No second factor for wiki accounts, and a length-only password policy | fixed, optional TOTP two-step verification with recovery codes, an administrator requirement, resets by owners, superusers, the CLI and the hosting portal, and a notice after 5 wrong codes a day (schema 9); new passwords refuse the user name, the email's local part and the 10,000 most common passwords; `tests/features/test_auth_two_step.py`, `tests/features/test_auth_two_step_alerts.py`, `tests/test_core_totp.py`, `tests/test_core_password_policy.py`, `tests/features/test_password_policy.py`. Not in scope: passkeys; the hosting portal's own password rule is unchanged |
| 58 | medium | availability | Uncached rendering with no total highlighting budget cost 6-10 s of CPU per request, reachable by any signed-in user through `/api/preview` | fixed, a 64 KB per-document highlighting allowance shared by builder blocks, content-hash caches for pages, snippets and PDFs, and editor-only preview and highlighting at 60 a minute; `tests/test_core_markdown_budget.py`, `test_a_builder_page_highlights_within_one_budget`, `test_preview_is_for_accounts_that_can_edit`. The first view of a page with more than 64 KB of code still costs about 1 s |
| 59 | medium | broken feature | Uploads with non-Latin file names were refused, and other names were mangled | fixed, file names keep every script; `test_clean_filename_keeps_every_script`, `test_attachments_with_non_latin_names_are_stored_and_served`, `test_editor_images_with_non_latin_names_are_accepted` |
| 60 | low | broken feature | Canvas outline and kanban JSON downloads failed for titles outside Latin-1 | fixed, RFC 5987 `filename*`; `test_canvas_outline_download_of_a_non_latin_title`, `test_kanban_json_export_of_a_non_latin_title` |
| 61 | low | security | The declared nh3 range allowed versions that break every render (0.2) or let mention and shortcode rewriting break out of attribute values (0.3.0-0.3.6) | fixed, nh3 0.3.7 or later, and brackets inside attribute values are escaped before the rewrites; `test_mentions_embeds_and_task_lists_never_rewrite_attribute_values`, `test_rewrites_still_apply_to_text` |
| 62 | low | data loss | A title-only change could revert content saved concurrently | fixed, unchanged fields are read in the write transaction; `test_title_change_keeps_content_saved_after_the_callers_snapshot`, `test_builder_change_keeps_title_saved_after_the_callers_snapshot` |
| 63 | low | security | Style values were not validated, so editors could overlay the page chrome | fixed, only bounded non-negative lengths; `test_style_values_that_could_overlay_page_chrome_are_dropped`, `test_ordinary_layout_styles_are_kept` |
| 64 | medium | abuse | About 10 throwaway accounts could fill the platform's wiki capacity for 14 days | fixed, new self-service accounts hold one wiki for 7 days and together at most half the limit, and one address creates at most 10 accounts a day; `tests/hosting/test_hosting_signup_capacity.py` |
| 65 | medium | abuse | Any signed-in user could spend the daily email budget, which switched email verification off and blocked password-reset email | fixed, budgets in the database shared by every worker, a reserve for account recovery, a per-account budget, and verification fails closed; `test_verification_stays_required_when_the_daily_budget_is_used_up`, `test_one_account_cannot_spend_the_whole_platform_budget`, `test_password_resets_use_the_reserve_and_the_budget_is_shared_by_every_worker` |
| 66 | medium | permissions | An ownership transfer silently left the previous owner as a full-access collaborator | fixed, no access by default, or view only; `test_accepting_a_transfer_removes_the_previous_owner_by_default`, `test_full_access_cannot_be_chosen_for_the_previous_owner` |
| 67 | medium | data loss | Every customer wiki expired after 14 days with no warning and no extension, and expiry could not be switched off | fixed, `INSTANCE_DURATION_DAYS=0`, warning emails and an owner-renewal setting (off by default); `tests/hosting/test_hosting_expiry.py` |
| 68 | low | auth | "Administrators must use two-step verification" gated only `/admin`, so an administrator without it kept full power over every wiki | fixed; `test_an_admin_without_required_mfa_has_no_power_over_tenant_wikis` |
| 69 | low | availability | An unverified email address stayed reserved forever, locking its real owner out of sign-up | fixed, released after the verification link's lifetime while verification is required; `test_an_address_left_unverified_no_longer_blocks_its_owner`, `test_a_verified_address_is_never_released` |
| 70 | low | availability | A custom domain whose proof lapsed stayed bound to the old wiki forever | fixed, claimable by another wiki 7 days after the lapse; `test_a_domain_whose_proof_lapsed_long_ago_can_be_claimed_again` |
| 71 | low | limits | Transfers and suspended wikis bypassed the per-account wiki limit | fixed; `test_accepting_a_transfer_respects_the_recipients_wiki_limit`, `test_suspended_wikis_count_toward_the_account_limit` |
| 72 | low | auth | Someone holding a portal password could keep the owner from completing the second factor | fixed, the 5-minute bucket is per device too (the wiki's own two-step verification was built that way and also notifies the owner); `test_someone_holding_the_password_cannot_lock_the_owner_out_of_the_second_step` |
| 73 | high | availability | One upload named `*.db`, `*.sqlite` or `*.sqlite3` made every managed backup, rollback package and remote backup fail | fixed, only the applications' own databases are treated as SQLite; `test_uploads_named_like_databases_do_not_break_backups`, `test_the_wiki_database_itself_is_still_checked` |
| 74 | high | availability | Global free-space admission was re-checked on every quota verification, so a snapshot staged on the shared XFS volume stopped every tenant start and rolled updates back | fixed, capacity is checked only when a wiki is admitted or its limit raised, and snapshots must leave the reservations free; `test_admitted_tenant_keeps_starting_while_other_data_uses_the_volume`, `test_a_snapshot_on_the_wikis_volume_leaves_their_storage_reservations_free` |
| 75 | medium | availability | One wiki that could not restart under a new release rolled back the whole platform update, security updates included | fixed, at most one in ten such wikis is stopped and reported (`isolated_tenants`); `BANANA_UPDATE_UNREADY_WIKIS=rollback` keeps the old rule; `test_an_update_that_breaks_one_wiki_isolates_it_instead_of_rolling_back_the_platform`, `test_only_a_few_wikis_are_ever_isolated` |
| 76 | medium | availability | Every backup or update stops all wikis, and restarts are serialized under one agent lock with quota work that grows with every allocation | partly fixed, the per-verification allocation sweep is gone, publishing routes no longer re-verifies every wiki (`test_publishing_routes_does_not_recheck_every_running_wikis_storage`), and the portal's restart parallelism follows `BW_HOSTING_RECOVERY_MAX_WORKERS` (`tests/hosting/test_hosting_recovery_workers.py`). The platform-wide quiesce, the global start lock and the full-tree repair on a stopped start stay, for safety; docs/hosting.md ("Restarts and updates at scale") sizes the readiness timeout |
| 77 | medium | isolation | All tenant containers run as the portal's host UID without user-namespace remapping, so one container escape exposes every tenant and the portal's secrets | open: per-tenant UIDs need the portal's tenant file operations moved into the runtime agent, a redesign. docs/hosting.md ("One UID for every wiki") gives the threat model and the mitigations (tenant plugins off, patched kernel, runc and Docker, secrets off the host, untrusted tenants on separate servers); BLOCKERS.md |
| 78 | low | isolation | The root controller opened tenant-controlled SQLite files and WAL sidecars read-write | fixed, a hosted wiki's database is copied as bytes; `test_the_controller_never_opens_a_tenant_database` |
| 79 | low | network | Tenant isolation from the host and the LAN depended on manual firewalling, with unrestricted egress | fixed as an opt-in: predictable `bwt…` bridge names and a shipped nftables ruleset (`deploy/nftables/bananawiki-tenants.nft`) that the operator loads; `test_tenant_bridges_carry_the_name_the_host_firewall_matches`, `tests/ops/test_ops_firewall.py` (its `nft --check` case needs root and runs in CI) |
| 80 | low | supply chain | Production builds resolved unpinned dependency ranges, and opt-in automatic updates deployed unsigned commits as root | fixed, hash-pinned locks installed wheels-only, and `updates enable` requires signatures unless `--allow-unsigned`; `test_a_release_with_locks_installs_only_pinned_hashed_wheels`, `tests/test_requirements_lock.py`, `tests/ops/test_ops_release_tracking.py` |
| 81 | medium | release | Every audit and security fix existed only in local, unpushed commits | open, owner action: review, sign and publish the commits (BLOCKERS.md) |
| 82 | medium | release | No tagged releases, and the updater followed a branch's HEAD with signature checks off by default | fixed in code: new installations deploy the highest signed `vX.Y.Z` tag; existing servers keep their branch until switched; `tests/ops/test_ops_release_tracking.py`. Creating and signing the `v1.7.0` tags is an owner action (BLOCKERS.md) |
| 83 | medium | legal | The hosted service's privacy policy and terms were not sufficient for an Italian operator and contradicted the code in places | partly fixed: BananaWiki-site has complete English and Italian terms and privacy pages checked against the code (`BananaWiki-site/tests/test_site.py`), and the code now keeps their promises: moderation-log retention (`tests/hosting/test_hosting_event_retention.py`), Italian legal links (`tests/hosting/test_hosting_legal.py`), acceptance of new terms (`tests/hosting/test_hosting_terms.py`). The operator's own details, `TERMS_VERSION`, the effective date and a lawyer's review remain (BLOCKERS.md) |
| 84 | low | supply chain | Runtime dependencies were unlocked ranges resolved from PyPI on each server | fixed with #80 in both projects; Docker base images are still pinned by tag, not by digest |
| 85 | low | tooling | BananaChat's lint configuration left out the security rule set | fixed in BananaChat: `ruff.toml` selects `S` with reviewed exceptions, and `ruff check .` passes |
| 86 | low | release | The readiness commits are authored by a tool identity and unsigned | open, owner action: re-author or sign them before publishing (BLOCKERS.md) |
| 87 | low | docs | Release records were stale (test counts, supported Python versions) | partly fixed: `requirements.txt` states Python 3.11-3.14 and CI tests 3.14. Test counts in BananaChat's `docs/production-readiness.md` and in READINESS.md are regenerated after the final re-audit |

### Follow-ups done in this round

Besides the findings, these items raised while fixing them were done, each
with tests:

* Chat blocking (schema 10, `chat__blocks`), private to the member who
  blocks, with the send-path check inside the send transaction;
  `tests/features/test_chat_blocks.py`.
* Password policy for new passwords (bundled list of the 10,000 most common
  passwords, from SecLists' NCSC list under MIT, attributed in NOTICE);
  `tests/test_core_password_policy.py`, `tests/features/test_password_policy.py`.
* A security notice to the owner after 5 wrong two-step codes in a day;
  `tests/features/test_auth_two_step_alerts.py`.
* Two-step verification and chat blocks in the personal data export, without
  secrets; `test_export_says_whether_two_step_verification_is_on_without_its_secrets`.
* The token page shows which token issued a token; `test_the_token_page_shows_which_tokens_a_token_issued`.
* The contrib GPU speech server versions with the release;
  `test_gpu_server_versions_with_the_release`.
* `bananawiki reset-password --generate` draws another random password when
  one happens to contain the account's name or email (about 1 in 2,300 for a
  3-letter name), instead of failing on the new password policy;
  `test_a_generated_password_never_trips_the_password_policy`.
* Hosting: per-address limits by IPv6 /64 (`tests/hosting/test_hosting_ipv6_limits.py`);
  "Reset admin password" also resets the wiki account's two-step verification
  (`tests/hosting/test_hosting_wiki_recovery.py`); `/platform-oauth/merge`
  asks for the two-step code (`tests/features/test_auth_oauth_two_step.py`);
  Italian legal links (`tests/hosting/test_hosting_legal.py`); moderation-log
  retention (`tests/hosting/test_hosting_event_retention.py`); automatic end
  of storage suspensions (`tests/hosting/test_hosting_storage_suspension.py`);
  `BW_HOSTING_RECOVERY_MAX_WORKERS` for the portal's restarts
  (`tests/hosting/test_hosting_recovery_workers.py`); the loop-until-clean
  hosting v3 repair (`tests/hosting/test_hosting_migration_repair.py`);
  acceptance of new terms (`tests/hosting/test_hosting_terms.py`).

### Checks at the end of the round

Run on 9 October 2026 against the working tree (Python 3.14, Linux x86_64,
`age` on the PATH, pytest's temporary directory in a private folder).

| Check | Command | Result |
|---|---|---|
| Tests | `python -m pytest tests -q -n 3 -p no:cacheprovider -o addopts=""` | 3346 passed, 24 skipped, 0 failed. Skipped: 7 file-ownership and 1 `nft --check` case that need root, 10 that need a Caddy binary, 4 that need the deployment FFmpeg binary, 2 Windows-only. The one warning is Pillow's decompression-bomb warning, raised on purpose by an image-limit test |
| Lint | `ruff check .` | all checks passed |
| Dependency locks | `python scripts/lock_requirements.py --check` and `--verify`; `tests/test_requirements_lock.py` | the locks are complete and satisfy `requirements.txt`; every pin has a hashed wheel on PyPI for CPython 3.11-3.14 on Linux x86_64 and aarch64 |
| Dependency audit | `pip-audit -r requirements.lock --require-hashes`, the same for `requirements-build.lock` | no known vulnerabilities |
| Package | `python -m build` on a clean copy of the tracked and new files; the wheel installed into an empty folder | wheel and sdist for 1.7.0, no warnings. The wheel holds the common-password list and its licence, wiki migrations 8-10, hosting migration 6 and every new template; installed, it loads the 10,000 passwords and reports wiki schema 10 and hosting schema 6 |
| Environment variables | `tests/test_env_example.py` | every `BW_` and `HOSTING_` variable in docs/configuration.md is in `.env.example` |

Not repeated in this round: the browser pass (pages at 375, 768 and 1440 px,
axe, keyboard walk). The pages added in this round (two-step settings and
sign-in, Settings → Blocked people, chat reports, the portal's terms page, the
token page) are covered by route tests but have not been crawled; READINESS.md is
redone after that pass and the re-audit.


## Fifth round (second independent audit and visual pass, October 2026)

A second independent audit read the fourth round's fixes and the code they
added: wiki sign-in and two-step verification, content and the REST API, the
hosting portal, the operations code of both projects, the upgrade path from
1.6 and the new interface. It reported 33 findings for BananaWiki (3 high, 10
medium, 20 low); the BananaChat findings, FNU-1 (BananaChat's link warning)
and OPS2-10 (BananaChat's compute pairing) are in BananaChat's AUDIT.md. Each
fix began with a regression test that failed on the code before it, except
the documentation-only UP-3. An independent verifier then re-ran each
finding's proof of concept and variants of it against the fixed tree; where it
found a gap, a further pass closed it with its own failing-first test, or the
row says why it stays. For the rows marked "PoC re-run", the verifier's proofs
of concept were run again on 10 October 2026 against the tree described in
READINESS.md.

| # | ID | Severity | Area | Finding | Status and regression tests | Final verification |
|---|---|---|---|---|---|---|
| 88 | WH-1 | high | hosting sign-in | The new administrator two-step and terms gates sent accounts in two blocking states at once (for example an unconfirmed address and new terms) back and forth between two pages ("too many redirects"), locking them out of the portal | fixed: only the first blocking state in a fixed order counts (scheduled deletion, flagged address, missing address, unconfirmed address, approval, administrator two-step, new terms), every gate page is exempt from its own gate, and signing out, deleting the account and revoking tokens work in every state; `tests/hosting/test_hosting_gates.py` (every combination of states against every portal page, and the looping combinations over HTTP) | closed; PoC re-run: every chain ends on one page |
| 89 | OPS2-01 | high | operations | On a server that requires signatures, `restore` and `rollback` could not deploy a release whose tag was signed (the documented release procedure), only one whose commit was | fixed: a revision verifies by its own SSH signature or by a signed release tag of the update source naming it; the refusal gives the three steps to restore an unsigned one; `test_ops_release_signatures.py::test_a_tag_signed_release_restores_once_its_release_directory_is_gone`, `::test_a_restore_of_an_unsigned_revision_says_how_to_proceed` | closed; restoring a tag-signed release needs the update source to be reachable (fails closed with that message) |
| 90 | UP-1 | high | upgrade | Hosted wikis that 1.6 left above their plan, the 10 GiB ceiling or 100,000 files could not start after the update (hard-quota adoption refused them) | fixed: adopted at what they hold plus 64 MiB and 1,024 files, the limit follows usage back down to the plan, `storage.over_plan` event and one email to the owner; `tests/ops/test_ops_quota_adoption.py`, `tests/hosting/test_runtime_over_plan.py` | the verifier found that adopting without capacity admission could leave reservations above free space, so every later snapshot was refused; closed in the final pass: such wikis count at their adoption headroom and the controller warns with the shortfall (`test_wikis_adopted_without_admission_do_not_block_every_later_snapshot`, `test_the_lifecycle_controller_warns_about_wikis_adopted_beyond_the_volume`). Accepted: a stopped wiki with more than twice `HOSTING_AGENT_MAX_INODES` files needs that limit raised before the update (UPGRADING.md) |
| 91 | wiki-2fa-device-budget-multiplication | medium | two-step verification | Device cookies could be collected without limit while two-step verification was off, each with its own code budget, so the 6-digit code could be brute-forced | fixed: device cookies are signed with a per-account device epoch that a new password and every two-step change retire; at most 100 wrong codes a day per account and a doubling wait from one browser; the portal's per-account ceiling (20 per 5 minutes, 150 a day) and `mfa_epoch`; `tests/features/test_auth_two_step_devices.py`, `tests/hosting/test_hosting_access.py::test_device_cookies_from_before_two_step_verification_give_no_extra_guesses`, `::test_every_browser_together_has_a_ceiling_on_codes` | closed; variant re-run (cookies collected while the owner turned it off and on): nothing extra |
| 92 | wiki-audit-guard-onboarding-bypass | medium | audit | A plain administrator could switch the audit log off, unrecorded, by replaying the onboarding wizard | fixed where features are switched: `registry.set_enabled` asks the new `feature.switch` interceptors and records through `feature.switched`; the wizard no longer offers the audit log to plain administrators; `tests/features/test_audit_feature_switches.py` | no gap reported by the verifier |
| 93 | wiki-rehash-revokes-api-tokens | medium | sign-in | Re-hashing a password at sign-in (another hash method or cost) revoked every API token of the account and made its browsers unknown | fixed: the re-hash carries the tokens' credential stamp and the device epoch over in its transaction; `tests/features/test_auth_rehash.py` | the verifier found a race (an API request reading the token before the re-hash and the account after it still revoked the token); closed in the final pass: the token and its owner are re-read in one transaction before revoking (`test_a_re_hash_between_the_token_and_owner_reads_keeps_the_token`); PoC re-run: the token stays active |
| 94 | WCA-1 | medium | availability | Listing a ticket's comments rendered an unbounded number of documents per request (about 30 s of CPU per GET, repeatable) | fixed: at most 300 comments per ticket, one shared highlighting allowance per listing, renderings kept per process, and at most about 4 MB of HTML per listing (older comments as plain text); `tests/features/test_kanban_comment_budget.py` | the verifier found that reference-link comments still expanded about a hundredfold (12.4 MB of HTML for 60 comments); closed in the final pass by the listing budget (`test_a_listing_renders_a_bounded_amount_of_html`); PoC re-run with 300 such comments: 5.6 MB in 0.48 s, 0.05 s when cached |
| 95 | WH-2 | medium | hosting accounts | The fix for squatting unverified addresses could be bypassed by changing the address away and back, refreshing the hold indefinitely | fixed: address changes count in a 30-day series per account; only the first three hold the new address from the moment they are typed; a later verification link holds it at most twice the link's lifetime; `tests/hosting/test_hosting_email_budget.py` (flip, typo, later-link and series tests) | the verifier bypassed it by confirming an address of the squatter's own in between; closed in the final pass (confirming no longer restarts the series; `test_confirming_an_address_of_ones_own_does_not_renew_the_hold_on_another`); PoC re-run: the real owner's sign-up succeeds. Accepted: an attacker who keeps creating new accounts can still hold an unconfirmed address a few days a month; the complete fix (uniqueness among verified addresses only) is a separate change (BLOCKERS.md) |
| 96 | WH-3 | medium | hosting email | Anonymous recovery requests could spend the whole email budget's recovery reserve and, with verification failing closed, block sign-ups and password resets | fixed: recovery uses only its fifth of the budget and never what verification needs, per address and per network caps; `test_one_network_cannot_ask_for_recovery_email_all_day`, `test_recovery_requests_cannot_spend_what_verification_needs` | the verifier still drained the recovery fifth from 6 IPv4 networks or one IPv6 /48 and blocked a victim's reset with 3 reminders; mitigated in the final pass: reminders use at most half the fifth, the per-address cap counts resets and reminders apart, an IPv6 /48 makes at most 30 requests a day, a spent fifth is said alike to everyone (429) and shown in `/admin/attention` (`test_user_name_reminders_cannot_block_ones_password_reset`, `test_user_name_reminders_cannot_use_up_what_password_resets_need`, `test_ipv6_recovery_requests_also_count_per_site`, `test_a_spent_recovery_share_is_said_and_shown_to_administrators`); PoC re-run at the default limit: the victim's own reset is delivered in all three scenarios. Accepted: someone who knows about ten user names with their verified addresses and has a few networks can still pause recovery for a day (BLOCKERS.md) |
| 97 | OPS2-02 | medium | backups | With append-only remote backups, branches named with future dates made `backups prune` delete every genuine snapshot, and one crafted ID broke every later upload | fixed: retention counts only snapshots the series' recovery key authenticated and never deletes below `--keep` of them; implausible IDs are neither counted nor deleted; `tests/ops/test_ops_backup_retention.py` | closed (also through Git's partial-fetch path); accepted: someone holding the recovery key (a full server compromise) can still push authenticated snapshots (BLOCKERS.md) |
| 98 | OPS2-03 | medium | release | With commit signing and tag tracking, the tag name was not authenticated, so anyone who could push tags could have a signed but unreleased commit deployed | fixed: a release must be an annotated tag object for exactly that name and commit, SSH-signed by an allowed signer; `test_a_signed_commit_under_a_tag_anyone_could_push_releases_nothing`, `test_ops_release_tracking.py::test_a_release_tag_signed_for_its_name_is_required_when_signatures_are` | closed (a wrong key, a fake signature block in the message, a force-moved name and a renamed tag object are refused) |
| 99 | UP-2 | medium | upgrade | A hosted wiki wrote its pre-upgrade database copy inside its quota'd directory, so a wiki with little room could not start under 1.7 | fixed: hosted wikis write no copy (the platform's update package covers them); a standalone wiki that cannot write its copy refuses to upgrade with the space it needs; `tests/test_takeover_copy_space.py` | closed; accepted: copies written by earlier 1.4 to 1.6 upgrades stay in hosted wikis and count against their storage |
| 100 | UP-3 | medium | docs | "Going back to 1.6" told checkout installs to restore the newest pre-upgrade copy, which is stale for installs that followed `main` | fixed: UPGRADING.md names the copy by the schema it was written from and says which releases can open it (documentation only, no test) | closed |
| 101 | wiki-api-current-password-oracle | low | REST API | `current_password` on `PUT /users/<own id>` had no attempt limit | fixed: it shares the account page's limit (10 in 15 minutes), then 429 `too_many_attempts`; `tests/features/test_api_current_password_limit.py` | no gap reported by the verifier |
| 102 | wiki-site-import-two-step-lockout | low | site migration | Importing a site from another wiki locked out every account with two-step verification, and re-imports duplicated the audit log | fixed: set-ups this wiki cannot read are turned off with a notice and a `site.import_two_step_off` entry; carried-over audit entries are not duplicated; `tests/features/test_site_migration_two_step.py` | no gap reported by the verifier |
| 103 | wiki-auto-logout-hierarchy | low | permissions | A plain administrator could sign out owners, superusers and other administrators through the daily automatic sign-out | fixed: it ends only the sessions of users and editors; `tests/features/test_admin_auto_logout_hierarchy.py` | no gap reported by the verifier |
| 104 | WCA-2 | low | audit | An administrator reading a private group through its JSON feed was not audited | fixed: `chat.group_read`, once per 10 minutes per group; `test_chat_privacy.py::test_reading_a_private_group_as_json_is_audited_once_per_window` | closed; PoC re-run: the read is in the audit log |
| 105 | WCA-3 | low | chat | A direct-message sender could still erase messages from the recipient's copy | partly fixed: deleting for both participants only within 15 minutes of sending; `test_chat_privacy.py::test_a_sender_takes_back_a_direct_message_only_shortly_after_sending_it` | accepted (owner decision, BLOCKERS.md): deleting the sender's account still removes the conversation for both, because the chat tables cascade; reported copies survive (`test_deleting_an_account_removes_its_conversations_but_not_reported_copies`); PoC re-run: an older message is refused with 403 |
| 106 | WCA-4 | low | privacy | Cached PDFs kept serving images that had since been deleted or replaced | fixed: the cache key covers every embedded upload; `tests/features/test_page_export_pdf_cache.py` | closed; PoC re-run, also with a replacement of the same size and time |
| 107 | WCA-5 | low | performance | Page-list excerpts of a builder page used up the page's own highlighting allowance | fixed: plain text never highlights code; `tests/test_core_markdown_plain_text.py` | no gap reported by the verifier |
| 108 | WH-4 | low | hosting | The terms gate did not cover API and wiki sign-in tokens, and a gated account could not revoke its tokens | fixed; `test_hosting_gates.py::test_api_tokens_stop_working_until_the_new_terms_are_accepted`, `::test_wiki_sign_in_tokens_stop_working_until_the_new_terms_are_accepted`, `::test_a_blocked_account_can_still_revoke_its_tokens` | closed; PoC re-run: the API answers 403 |
| 109 | WH-5 | low | hosting legal | The recorded terms version did not match the published terms, and a later required version was lowered silently | fixed: `HOSTING_TERMS_VERSION` names the published version; the portal refuses to start when the required version is later or either is not a real date; `tests/hosting/test_hosting_terms.py` | no gap reported by the verifier; setting the version is an owner action (BLOCKERS.md) |
| 110 | WH-6 | low | hosting | Owner renewal overrode an end date an administrator had brought forward, and any collaborator allowed to start and stop could renew | fixed; `test_hosting_expiry.py::test_only_the_owner_renews_not_a_collaborator_who_may_start_and_stop_it`, `::test_an_end_date_an_administrator_brought_forward_is_not_renewed_away` | no gap reported by the verifier |
| 111 | WH-7 | low | hosting | Any wiki with the custom-domain permission could release another wiki's lapsed domain binding without proving anything | fixed: the binding moves only once the claimant verifies its own TXT token; `test_hosting_access.py::test_a_recently_lapsed_proof_still_holds_the_domain`, `::test_a_long_lapsed_domain_changes_wiki_only_once_the_claimant_proves_it`, `::test_an_owner_whose_dns_came_back_keeps_the_domain` | no gap reported by the verifier |
| 112 | OPS2-04 | low | availability | One unverifiable highest tag stopped all updates | fixed: the highest of the 20 highest tags that verifies is deployed, skipped tags are reported; `test_an_unverifiable_higher_tag_does_not_hold_back_a_signed_release` | partly closed: 20 unverifiable tags still block updates, as deleting the release tags would; accepted, protect `v*` tags (BLOCKERS.md) |
| 113 | OPS2-05 | low | release | A restore replaced the server's update source, signers and credentials with the package's after the trust check | fixed: an installed server keeps its own (`source_warning`); `test_a_restore_keeps_the_servers_update_source_and_signature_trust` | closed, also when the restore fails and is rolled back |
| 114 | OPS2-06 | low | backups | `--allow-unauthenticated` accepted snapshots that could not predate authentication, and the refusal blamed old releases | fixed: future-dated and newer-than-authenticated snapshots are refused, with an honest message; `test_an_unauthenticated_snapshot_newer_than_an_authenticated_one_is_never_restored` | the verifier still restored a forgery dated like the first authenticated snapshot, or older than the oldest one retention kept; closed in the final pass: every authenticated index records when authentication began (`test_an_unauthenticated_snapshot_dated_like_the_first_authenticated_one_is_never_restored`, `test_retention_does_not_forget_when_authentication_began`) |
| 115 | OPS2-07 | low | network | The host firewall's DNS exception let wikis reach port 53 on every private address | fixed: DNS only to this host's resolvers, filled in by `bananawiki firewall`; `tests/ops/test_ops_firewall.py` | closed; the persistence instructions still pointed at the shipped file (empty resolver set) and were corrected in the final pass |
| 116 | OPS2-08 | low | availability | Isolating an unready wiki was best effort: it could be reported isolated while it kept starting | fixed: recorded for the portal, stopped through the runtime agent and checked, never started again by the portal's recovery; `tests/ops/test_ops_isolation.py`, `tests/hosting/test_runtime_isolation.py` | the verifier found that a changed policy restarting running wikis lifted the isolation, and that the controller recorded it in another folder than the portal reads when `HOSTING_DATABASE_PATH` is moved; both closed in this pass (`test_a_policy_change_does_not_start_an_isolated_wiki`, `test_the_portal_applies_a_policy_without_starting_wikis_explicitly`, `test_isolation_is_recorded_where_the_portal_reads_it`) |
| 117 | OPS2-09 | low | supply chain | A release without one of its lock files silently fell back to unpinned ranges | fixed: refused before anything changes unless `--allow-unpinned`; releases before 1.7 install their ranges with a warning; `tests/ops/test_ops_release_locks.py` | closed; the residual (`backups restore` had no `--allow-unpinned`) was fixed in the final pass: `tests/ops/test_ops_release_locks.py::test_a_remote_snapshot_of_such_a_release_is_restored_only_with_allow_unpinned`; so was the verifier's note that an annotated `__version__: str = "1.7.1"` read as no version, so as a release from before 1.7 (`::test_an_annotated_version_counts_like_a_plain_one`) |
| 118 | UP-4 | low | upgrade | Wiki migrations 8-10 called feature code, so a later change to a feature could change what they create | fixed: each carries its own frozen DDL (11 too); `tests/test_wiki_migrations_frozen.py` | no gap reported by the verifier |
| 119 | FNU-2 | low | chat privacy | A blocked member could tell they were blocked | fixed: messages in an existing conversation are accepted and withheld (`chat_messages.withheld`), the blocker is not suggested, new conversations and group adds answer as for an inactive account; `test_chat_privacy.py::test_a_blocked_member_is_not_told_in_a_conversation_they_already_have`, `tests/features/test_chat_blocks.py` | the verifier found two more tells (re-adding a member, opening an existing conversation); closed in the final pass (`test_adding_a_member_who_blocked_you_answers_as_for_any_member`, `test_starting_a_conversation_with_someone_who_blocked_you_reads_like_an_inactive_name`); PoC re-run: the answers match. Accepted: seeing the other person post in a shared group while a new conversation is refused still hints at a block (BLOCKERS.md) |
| 120 | FNU-3 | low | privacy | Chat moderation views and exports showed the IP addresses of accounts the administrator may not manage | fixed: hidden per sender, as on the sessions pages; `test_chat_privacy.py::test_moderation_views_hide_addresses_of_accounts_the_administrator_may_not_manage` | no gap reported by the verifier |

### Changes made with this round

* Kanban done columns (schema 11): finished work no longer shows as overdue;
  `tests/features/test_kanban_done_columns.py`. The visual pass had asked for
  it and it needed a data model change. Its toggle is a check-mark icon
  button (named "Done column: <title>", explained by its tooltip), so at the
  default column width the column tools keep one row with the delete button
  (`test_the_done_toggle_is_an_icon_so_the_column_tools_keep_one_row`;
  checked in Chromium at 375, 768, 1280 and 1440 px, light and dark, English
  and Italian, axe clean).
* The wiki's two-step page ends the pending sign-in with "Back to sign-in"
  (`POST /login/two-step/cancel`, a form with the CSRF token) instead of a
  link to `/login` that left the password step passed for five minutes;
  `test_auth_two_step.py::test_leaving_the_second_step_forgets_the_pending_sign_in`,
  `::test_leaving_the_second_step_needs_the_pages_csrf_token`.
* Hosting: "Back to sign in" on the two-step page ends the pending sign-in
  (`POST /login/mfa/cancel`); creating a wiki refuses an administrator login
  typed without the tick box; denied accounts wait one day as documented
  (hosting schema 7); each with tests in `tests/hosting/`.
* Italian calls a wiki feminine everywhere and the portal uses the wiki's
  names for shared features; tests pin both catalogues and the built-in docs.
* REST API version 2.3.0 (`test_status_and_openapi_need_no_token`).
* The mascot (pull request #21) was merged and reviewed (routes, CSRF,
  sprite, slots, CSP): British "Customise", singular counts, one translated
  audit action per button, the hint's spacing, room for the site name on
  phones, a still mascot with the system's reduced motion;
  `tests/features/test_mascot.py`.
* Tests that depended on the host's speed now control time: the in-memory
  rate limiter's clock is stopped where a test sends N+1 requests
  (`frozen_limiter_clock` in `tests/conftest.py`), `Retry-After` is checked
  against the time the calls took, and the concurrent portal sign-in test
  gives SQLite's write queue room. Its "database is locked" failures were
  measured to come from slow commits on a saturated disk (an injected 1 s
  commit reproduces them with the default 5 s busy timeout and not with
  30 s); no request holds the write lock while it waits for anything else.

### Visual and accessibility pass

The wiki (247 pages) and the hosting portal (109 pages) were crawled in
Chromium at 375, 768 and 1440 px, light and dark, English and Italian, with
axe-core (WCAG 2.1 A and AA), a keyboard walk and screenshots that were
reviewed by eye. The first crawl found 87 automated issues and 494 reviewed
ones in the wiki, 30 and 193 in the portal; three fix rounds with rechecks
followed (summarised in CHANGELOG.md, "Interface and accessibility"). Left by
design: the canvas stage is a pan-and-zoom viewport, so text past its edge
is clipped on phones; an intermittent console error comes from YouTube's
player inside embedded videos (the wiki does not delegate the Compute
Pressure API to it); native file and date controls follow the browser's
language; dates are ISO everywhere; a wrapped breadcrumb line may end with
its separator; read-only comparison tables (leaderboard, API call log)
scroll rather than stack; bare URLs are not turned into links (angle-bracket
links are). The final crawl of this tree, with the extra widths 700, 950 and
1180 px, is in READINESS.md.

## Production incident: update onto ext4 storage (October 2026)

**What happened.** A hosting server (Docker from the distribution's
`docker.io` package without BuildKit, the wiki storage on ext4, a reverse
proxy other than Caddy in front, hourly automatic updates following `main`)
ran `b8bc352` (2 October). Its update to `main` at `9591b29` failed twice.
First the tenant image did not build: since `1f2c8ed` (7 October) the
Dockerfiles use `RUN --mount`, which needs BuildKit, and
without the `docker-buildx` package Docker fell back to the legacy builder
("the --mount option requires BuildKit"); the update said only "docker build
failed (exit 1)". With `docker-buildx` installed, the update stopped the
wikis, switched release, and the new runtime agent refused to start them
("Tenant storage must be an XFS filesystem with enforced project quotas");
after the 360 s readiness timeout it rolled back. Nothing was lost, but
automatic updates skip only the exact commit that failed, so each new commit
on `main` repeated the 6-minute outage until automatic updates were paused.

**Root causes.**

1. Hard per-wiki XFS project quotas were made unconditional (the agent
   refused any other storage) for platforms that already ran on ext4, with
   no setting to keep them running as under 1.6.
2. An update runs the **installed** controller. `b8bc352` predates the
   storage preflight (`777ef39`, 8 October), and no installed controller can
   know what a newer release will refuse, so it stopped the wikis first and
   found out only after the readiness timeout.
3. BuildKit became a requirement without a check or a message naming the
   package (the `docker.io` package of Debian and Ubuntu does not pull in
   `docker-buildx`).

**Why the audits missed it.** The upgrade-path checks covered the data (1.6
databases and files under 1.7) and the 1.7 controller updating to later
releases, on simulated storage that enforced quotas. No test ran the managed
update the way a server installed before the preflight performs it: the old
controller, the new release, storage without XFS quotas. CI builds the
images on GitHub's runners, which have `buildx`, so the missing component
never showed.

**Fixes** (each with a regression test that failed first; the files are
`tests/ops/test_ops_tenant_quota_modes.py`, `tests/hosting/test_runtime_quota_modes.py`
and, for SEC-4, `tests/ops/test_ops_agent.py`):

* `BW_HOSTING_TENANT_QUOTAS` (`auto`, which became the default for new
  platforms too with the owner's decision below: quotas where the storage
  enforces them, never a silent fallback; `required`, an explicit opt-in;
  `off`), with `config get|set tenant-quotas`,
  `install --tenant-quotas`, `status` and a portal banner; under an older
  updater's units the 1.7 agent starts the wikis without quotas instead of
  refusing them (`test_after_an_old_updater_install_the_maintenance_service_starts_every_wiki_without_hard_quotas`).
* The new release's own `banana preflight`, run before anything stops; a
  refusal records the commit (`failed-revision.json` with stage, reason and
  time) so automatic runs do not repeat it
  (`test_the_new_release_checks_this_server_before_anything_stops`).
* BuildKit checked before preparing, naming `docker-buildx` /
  `docker-buildx-plugin`; a failed build shows Docker's last lines
  (`test_an_update_without_buildkit_stops_nothing_and_names_the_package`).
* UPGRADING.md: updating from a controller before 1.7, moving the wiki
  storage to XFS on an attached block volume, and the checkout path that runs
  the 1.7 controller for that one update.

**Verification and follow-ups.** The update was then replayed end to end
without root or Docker: the real `b8bc352` controller as the installed
updater, two wikis created by its own portal, real ext4 statfs, generation 2
units, no Caddy, the 1.7 tree as the candidate; then the 1.7 convergence and
later updates. It came up without downtime beyond the update itself. The
verifiers' findings and what was done:

| ID | Severity | Finding | Status and regression tests |
|---|---|---|---|
| R1 | medium | A failed unit convergence (the `update` after an older updater installed 1.7) was not remembered: every automatic run stopped the services and waited out the readiness timeout again | fixed: recorded in `config/failed-convergence.json` with an event, shown by `status`; automatic runs answer `paused` until `update`, `restart` or a new revision (`test_a_failed_unit_convergence_is_not_repeated_by_every_automatic_run`, `test_a_paused_convergence_does_not_hold_back_a_new_revision`, `test_a_restart_that_installs_the_units_ends_a_paused_convergence`) |
| SEC-1 | medium | Without hard quotas the agent no longer compared the started container's `/data` with the tenant directory, so a directory swapped for a link at `docker run` was mounted and its gate released | fixed: the directory is pinned (`O_NOFOLLOW`), live aliases are refused, and the gate is released only once `/proc/<pid>/root/data` is that inode; an agent under an older updater's units (no `CAP_SYS_PTRACE`) cannot compare, logs it once and starts as 1.6 did (`test_without_hard_quotas_a_directory_swapped_at_start_is_refused`, `test_without_hard_quotas_a_task_on_a_swapped_directory_is_refused`, `test_an_agent_unit_that_cannot_inspect_mounts_starts_wikis_as_16_did_and_says_so`) |
| VCR-1 | medium | CHANGELOG still said unsupported storage fails closed and listed none of the incident fixes | fixed (documentation) |
| R2, VCR-2 | low | `status` kept asking to retry `9591b29`, which the installed release contains (an older updater never clears its record) | fixed: such a record is ignored and removed (`test_a_failed_revision_the_platform_has_moved_past_is_forgotten`) |
| R3 | low | Without Caddy the agent logged "Could not refresh tenant routes" at every start and stop | fixed (`test_a_host_without_caddy_does_not_log_routing_warnings`) |
| R4, VCR-10 | low | Wikis without a plan go from unlimited to the 10 GiB ceiling without hard quotas; UPGRADING said "as in 1.6" | documented in UPGRADING.md (check sizes, raise `HOSTING_AGENT_MAX_STORAGE_BYTES`); same limit as under hard quotas, kept |
| R5, VCR-9 | low | The Italian banner showed the agent's English reason; with `off` it advised moving to XFS | fixed: the reason is labelled as the server's detail (`lang="en"`), `off` has its own message naming `config set tenant-quotas`, as a note rather than a warning (`test_administrators_are_told_when_wikis_run_without_hard_quotas`) |
| R6 | low | The 1.7 agent's container and network commands never ran against a real Docker | CI now starts, tasks and stops two wikis through the agent against the runner's Docker (`.github/workflows/ci.yml`, not run locally: no Docker here; BLOCKERS.md); the candidate preflight now refuses the two known rollback causes, tenant plugins in port or onion mode and a kernel without IPv6 (`test_this_releases_preflight_refuses_what_would_keep_its_portal_or_wikis_down`, `test_the_preflight_decides_the_hosting_mode_as_the_portal_does`) |
| SEC-2 | low | Python 3.14's `Path.exists()` answers False when the quota registry cannot be examined, so `auto` failed open | fixed: `os.lstat`, any error but "not found" counts as used, so since the owner's decision below a loss is reported loudly rather than as the calm notice (`test_a_quota_registry_that_cannot_be_read_counts_as_used`, `test_a_quota_registry_the_controller_cannot_read_counts_as_used`) |
| SEC-3 | low | Stickiness is per server; `install --restore` ignored `--tenant-quotas` | fixed: refused with `--restore` (`test_a_restore_does_not_silently_ignore_a_quota_setting`); per-server memory documented; since the owner's decision below the registry only decides between the notice and the loud warning, and `required` is the choice for refusal |
| SEC-4, VCR-6 | low | The in-process fallback (no agent socket) now runs wikis without quotas under `auto`, against RUNTIME_AGENT.md | kept (refusing would roll back every first update by the 1.4 updater) and documented; `test_the_in_process_fallback_follows_the_quota_setting` |
| SEC-5 | low | Docker not answering or a preflight timeout recorded the revision as failed | fixed: the preflight answers `retryable`, the controller records nothing for it or a timeout (`test_a_preflight_held_up_by_the_host_is_not_the_revisions_failure`, `test_this_releases_preflight_says_when_docker_held_it_up`) |
| SEC-6 | low | `status` reported the controller's view of quotas (true on XFS) while the agent ran under units without quota calls | fixed: `status` and `config get` report the agent's view (`test_status_reports_what_an_agent_under_older_units_can_do`) |
| SEC-7 | low | A buildx builder that keeps its results to itself built "successfully" without a tenant image | fixed: the image must exist after the build, before anything stops (`test_a_build_that_leaves_no_tenant_image_fails_before_anything_stops`) |
| SEC-8 | low | Nothing connected tenant plugins with the absence of hard quotas | `status` warns, UPGRADING.md says so (`test_status_warns_when_tenant_plugins_run_without_hard_quotas`) |
| VCR-3 | low | `off` on XFS keeps the kernel limits already set | documented; `config set tenant-quotas off` says how to clear them (`test_turning_quotas_off_on_xfs_says_that_the_limits_already_set_stay`) |
| VCR-4 | low | The Docker and Compose docs did not say BuildKit is needed | documented in docs/deployment.md, UPGRADING.md and the Dockerfile |
| VCR-5 | low | `# syntax=docker/dockerfile:1` made every build look the frontend up on Docker Hub, breaking the registry-unreachable fallback | removed from both Dockerfiles (the built-in frontend of Docker 20.10+ has `RUN --mount`); `test_the_images_build_without_fetching_a_dockerfile_frontend`; the CI image builds confirm it |
| VCR-7 | low | XFS platforms get `auto`, whose stickiness depends on the registry | superseded by the owner's decision below: `auto` is no longer sticky and warns loudly when quotas are lost; `required` is documented as the opt-in for refusal |
| VCR-8 | low | After the older-updater path the running release keeps dependencies from `requirements.txt` ranges until the next release | documented; the checkout path avoids it |

**Owner decision: XFS is optional.** After this round the owner decided
that no installation or update requires XFS. Hard per-wiki quotas are used
automatically only where the storage supports them; each wiki's size limit
(its plan, `INSTANCE_STORAGE_LIMIT_MB` 500 MB by default, the upload checks,
the portal's 5-minute storage scan that suspends a wiki above 110%, and
`HOSTING_AGENT_MAX_STORAGE_BYTES` for a wiki without a plan) stays, enforced
by the application. So new installations default to `auto` too
(`tenant_quotas.INSTALL_DEFAULT`), and `install` never refuses another
filesystem: it says which limits apply and how to get hard quotas. `auto` is
no longer sticky: when the quota registry shows that hard quotas were
enforced and the storage no longer enforces them, the wikis start without
them, and the agent's log (at its start and when it happens), `status`
(`tenant_storage.lost`, in `warnings`) and the administration page (a
warning banner) say "hard quotas were enforced on this platform before and
are no longer available" with the reason; storage that never had them gets
a calm notice (`notes`, an information banner). `required` stays as the
operator's explicit opt-in, with the refusal before anything stops
described above; `off` is unchanged. The tests that asserted the old
behaviour now assert the new one (`test_the_three_settings_decide_as_documented`,
`test_new_platforms_default_to_auto`,
`test_auto_keeps_the_wikis_running_once_quotas_are_lost_and_says_so_loudly`,
`test_required_refuses_an_update_on_storage_whose_quotas_were_lost_before_anything_stops`,
`test_a_new_hosting_platform_installs_on_any_storage_and_says_which_limits_apply`,
`test_a_new_hosting_platform_requires_hard_quotas_only_when_the_operator_asks`,
`test_auto_starts_the_wikis_once_quotas_are_lost_and_says_so_loudly`,
`test_quotas_lost_while_the_agent_runs_are_logged_when_it_happens`,
`test_administrators_are_told_when_wikis_run_without_hard_quotas`,
`test_the_storage_notices_are_translated`). The banners were read at 375
and 1440 px in English, Italian and German, light and dark: on a phone
`(BW_HOSTING_TENANT_QUOTAS=required),` ran into the banner's padding, so a
setting now breaks after its `=` and other long tokens wrap
(`test_the_storage_banner_wraps_settings_and_paths_on_a_phone`). The
security trade-off is recorded in BLOCKERS.md.

A review of that change found a few small faults, all fixed (behaviour with
tests written first). Every update printed the calm notice of storage
without hard quotas as a warning, then again as a note: the new release's
preflight listed it under `warnings`. It now answers such notes under
`notes` (still protocol 1: a controller that does not know the key ignores
it), and the controller prints them as notes and does not repeat what its
agent then decides (`test_an_update_says_once_what_the_wikis_storage_gets`,
`test_the_preflight_notes_of_the_new_release_are_reported_as_notes`). The
calm banners (never had hard quotas, `off`) no longer said that a wiki can
fill the disk between two checks, and said its limit was "still enforced":
they now say "still checked" and "at worst, fill the disk" in English,
Italian and German. The portal's notice and the loss warning (agent log,
`status`, portal) blamed the storage even when the agent ran under units an
older controller wrote: they now point to `status`, and to `restart` for
that case. The Italian and German `off` banner is tested again
(`test_the_storage_notices_are_translated`). In UPGRADING.md the undo of
the move to XFS no longer claims that a `required` set in step 7 would
refuse the wikis (it stays on the volume), and deployment.md no longer
suggests putting only `data/` on XFS.

The mascot merge (pull request #21) also left the portal's help without the
new default plugin; the help now lists it in English and Italian
(`test_help_marks_the_plugins_a_new_wiki_starts_with`).

## Upstream merge: German and the third hardening round (October 2026)

Upstream `main` moved from `9591b29` to `3dde316` with two pull requests by
Dhareck: #22 (German interface) and #23 (his third hardening round). They
were merged into the 1.7 tree as a three-way merge on `9591b29`, file by
file, not as a Git merge (BLOCKERS.md says how to record it). The R- and S-
numbers are Dhareck's: they come from his own review, are not defined in
this repository and appear in his commit messages, test names and code
comments. They are not the R1-R6 and SEC- rows of the production incident
above. R-07, R-23 and R-27 continue findings of the same numbers from #19
and #20. Where both sides changed the same code, both behaviours were kept,
each combination with a regression test written first. Upstream's tests were
kept; the few that assumed the other side's old behaviour were adapted (named
below).

| Upstream | What it changes | Where it meets 1.7 | Resolution |
|---|---|---|---|
| R-07 (#23) | An update that fails only because wikis with their own plugins do not serve quarantines those plugins (`hosting-admin instance quarantine-plugins`) and is kept when they serve again | #75 and OPS2-08 (#116): at most one in ten unready wikis is isolated instead of rolling back (`BANANA_UPDATE_UNREADY_WIKIS`) | One sequence in `ops/manager.finish()`: upstream's quarantine first (when every failing wiki holds plugins), then isolation of what still fails under `isolate`, otherwise rollback; under `rollback` upstream's behaviour is unchanged. `ops/system.run()` takes upstream's rule that an environment passed in is the whole environment, so `hosting-admin` run as the service account no longer inherits root's; the BuildKit build passes its own. A quarantine that fails (its task migrates the wiki's database first, so a full quota or data the release refuses fails it too) is that wiki's own failure under `isolate`: it is isolated with the rest, and the update rolls back only when isolation cannot take them. The portal sets the quarantine marker before that task: a wiki whose marker is then in force is reported so (isolation reason and warning), with its plugins still switched on in its database. `last-update.json` keeps `quarantined_tenants` for `status`. `test_a_wiki_still_failing_with_its_plugins_quarantined_is_isolated_under_the_isolate_policy`, `test_a_failing_wiki_without_plugins_is_isolated_without_quarantining_any`, `test_a_wiki_whose_plugins_cannot_be_quarantined_is_isolated_under_the_isolate_policy` (with and without the marker), `test_a_failed_quarantine_never_keeps_the_others_from_theirs`, `test_failed_quarantines_on_more_wikis_than_isolation_takes_roll_the_update_back`, `test_the_wikis_an_update_quarantined_stay_in_status_after_later_operations`, `test_the_portal_command_run_as_the_service_account_gets_none_of_the_controllers_environment`; upstream's R-07 tests in `tests/ops/test_ops_recovery.py` set the rollback policy |
| R-23 (#23) | Database copies, wiki exports, plugin snapshots and duplicates are checked against the size and SHA-256 the snapshot reported and the wiki's byte budget; exports keep to the storage limit plus a tenth and 64 MiB and store hard links once | #78 (the controller copies a tenant's database as bytes), the platform-backup bound of the third round (upstream R-23 from #20), 1.7's quota-aware duplicate and #90 (UP-1: wikis adopted above their plan) | `duplicate()` keeps 1.7's storage preparation under the quota mode and upstream's `save_copy(..., max_bytes=budget)`. A wiki adopted above its plan is budgeted from the hard limit it was adopted at (`platform_backup.held_limit`, from the portal's `over-plan.json`), which its quota holds it to: from the plan alone its genuine database was left out of every platform backup and its download refused, while a sparse file past the adopted limit is still refused. `tests/hosting/test_runtime_archives.py`, `tests/hosting/test_runtime_agent.py`, `test_a_wiki_held_above_its_plan_keeps_its_database_in_the_platform_backup`, `test_a_wiki_held_above_its_plan_can_still_be_downloaded`, `test_a_plugin_snapshot_of_a_wiki_held_above_its_plan_is_kept`, `test_the_limit_a_wiki_was_adopted_at_bounds_its_archives`. Without hard quotas nothing bounds a wiki above its plan, so the plan stays its budget (BLOCKERS.md) |
| R-27 (#23) | Gunicorn disconnects a client once a write has waited `BW_WRITE_TIMEOUT` / `HOSTING_WRITE_TIMEOUT` seconds (`ops/write_timeout.py`) | #28: the portal's proxy fallback cuts a response after 300 seconds and keeps a slot free | Different layers, both kept; `tests/ops/test_ops_write_timeout.py` |
| S-30 (#23) | An account merge no longer leaves the target as a collaborator of its own wikis | #66: an accepted transfer no longer keeps the previous owner as a collaborator | Complementary, both kept; no migration (UPGRADING.md gives a one-off SQL clean-up for earlier merges); `tests/hosting/test_hosting_merge_collaborators.py` |
| (#23) | Whole-site imports refuse encrypted members and compression other than stored or deflated before reading anything | #36: whole-site export and import are for owners and superusers, imports drop foreign triggers and views | Complementary, both kept. One set of member rules for every import of an uploaded archive (`core/zipcheck.py`: the whole-site and board imports, hosted wiki imports, platform restores), so patched and strongly encrypted members are refused before they are read everywhere; `tests/features/test_site_admin_migration.py`, `test_import_refuses_members_zipfile_cannot_unpack_like_a_site_import`, `test_restore_refuses_members_zipfile_cannot_unpack_like_a_site_import` |
| (#23) | Portal log lines with form or runtime values (bulk administrator actions, a stuck wiki move) replace line breaks (`core.web.one_line`) | #31: `BW_LOG_FORMAT=json` / `HOSTING_LOG_FORMAT=json` | Complementary: the text format is safe at those lines too. The helper moved to `core.logformat.one_line` (re-exported by `core.web`), so the standard-library-only controller uses the same one, and it now also covers the names a wiki gives its own files and folders where the portal and the controller log them (folders nested too deep, files an export or backup leaves out, files a snapshot cannot stage); `tests/test_core_hardening.py` and the `test_tenant_file_names_never_start_a_log_line_of_their_own` tests |
| #22 | German for the wiki, the portal and its help centre, the desktop launcher and the built-in guide; `Accept-Language: *` and ties that include the default get the default language | 1.7's interface work: `HOSTING_SERVICE_NAME` (`{service}` in hosting strings, `hosting.brand` and `email.eyebrow` removed), British English, Italian *la wiki*, the built-in guide's title rule and its **Administration → Documentation** path, the maintenance page per language, the portal's decimal comma, the per-language legal addresses | The German catalogues follow 1.7's keys and placeholders (`tests/test_core_translations.py` holds every catalogue to English); the maintenance page is built for every portal language with upstream's tie rule (`test_the_maintenance_gate_has_a_page_in_every_portal_language`); German sizes use the decimal comma; `HOSTING_TERMS_URL_DE` and `HOSTING_PRIVACY_URL_DE` follow from the language list; upstream's `test_user_guide_mirrors_english` checks the title line in the file, since 1.7 turns it into the page title. German follows Dhareck's informal "du" and "das Wiki" (`test_german_never_reads_as_the_formal_address`, `test_german_treats_wiki_as_neuter_and_addresses_the_reader_with_du`); long German compounds are hyphenated on German pages only (`test_long_german_compounds_are_hyphenated_where_they_must_break`, `test_a_long_feature_name_stays_in_its_column`) |

Upstream's documentation names no deployment, organisation or server; the
public-repository check (`tests/test_project_names.py`) covers the merged
tree.

## Upstream merge: German for the third round and the mascot's polish (October 2026)

Upstream `main` then moved from `3dde316` to `519ac7d` with two more pull
requests by Dhareck: #24 (German for two messages of #23) and #25 (the
mascot's polish). They were merged like #22 and #23: a three-way merge on
`3dde316`, file by file, not as a Git merge (BLOCKERS.md says how to record
it). Where both sides changed the same thing there is one implementation,
with both sides' intent; no test of either side was removed, and the two
upstream assertions that assumed upstream's names are adapted (named
below).

| Upstream | What it changes | Where it meets 1.7 | Resolution |
|---|---|---|---|
| #24 | German for two of #23's messages, in the portal and in Admin → Site migration | 1.7's German catalogues, completed in the previous merge | Dhareck's wording taken |
| #25 | The mascot's script reads its click count and the address it saves to from data attributes on its own button; the separate settings block (`mascot/_scripts.html`) is gone | 1.7 loaded the script through the `page.scripts` slot, compatible with the CSP nonce | Upstream's approach taken: the stylesheet and a deferred same-origin script sit in `mascot/_topbar.html`, with no inline script, which fits the CSP (`script-src 'self'` plus nonce, `style-src-elem 'self'`); the mascot's `page.scripts` slot is gone, `_scripts.html` deleted. **Security:** before this, the script read its settings with `getElementById('mascot-config')`, and the settings block came after the page content, so a page heading with that id (any editor can write one: a heading "Mascot config" gets it from its text) was found first and could point an administrator's eleventh click, a `POST` carrying the CSRF token, at another endpoint of the wiki. Upstream's `tests/features/test_mascot_script.py` (a trap heading on the page) holds it |
| #25 | Saving writes only the mascot's two keys over what is stored, in one transaction, and reads them without validating the rest; the change for every account is one batch | 1.7's display preferences (`DEFAULTS`, `OWNED_KEYS`, a reset that keeps the language and the mascot) and its audit entries `mascot.everyone.<action>` with `{count}` | Upstream's `service.py` kept, on 1.7's preferences module (unchanged); the routes keep 1.7's audit names and the singular through `flash_t(..., count=...)`. Adapted: upstream's `test_mascot_service` checks the audit row as `mascot.everyone.shades_on` with `{count}` instead of `mascot.bulk_updated` with `{action, accounts}` |
| #25 | The singular for one account; the reset confirmation says what a reset keeps; German texts | 1.7's "Customise", its `_one` keys for the singular, Italian "a" | One set of keys in English, Italian and German in 1.7's convention (`<action>` the plural, `<action>_one` the singular, in upstream's wording); Italian keeps "a" (not "su"); the reset texts are upstream's. Adapted: upstream's `test_every_action_has_a_singular_and_a_plural_result` checks `<action>_one` and a base key with `{count}`, and its Italian expectations use "a" |
| #25 | The mascot stands still under reduced motion, tilts only where hovering is real, and is a 2.75rem target on touch screens | 1.7's reduced-motion rule, its blink of the button icons and its 5-pixel pull on phones | One reduced-motion block, last in `mascot.css` as upstream's test asks, covering the button icons' blink too; both sides' touch and phone rules kept. `test_on_touch_phones_the_finger_sized_mascot_stays_clear_of_the_menu_button`: the pull stays within the top bar's gap, so the 44-pixel box never covers the menu button (it fails with a 7-pixel pull). The space this takes from long site names on touch phones is an owner decision (BLOCKERS.md) |
| #25 | Mascot documentation for administrators and users | 1.7's own mascot section in `docs/features.md` and the user guide | One mascot section in `docs/features.md` and one paragraph under Customise in `docs/user-guide/en` and `it` |

The review of this merge and of the new icons (below) found:

| # | Severity | Finding | Status |
|---|---|---|---|
| UM-1 | medium | The release records still described `origin/main` at `3dde316` and did not mention #24, #25 or the redirect closed by #25 | fixed: BLOCKERS.md, READINESS.md and this section |
| UM-2 | low | The mascot's button icons under Customise and in Appearance were 28 pixels, 1.75 screen pixels per art pixel: uneven rows and blurred eyes at 1x | fixed: 32 pixels, twice the art, with block margins that keep the buttons as tall as before (`test_the_button_icons_draw_the_banana_at_twice_its_grid`) |
| UM-3 | low | The desktop launcher gave the window manager only the 256-pixel icon, which it scaled to 16 to 48 pixels itself, blurring the pixel art | fixed: 256, 48, 32 and 16 pixels, each an exact enlargement of the art (`test_the_desktop_window_gets_the_banana_at_exact_sizes`). The Windows icon's missing 24-pixel frame is an owner decision (BLOCKERS.md) |
| UM-4 | low | The hosting portal answered `/favicon.ico` with 404; the wiki and BananaChat send their icon | fixed: a redirect to the versioned icon, outside the account gates (`test_browsers_asking_for_favicon_ico_get_the_banana`) |
| UM-5 | low | A custom page shown in its sandbox linked the favicon but not the home-screen icon | fixed (`test_a_custom_page_in_its_sandbox_links_the_home_screen_icon_too`) |
| UM-6 | low | `test_reruns_write_the_same_bytes` reused the generator's cached drawings, so it could not catch output that changes between runs | fixed: each run loads the script afresh; a copy of the script with a random compression level now fails it |
| UM-7 | low | A display preference of `1e999` (infinity, which `int()` cannot hold) made every page answer 500, from a saved account, a public visitor's cookie or the display form; the mascot's own reader of #25 already caught it | fixed (`test_numbers_too_large_for_an_integer_are_dropped_not_a_server_error`) |
| UM-8 | low | A test comment and a documentation line still explained the phone layout with the earlier 23-pixel logo, and a line of `docs/features.md` was not wrapped | fixed |
| UM-9 | low | The icon's dark outline merges into near-black backgrounds | owner decision (BLOCKERS.md); the website's screenshots, which still show the earlier icon in the apps' top bars, are open in the website's AUDIT.md (#35) |

## Owner decision: the mascot's banana as every icon (October 2026)

The owner made the mascot's pixel-art banana (its smile, without the
sunglasses) the icon of every BananaSuite app, for static icons only: the
moving mascot stays inside wikis, never in the hosting portal, BananaChat or
the website. `scripts/make_icons.py` draws every icon from
`mascot/sprite.py` with each art pixel a whole square (CHANGELOG.md, "New
icons"); `tests/test_icons.py`, `tests/features/test_site_icons.py` and
`tests/hosting/test_hosting_icons.py` hold the drawings to the sprite, the
links to content versions and the logos to 32 pixels without smoothing.
