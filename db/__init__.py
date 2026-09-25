"""
BananaWiki: Database layer (SQLite)

This package re-exports every public symbol from the internal sub-modules so
that ``import db`` and ``db.function_name()`` continue to work exactly as
before.
"""

import sqlite3

from ._connection import (  # noqa: F401  (keep first (used by other sub-modules))
    get_db,
    get_db_context,
    create_consistent_backup,
    retry_on_busy,
    get_db_observability_snapshot,
    integrity_check,
    write_serialized,
    SYSTEM_USER_ID,
)

# Re-exported as a db-layer alias so route modules never import sqlite3
# directly: the database driver stays an implementation detail of this package.
IntegrityError = sqlite3.IntegrityError

# Schema
from ._schema import init_db  # noqa: F401

# Blob storage
from ._blobs import (  # noqa: F401
    store_blob,
    get_blob,
    get_blob_content,
    delete_blob,
)

# Users & editor access
from ._users import (  # noqa: F401
    _gen_user_id,
    generate_random_id,
    is_suspension_active,
    check_suspension_expired,
    create_user,
    create_signup_user,
    complete_initial_setup,
    get_user_by_id,
    get_user_by_username,
    _ALLOWED_USER_COLUMNS,
    update_user,
    delete_user,
    record_username_change,
    get_username_history,
    _A11Y_DEFAULTS,
    _clean_a11y_pref,
    get_user_accessibility,
    save_user_accessibility,
    record_login_attempt,
    count_recent_login_attempts,
    clear_login_attempts,
    clear_all_login_attempts,
    record_rate_limit_hit,
    count_recent_rate_limit_hits,
    check_and_record_rate_limit_hit,
    clear_all_rate_limit_hits,
    prune_stale_rate_limit_hits,
    list_users,
    count_admins,
    count_owners,
    log_impersonation_start,
    log_impersonation_stop,
    get_editor_access,
    set_editor_access,
    set_user_chat_disabled,
    is_user_chat_disabled,
    mass_logout_all_users,
    update_username_mentions,
    get_pending_users,
    count_pending_users,
    approve_user,
    deny_user,
    cleanup_denied_users,
    cleanup_pending_users,
)

# Persistent login sessions
from ._sessions import (  # noqa: F401
    create_user_session,
    get_user_session,
    get_user_session_id,
    list_active_user_sessions,
    list_user_session_history,
    clear_user_session_history,
    touch_user_session,
    revoke_user_session,
    revoke_user_session_token,
    revoke_other_user_sessions,
    revoke_all_user_sessions,
)

# Invite codes
from ._invites import (  # noqa: F401
    generate_invite_code,
    validate_invite_code,
    use_invite_code,
    delete_invite_code,
    hard_delete_invite_code,
    list_invite_codes,
    list_expired_codes,
    get_invite_code_usage,
)

# Categories
from ._categories import (  # noqa: F401
    create_category,
    get_category,
    update_category,
    is_descendant_of,
    update_category_parent,
    delete_category,
    count_pages_in_category,
    list_categories,
    get_category_tree,
    update_pages_sort_order,
    update_categories_sort_order,
    search_categories,
)

# Pages & attachments
from ._pages import (  # noqa: F401
    update_category_sequential_nav,
    get_adjacent_pages,
    update_page_slug,
    search_pages,
    search_pages_full,
    create_page,
    get_page,
    get_page_by_slug,
    get_home_page,
    set_home_page,
    get_pages_in_category,
    list_searchable_pages,
    list_active_pages_for_tts,
    list_all_pages,
    update_page,
    update_page_title,
    update_page_category,
    VALID_DIFFICULTY_TAGS,
    PAGE_PROTECTION_ADMIN_UNLOCK_DELAY_SECONDS,
    set_page_protection,
    clear_page_protection,
    request_page_protection_unlock,
    list_protected_pages,
    update_page_tag,
    set_page_deindexed,
    delete_page,
    get_page_history,
    get_history_entry,
    transfer_history_attribution,
    bulk_transfer_history_attribution,
    delete_history_entry,
    clear_page_history,
    _UPLOAD_REF_RE,
    get_all_referenced_image_filenames,
    add_page_attachment,
    get_page_attachments,
    get_page_attachment,
    delete_page_attachment,
    propagate_mention_rename,
    propagate_mention_deletion,
)

