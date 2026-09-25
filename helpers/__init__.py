"""
BananaWiki: Utility functions, constants, and shared helpers.

Extracted from app.py to keep route handlers separate from reusable logic.

This package re-exports every public and private name so that existing
``from helpers import X`` statements continue to work unchanged.
"""

from helpers._constants import (          # noqa: F401
    ALLOWED_TAGS,
    ALLOWED_ATTRS,
    _get_dummy_hash as _DUMMY_HASH,
    ROLE_LABELS,
    _USERNAME_RE,
    MAX_SUSPEND_REASON_LENGTH,
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
)

from helpers._rate_limiting import (      # noqa: F401
    _RateLimitStore,
    _LOGIN_ATTEMPTS,
    _LOGIN_MAX_ATTEMPTS,
    _LOGIN_WINDOW,
    _check_login_rate_limit,
    _record_login_attempt,
    _clear_login_attempts,
    _RL_LOCK,
    _RLStore,
    _RL_STORE,
    _RL_GLOBAL_MAX,
    _RL_GLOBAL_WINDOW,
    _rl_check,
    rate_limit,
    _client_key,
    _current_client_key,
)

from helpers._markdown import (           # noqa: F401
    render_markdown,
    highlight_code_html,
    _make_video_iframe,
    _embed_videos_in_html,
)

from helpers._diff import (              # noqa: F401
    compute_char_diff,
    compute_diff_html,
    compute_formatted_diff_html,
)

from helpers._text import slugify        # noqa: F401

from helpers._validation import (        # noqa: F401
    _ALWAYS_BLOCKED_EXTENSIONS,
    safe_unlink_in,
    _safe_ext,
    _parse_ext_list,
    _is_extension_allowed_by_settings,
    allowed_file,
    allowed_attachment,
    allowed_chat_file,
    get_effective_max_upload_size,
    _is_valid_hex_color,
    _is_valid_username,
    _safe_referrer,
    get_safe_next_url,
)

from helpers._auth import (              # noqa: F401
    get_current_user,
    login_required,
    editor_required,
    admin_required,
    editor_has_category_access,
    user_can_view_page,
    user_can_view_category,
    filter_visible_navigation,
    is_public_mode_active,
    is_open_signup_active,
    is_approval_required_active,
    is_page_builder_active,
    user_can_use_page_builder,
)

from helpers._time import (              # noqa: F401
    get_site_timezone,
    normalize_birth_date,
    is_birthday_today,
    time_ago,
    format_datetime,
    format_datetime_local_input,
    local_datetime_to_utc,
    get_effective_chat_cleanup_settings,
    get_time_since_last_chat_cleanup,
    get_time_until_next_chat_cleanup,
    is_future,
)

from helpers._crypto import (            # noqa: F401
    ENCRYPTED_SETTINGS_COLUMNS,
    encrypt_value,
    decrypt_value,
    _reset_cache,
)

from helpers._passwords import (         # noqa: F401
    check_password_hash,
    generate_password_hash,
    get_password_hash_method,
    is_scrypt_supported,
)

from helpers._favicon import (           # noqa: F401
    BANANA_FAVICON_COLORS,
    generate_banana_favicon,
)


from helpers._request_cache import (     # noqa: F401
    get_request_category_tree,
    get_request_list_categories,
    get_request_user_accessibility,
    get_request_sidebar_people,
    get_request_unread_dm_count,
    get_request_unread_group_count,
    get_request_reservations_map,
)

from helpers._pdf import generate_page_pdf  # noqa: F401

from helpers._bot_protection import (      # noqa: F401
    generate_form_token,
    check_bot_protection,
    is_bot_protection_enabled,
    HONEYPOT_FIELD,
    FORM_TIME_FIELD,
)

from helpers._translations import (         # noqa: F401
    t,
    reload_translations,
    get_js_translations,
    list_translation_files,
    read_translation_file,
    write_translation_file,
    delete_translation_file,
    validate_language_payload,
    get_language_meta,
    MAX_TRANSLATION_FILE_BYTES,
)

from helpers._server_restart import (        # noqa: F401
    is_hosted_instance,
    is_easy_wiki,
    get_restart_cooldown_remaining,
    trigger_server_restart,
)

from helpers._interface_languages import (  # noqa: F401
    BUILTIN_INTERFACE_LANGUAGES,
    parse_custom_interface_languages,
    dump_custom_interface_languages,
    get_custom_interface_languages,
    get_enabled_interface_languages,
    get_all_interface_languages,
    normalize_language_selection,
    normalize_docs_language,
    upsert_custom_interface_language,
    set_custom_interface_language_enabled,
    delete_custom_interface_language,
    get_interface_language_label,
    match_best_interface_language,
)

from helpers._bulk_markdown import (        # noqa: F401
    BulkImportResult,
    import_markdown_bundle,
    extract_markdown_files_from_zip,
)

from helpers._page_builder import (         # noqa: F401
    BuilderValidationError,
    validate_builder_payload,
    dump_builder_payload,
    load_builder_payload,
    compile_builder_markdown,
)

from helpers._joke_audio import (          # noqa: F401
    is_joke_audio_extension,
    get_real_audio_ext,
    get_joke_success_message,
    get_joke_fail_message,
    convert_joke_audio,
)