# Drafts
from ._drafts import (  # noqa: F401
    save_draft,
    get_draft,
    get_drafts_for_page,
    delete_draft,
    transfer_draft,
    get_user_draft_count,
    list_user_drafts,
    save_page_builder_draft,
    save_page_builder_draft_if_current,
    get_page_builder_draft,
    delete_page_builder_draft,
)

# Editing sessions
from ._editing_sessions import (  # noqa: F401
    heartbeat,
    get_active_editors,
    remove_session,
    cleanup_stale,
)

# Site settings
from ._settings import (  # noqa: F401
    get_site_settings,
    _ALLOWED_SETTINGS_COLUMNS,
    update_site_settings,
    get_last_backup_sent_at,
    check_and_set_backup_sent,
    check_and_claim_chat_cleanup,
    get_last_server_restart_at,
    check_and_claim_server_restart,
)

# Announcements & contributions
from ._announcements import (  # noqa: F401
    create_announcement,
    get_announcement,
    list_announcements,
    _ALLOWED_ANN_COLUMNS,
    update_announcement,
    delete_announcement,
    get_user_contributions,
    get_active_announcements,
    get_visible_announcement,
    get_announcement_audience_user_ids,
)

# Import/export
from ._migration import (  # noqa: F401
    _MIGRATION_VERSION,
    _EXPORT_TABLES,
    export_site_data,
    import_site_data,
)

# User profiles
from ._profiles import (  # noqa: F401
    get_user_profile,
    upsert_user_profile,
    delete_user_profile,
    list_published_profiles,
    list_all_users_with_profiles,
    get_contribution_years,
    get_contributions_by_day,
    get_profile_group_badges,
    get_user_profile_group_settings,
    set_profile_group_badge_visible,
    clear_profile_group_badge,
)

# Direct messages
from ._chats import (  # noqa: F401
    get_or_create_chat,
    get_chat_by_id,
    is_chat_participant,
    get_user_chats,
    get_chat_messages,
    get_chat_message_by_id,
    send_chat_message,
    add_chat_attachment,
    get_user_chat_attachment_count_today,
    get_chat_attachment,
    get_all_chats_admin,
    get_user_chats_admin,
    get_all_messages_for_backup,
    cleanup_old_chat_messages,
    cleanup_old_chat_attachments,
    clear_chat_messages,
    delete_chat_message,
    increment_unread_count,
    reset_unread_count,
    get_total_unread_dm_count,
    get_all_messages_for_chats,
)

# Audit / role history / custom tags / contribution management
from ._audit import (  # noqa: F401
    record_role_change,
    get_role_history,
    get_user_custom_tags,
    add_user_custom_tag,
    update_user_custom_tag,
    delete_user_custom_tag,
    reorder_user_custom_tags,
    get_user_custom_tag,
    deattribute_contribution,
    deattribute_all_user_contributions,
    delete_role_history_entry,
    delete_all_role_history,
    get_role_history_entry,
    mass_reattribute_contributions,
    record_suspension_action,
    get_suspension_history,
)

# Group chats
from ._groups import (  # noqa: F401
    generate_invite_code_for_group,
    create_group_chat,
    get_or_create_global_chat,
    get_group_chat,
    get_group_chat_by_invite,
    is_group_member,
    get_group_member,
    get_group_member_role,
    add_group_member,
    remove_group_member,
    get_group_members,
    set_group_member_role,
    set_group_member_timeout,
    is_group_member_timed_out,
    get_user_groups,
    send_group_message,
    send_group_system_message,
    get_group_messages,
    add_group_attachment,
    get_group_attachment,
    delete_group_message,
    get_group_message_by_id,
    get_all_group_chats_admin,
    get_all_group_messages_for_backup,
    get_group_messages_for_export,
    cleanup_old_group_messages,
    cleanup_old_group_attachments,
    get_user_group_attachment_count_today,
    transfer_group_ownership,
    ban_group_member,
    unban_group_member,
    is_group_member_banned,
    get_group_banned_members,
    regenerate_group_invite_code,
    set_group_chat_active,
    delete_group_chat,
    clear_group_messages,
    increment_group_unread_count,
    bulk_increment_group_unread_count,
    reset_group_unread_count,
    get_total_unread_group_count,
    get_all_messages_for_groups,
)

# Custom permissions
from ._permissions import (  # noqa: F401
    get_user_permissions,
    set_user_permissions,
    has_permission,
    has_category_read_access,
    has_category_write_access,
    clear_user_permissions,
)

# Custom roles
from ._custom_roles import (  # noqa: F401
    create_custom_role,
    get_custom_role,
    list_custom_roles,
    update_custom_role,
    delete_custom_role,
    assign_custom_role,
    unassign_custom_role,
    get_users_with_role,
    get_user_custom_role,
    get_role_permissions,
)

# Badges
from ._badges import (  # noqa: F401
    VALID_TRIGGER_TYPES,
    create_badge_type,
    get_badge_type,
    get_badge_type_by_name,
    list_badge_types,
    update_badge_type,
    delete_badge_type,
    award_badge,
    revoke_badge,
    get_user_badges,
    has_badge,
    get_badge_holders,
    get_all_badge_holder_counts,
    count_user_badges,
    get_unnotified_badges,
    mark_badges_notified,
    clear_badge_notifications,
    check_and_award_auto_badges,
    revoke_all_badges_for_type,
)

# Page reservations
from ._reservations import (  # noqa: F401
    MAX_QUOTA_REQUEST_REASON_LENGTH,
    MAX_QUOTA_REVIEW_REASON_LENGTH,
    reservations_enabled,
    get_default_reserved_pages_quota,
    get_effective_reserved_pages_quota,
    get_user_active_reservation_count,
    get_pending_reservation_quota_request,
    list_reservation_quota_requests,
    get_reservation_quota_request,
    create_reservation_quota_request,
    review_reservation_quota_request,
    cancel_reservation_quota_request,
    set_user_reserved_pages_quota,
    reserve_page,
    release_page_reservation,
    get_page_reservation_status,
    cleanup_expired_reservations,
    can_user_reserve_page,
    can_user_edit_page,
    get_user_reservations,
    get_all_active_reservations,
    get_all_active_cooldowns,
    get_active_page_reservations_map,
    get_directory_reservation_statuses,
    force_release_reservation,
    admin_assign_reservation,
    admin_clear_cooldown,
    count_pending_reservation_quota_requests,
)

# Plugins
from ._plugins import (  # noqa: F401
    list_plugins,
    get_plugin,
    register_plugin,
    update_plugin,
    enable_plugin,
    set_plugins_enabled,
    disable_plugin,
    delete_plugin,
    remove_plugin,
    is_plugin_enabled,
    seed_builtin_plugins,
)

# Database cleanup & optimization
from ._cleanup import (  # noqa: F401
    cleanup_expired_invite_codes,
    cleanup_stale_drafts,
    cleanup_expired_drafts,
    cleanup_notified_badge_notifications,
    cleanup_released_reservations,
    cleanup_old_login_attempts,
    cleanup_old_rate_limit_hits,
    cleanup_soft_deleted_messages,
    cleanup_expired_announcements,
    cleanup_resolved_quota_requests,
    cleanup_revoked_badges,
    cleanup_old_role_history,
    cleanup_old_username_history,
    cleanup_expired_group_timeouts,
    cleanup_expired_suspensions,
    optimize_database,
    run_full_cleanup,
    try_acquire_cleanup_lease,
    release_cleanup_lease,
)

# Per-user upload quotas
from ._upload_quota import (  # noqa: F401
    check_and_record_upload,
    get_user_upload_usage,
)

# Kanban boards
from ._kanban import (  # noqa: F401
    create_board as kanban_create_board,
    get_board as kanban_get_board,
    list_boards as kanban_list_boards,
    update_board as kanban_update_board,
    delete_board as kanban_delete_board,
    create_column as kanban_create_column,
    get_column as kanban_get_column,
    list_columns as kanban_list_columns,
    update_column as kanban_update_column,
    update_columns_sort_order as kanban_update_columns_sort_order,
    delete_column as kanban_delete_column,
    create_ticket as kanban_create_ticket,
    get_ticket as kanban_get_ticket,
    list_tickets as kanban_list_tickets,
    list_board_tickets as kanban_list_board_tickets,
    update_ticket as kanban_update_ticket,
    move_ticket as kanban_move_ticket,
    update_tickets_sort_order as kanban_update_tickets_sort_order,
    delete_ticket as kanban_delete_ticket,
    count_board_tickets as kanban_count_board_tickets,
    set_board_visibility as kanban_set_board_visibility,
    set_board_share as kanban_set_board_share,
    remove_board_share as kanban_remove_board_share,
    get_board_shares as kanban_get_board_shares,
    clear_board_shares as kanban_clear_board_shares,
    user_can_view_board as kanban_user_can_view_board,
    user_can_write_board as kanban_user_can_write_board,
    list_boards_for_user as kanban_list_boards_for_user,
    list_public_boards as kanban_list_public_boards,
    list_boards_for_individual_user as kanban_list_boards_for_individual_user,
    user_has_any_share as kanban_user_has_any_share,
    user_can_view_board_individual as kanban_user_can_view_board_individual,
    revoke_role_shares_for_restricted_roles as kanban_revoke_role_shares_for_restricted_roles,
    get_board_accessible_users as kanban_get_board_accessible_users,
    remove_assignees_without_board_access as kanban_remove_assignees_without_board_access,
    remove_all_invalid_assignees as kanban_remove_all_invalid_assignees,
    user_has_board_access as kanban_user_has_board_access,
    # Multi-assignee
    list_ticket_assignees as kanban_list_ticket_assignees,
    list_ticket_assignee_ids as kanban_list_ticket_assignee_ids,
    set_ticket_assignees as kanban_set_ticket_assignees,
    add_ticket_assignee as kanban_add_ticket_assignee,
    remove_ticket_assignee as kanban_remove_ticket_assignee,
    # Ticket attachments
    add_ticket_attachment as kanban_add_ticket_attachment,
    list_ticket_attachments as kanban_list_ticket_attachments,
    get_ticket_attachment as kanban_get_ticket_attachment,
    delete_ticket_attachment as kanban_delete_ticket_attachment,
    # Ticket history
    add_ticket_history_entry as kanban_add_ticket_history_entry,
    list_ticket_history as kanban_list_ticket_history,
    get_ticket_history_entry as kanban_get_ticket_history_entry,
    # Ticket comments
    add_ticket_comment as kanban_add_ticket_comment,
    list_ticket_comments as kanban_list_ticket_comments,
    get_ticket_comment as kanban_get_ticket_comment,
    update_ticket_comment as kanban_update_ticket_comment,
    delete_ticket_comment as kanban_delete_ticket_comment,
    # Activity log
    add_activity_log as kanban_add_activity_log,
    get_activity_log as kanban_get_activity_log,
    # Real-time event log
    append_event as kanban_append_event,
    latest_event_seq as kanban_latest_event_seq,
    events_since as kanban_events_since,
    prune_events as kanban_prune_events,
    # Board-level history (rollback)
    serialize_board_state as kanban_serialize_board_state,
    record_board_history as kanban_record_board_history,
    list_board_history as kanban_list_board_history,
    get_board_history_entry as kanban_get_board_history_entry,
    delete_board_history_entry as kanban_delete_board_history_entry,
    clear_board_history as kanban_clear_board_history,
    restore_board_from_snapshot as kanban_restore_board_from_snapshot,
    get_user_board_order as kanban_get_user_board_order,
    save_user_board_order as kanban_save_user_board_order,
)

# Beta testers

# Temporary accounts & pages
from ._temporary import (  # noqa: F401
    set_page_expiry,
    get_page_expiry,
    list_temp_pages,
    get_expired_pages,
    cleanup_expired_temp_pages,
    set_user_expiry,
    get_user_expiry,
    list_temp_users,
    get_expired_users,
    cleanup_expired_temp_users,
    set_role_expiry,
    get_role_expiry,
    list_temp_roles,
    get_expired_roles,
    cleanup_expired_temp_roles,
    cleanup_all_expired_temporary,
    set_page_temp_index_state,
    get_page_temp_index_state,
    list_temp_page_index_states,
    get_expired_page_index_states,
    cleanup_expired_temp_page_index_states,
)

# Custom pages
from ._custom_pages import (  # noqa: F401
    CONTENT_TYPES as CUSTOM_PAGE_CONTENT_TYPES,
    CONTENT_TYPE_GROUPS as CUSTOM_PAGE_CONTENT_TYPE_GROUPS,
    extract_youtube_video_id,
    create_custom_page,
    update_custom_page,
    delete_custom_page,
    get_custom_page,
    get_custom_page_by_path,
    list_custom_pages,
    count_custom_pages,
    add_custom_page_file,
    get_custom_page_file,
    list_custom_page_files,
    delete_custom_page_file,
)


# Deletion Slowdown plugin
from ._deletion_slowdown import (  # noqa: F401
    PENDING_DELETION_HOURS,
    mark_page_pending_deletion,
    restore_page_from_pending_deletion,
    get_pending_deletion_info,
    list_pending_deletions,
    get_expired_pending_deletions,
    cleanup_expired_pending_deletions,
)

# Built-in wiki documentation
from ._wiki_docs import (  # noqa: F401
    DOCS_CATEGORY_NAME,
    spawn_wiki_docs,
    is_docs_category,
    build_docs_archive,
)

# Canvas layouts
from ._canvas import (  # noqa: F401
    create_layout as canvas_create_layout,
    get_layout as canvas_get_layout,
    get_layout_by_slug as canvas_get_layout_by_slug,
    list_layouts as canvas_list_layouts,
    list_layouts_for_user as canvas_list_layouts_for_user,
    list_public_layouts as canvas_list_public_layouts,
    update_layout as canvas_update_layout,
    delete_layout as canvas_delete_layout,
    save_layout_data as canvas_save_layout_data,
    set_permission as canvas_set_permission,
    remove_permission as canvas_remove_permission,
    get_permissions as canvas_get_permissions,
    clear_permissions as canvas_clear_permissions,
    get_user_permission as canvas_get_user_permission,
    user_can_view as canvas_user_can_view,
    user_can_edit as canvas_user_can_edit,
    user_has_any_permission as canvas_user_has_any_permission,
    revoke_role_shares_for_restricted_roles as canvas_revoke_role_shares_for_restricted_roles,
    update_wiki_nodes_for_page as canvas_update_wiki_nodes_for_page,
    mark_deleted_wiki_nodes as canvas_mark_deleted_wiki_nodes,
    apply_ops as canvas_apply_ops,
    append_snapshot_event as canvas_append_snapshot_event,
    latest_event_seq as canvas_latest_event_seq,
    events_since as canvas_events_since,
    prune_events as canvas_prune_events,
    # Layout history (rollback)
    record_layout_history as canvas_record_layout_history,
    list_layout_history as canvas_list_layout_history,
    get_layout_history_entry as canvas_get_layout_history_entry,
    delete_layout_history_entry as canvas_delete_layout_history_entry,
    clear_layout_history as canvas_clear_layout_history,
    restore_layout_from_snapshot as canvas_restore_layout_from_snapshot,
    get_user_layout_order as canvas_get_user_layout_order,
    save_user_layout_order as canvas_save_user_layout_order,
)

# Pending contributions (draft approval system)
from ._contributions import (  # noqa: F401
    MAX_QUOTA_REQUEST_REASON_LENGTH as CONTRIBUTION_MAX_QUOTA_REQUEST_REASON_LENGTH,
    MAX_QUOTA_REVIEW_REASON_LENGTH as CONTRIBUTION_MAX_QUOTA_REVIEW_REASON_LENGTH,
    MAX_CONTRIBUTION_REASON_LENGTH,
    MAX_REVIEW_REASON_LENGTH as CONTRIBUTION_MAX_REVIEW_REASON_LENGTH,
    create_contribution,
    update_contribution,
    withdraw_contribution,
    auto_withdraw_contributions_for_promoted_user,
    approve_contribution,
    deny_contribution,
    get_contribution,
    get_pending_contribution_for_page,
    get_user_contribution_for_page,
    list_pending_contributions,
    list_user_contributions,
    list_contributions_for_page,
    count_pending_contributions_for_page,
    count_pending_contributions,
    cleanup_expired_contributions,
    cleanup_reviewed_contributions,
    # Quota management
    get_default_contribution_quota,
    get_effective_contribution_quota,
    get_user_pending_contribution_count,
    can_user_submit_contribution,
    get_pending_contribution_quota_request,
    list_contribution_quota_requests,
    get_contribution_quota_request,
    create_contribution_quota_request,
    review_contribution_quota_request,
    cancel_contribution_quota_request,
    set_user_contribution_quota,
    cleanup_resolved_contribution_quota_requests,
    count_pending_contribution_quota_requests,
)

# Page assessments
from ._assessments import (  # noqa: F401
    upsert_assessment,
    delete_assessment,
    get_assessment_for_page,
    set_assessment_questions,
    get_assessment_questions,
    count_user_attempts,
    can_user_attempt,
    submit_assessment_attempt,
    get_assessment_attempts,
    get_user_attempts_for_assessment,
    get_attempt_answers,
    reset_user_attempts,
    get_user_assessment_points,
)

# Per-wiki request analytics (daily roll-up)
from ._analytics import (  # noqa: F401
    record_request,
    get_analytics_summary,
    reset_analytics,
    ALLOWED_KINDS as ANALYTICS_KINDS,
)

# Feedback plugin

# Contributor leaderboard
from ._leaderboard import (  # noqa: F401
    LEADERBOARD_SORTS,
    LEADERBOARD_DATE_RANGES,
    get_leaderboard_stats,
    get_leaderboard_by_edits,
    get_leaderboard_by_chars,
    export_leaderboard_csv,
)

# Text-to-speech (built-in `tts` plugin)
from ._tts import (  # noqa: F401
    get_tts_generation,
    get_tts_generation_by_id,
    request_tts_generation,
    request_limited_tts_generation,
    mark_tts_processing,
    mark_tts_completed,
    mark_tts_failed,
    mark_tts_retry_pending,
    mark_tts_rate_limited_pending,
    delete_tts_generation,
    list_tts_filenames,
    list_tts_generations,
    list_tts_generations_with_pages,
    count_tts_generations_by_status,
    count_active_tts_generations,
    count_active_tts_generations_for_requester,
    count_tts_backfill_candidates,
    list_tts_backfill_candidates,
    clear_all_tts_generations,
    list_stuck_tts_generations,
    list_pending_tts_generations,
    list_resumable_failed_tts_generations,
    reset_to_pending,
)

# User profile fields (merged into `user_profiles` plugin)
from ._user_profile_fields import (  # noqa: F401
    get_field_definitions,
    get_user_field_values,
    get_visible_user_field_values,
    save_user_field_value,
    delete_user_field_values,
)

# API Service plugin (built-in `api_service` plugin)
from ._api_service import (  # noqa: F401
    create_api_token,
    verify_api_service_token,
    update_token_last_used,
    list_user_tokens,
    count_user_tokens,
    get_token_by_id,
    revoke_api_service_token,
    revoke_user_api_service_tokens,
    revoke_all_user_tokens,
    delete_tokens_for_user,
    list_all_tokens,
    log_api_call,
    get_audit_log,
    count_audit_log_entries,
    clear_audit_log,
    get_api_service_settings,
    update_api_service_settings,
    API_RATE_LIMIT_BOUNDS,
    API_MAX_TOKENS_BOUNDS,
    user_has_api_access,
    token_has_permission,
    create_userbot_token,
    get_userbot_token_info,
    revoke_userbot_tokens,
)

# Account merging
from ._account_merges import (  # noqa: F401
    create_merge_request,
    get_merge_request,
    get_active_requests_for_user,
    update_merge_request_status,
    approve_by_source,
    approve_by_target,
    approve_by_admin,
    deny_merge,
    cancel_merge,
    execute_merge,
    admin_execute_merge,
)
