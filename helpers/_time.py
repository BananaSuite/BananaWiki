"""Timezone-aware time formatting helpers."""

from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception

import db


def get_site_timezone():
    """Return the zoneinfo timezone configured in site settings (defaults to UTC).

    Falls back to ``datetime.timezone.utc`` when ``zoneinfo`` or ``tzdata`` is
    unavailable (e.g. frozen PyInstaller builds on Windows without tzdata).
    """
    settings = db.get_site_settings()
    tz_name = (settings["timezone"] if settings and settings["timezone"] else "UTC")
    if ZoneInfo is not None:
        try:
            return ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, KeyError):
            try:
                return ZoneInfo("UTC")
            except (ZoneInfoNotFoundError, KeyError):
                pass
    return timezone.utc


def normalize_birth_date(value):
    """Return a clean ISO birth date string, or ``""`` for blank input.

    Raises ``ValueError`` when the input is malformed or in the future.
    """
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Birth date must use YYYY-MM-DD format.") from exc
    today = datetime.now(get_site_timezone()).date()
    if parsed > today:
        raise ValueError("Birth date cannot be in the future.")
    return parsed.isoformat()


def is_birthday_today(birth_date, today=None):
    """Return True when *birth_date* has the same month/day as today's site date."""
    if not birth_date:
        return False
    try:
        parsed = date.fromisoformat(str(birth_date))
    except (TypeError, ValueError):
        return False
    if today is None:
        today = datetime.now(get_site_timezone()).date()
    return parsed.month == today.month and parsed.day == today.day


def time_ago(dt_str):
    """Return a human-readable 'X ago' or 'in X' string."""
    if not dt_str:
        return "never"
    try:
        dt = datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return "unknown"
    diff = datetime.now(timezone.utc) - dt
    secs = int(diff.total_seconds())
    if secs < 0:
        # Future date
        secs = abs(secs)
        if secs < 60:
            return "in a moment"
        elif secs < 3600:
            m = secs // 60
            return f"in {m} minute{'s' if m != 1 else ''}"
        elif secs < 86400:
            h = secs // 3600
            return f"in {h} hour{'s' if h != 1 else ''}"
        else:
            d = secs // 86400
            return f"in {d} day{'s' if d != 1 else ''}"
    if secs < 60:
        return "just now"
    elif secs < 3600:
        m = secs // 60
        return f"{m} minute{'s' if m != 1 else ''} ago"
    elif secs < 86400:
        h = secs // 3600
        return f"{h} hour{'s' if h != 1 else ''} ago"
    else:
        d = secs // 86400
        return f"{d} day{'s' if d != 1 else ''} ago"


def format_datetime(dt_str):
    """Return a human-readable date/time string in the configured site timezone."""
    if not dt_str:
        return ""
    try:
        dt = datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return ""
    site_tz = get_site_timezone()
    dt_local = dt.astimezone(site_tz)
    tz_label = dt_local.strftime("%Z")
    return dt_local.strftime(f"%Y-%m-%d %H:%M {tz_label}")


def format_datetime_local_input(dt_str):
    """Convert a UTC ISO datetime to site-timezone value for datetime-local inputs."""
    if not dt_str:
        return ""
    try:
        dt = datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return ""
    site_tz = get_site_timezone()
    dt_local = dt.astimezone(site_tz)
    return dt_local.strftime("%Y-%m-%dT%H:%M")


def local_datetime_to_utc(dt_str):
    """Convert a naive datetime string (from a datetime-local input) to a UTC ISO string.

    The input is assumed to be expressed in the site timezone configured in
    site settings.  Raises ``ValueError`` when *dt_str* cannot be parsed.
    Returns the converted string in UTC ISO format (with +00:00 offset).
    """
    naive = datetime.fromisoformat(dt_str)
    site_tz = get_site_timezone()
    local_dt = naive.replace(tzinfo=site_tz)
    utc_dt = local_dt.astimezone(timezone.utc)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%S")


def get_effective_chat_cleanup_settings(settings):
    """Return DM and group cleanup settings with legacy fallbacks applied.

    Older databases stored a single set of chat cleanup settings before the
    direct-message and group-chat settings were split. When the newer columns
    still contain their migration defaults, continue honoring the legacy values
    until the split settings have been explicitly saved from the admin UI so
    historical installations keep their original cleanup behavior.
    """
    if not settings:
        return {
            "dm": {
                "auto_clear_messages": 0,
                "auto_clear_attachments": 0,
                "message_retention_days": 0,
                "attachment_retention_days": 7,
            },
            "group": {
                "auto_clear_messages": 0,
                "auto_clear_attachments": 0,
                "message_retention_days": 0,
                "attachment_retention_days": 7,
            },
        }

    legacy = {
        "auto_clear_messages": settings["chat_auto_clear_messages"],
        "auto_clear_attachments": settings["chat_auto_clear_attachments"],
        "message_retention_days": settings["chat_message_retention_days"],
        "attachment_retention_days": settings["chat_attachment_retention_days"],
    }
    try:
        split_configured = bool(settings["chat_cleanup_split_configured"])
    except (KeyError, TypeError):
        split_configured = False

    def _resolve_scope(prefix):
        """Return one scope's cleanup settings, preferring legacy values when needed.

        ``defaults`` mirrors the *historical* migration defaults. Not the
        current schema defaults, because they are used to detect rows that
        were never explicitly saved from the admin UI.  Fresh installs now
        ship with ``auto_clear_attachments=0`` (see ``db/_schema.py``), but
        older DBs that were already migrated still hold the previous
        ``DEFAULT 1``; both code paths must keep working.
        """
        defaults = {
            "auto_clear_messages": 0,
            "auto_clear_attachments": 1,
            "message_retention_days": 0,
            "attachment_retention_days": 7,
        }
        resolved = {}
        for field, default in defaults.items():
            specific_key = f"chat_{prefix}_{field}"
            specific_value = settings[specific_key]
            legacy_value = legacy[field]
            # Before the split DM/group settings have been saved, treat any
            # non-default split value as an explicit modern override but keep
            # honoring the legacy columns when the split fields still look like
            # untouched migration defaults.
            if split_configured or (specific_value is not None and specific_value != default):
                resolved[field] = specific_value if specific_value is not None else default
            elif legacy_value is not None:
                resolved[field] = legacy_value
            else:
                resolved[field] = default
        return resolved

    return {
        "dm": _resolve_scope("dm"),
        "group": _resolve_scope("group"),
    }


def get_next_chat_cleanup_time():
    """Calculate and return the next scheduled chat cleanup time as an ISO datetime string."""
    try:
        site_tz = get_site_timezone()
        now = datetime.now(site_tz)
        settings = db.get_site_settings()

        # Get cleanup schedule from DB settings with sensible defaults
        cleanup_frequency = (settings["chat_cleanup_frequency_days"] if settings and settings["chat_cleanup_frequency_days"] else 7)
        cleanup_hour = (settings["chat_cleanup_hour"] if settings and settings["chat_cleanup_hour"] is not None else 3)

        # Get last cleanup time
        last_cleanup = None
        if settings:
            try:
                last_cleanup_str = settings["last_chat_cleanup_at"]
                if last_cleanup_str:
                    # Parse ISO format datetime
                    last_cleanup = datetime.fromisoformat(last_cleanup_str.replace('Z', '+00:00'))
                    # Convert to site timezone
                    last_cleanup = last_cleanup.astimezone(site_tz)
            except (KeyError, ValueError, AttributeError, TypeError):
                pass

        # If no last cleanup recorded, next cleanup is at configured hour today/tomorrow
        if not last_cleanup:
            target = now.replace(hour=cleanup_hour, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
        else:
            # Schedule for configured frequency after last cleanup
            target = last_cleanup.replace(hour=cleanup_hour, minute=0, second=0, microsecond=0)
            target += timedelta(days=cleanup_frequency)

            # If target is in the past, schedule for the next interval
            while target <= now:
                target += timedelta(days=cleanup_frequency)

        # Convert to UTC for storage
        return target.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def get_time_since_last_chat_cleanup():
    """Return a human-readable string for time elapsed since last chat cleanup."""
    try:
        settings = db.get_site_settings()
        if not settings:
            return "Messages have not been cleaned up yet"

        try:
            last_cleanup_str = settings["last_chat_cleanup_at"]
            if not last_cleanup_str:
                return "Messages have not been cleaned up yet"
            return time_ago(last_cleanup_str)
        except (KeyError, TypeError):
            return "Messages have not been cleaned up yet"
    except Exception:
        return "Unknown"


def get_time_until_next_chat_cleanup():
    """Return a human-readable string for time remaining until next chat cleanup."""
    try:
        next_cleanup_str = get_next_chat_cleanup_time()
        if not next_cleanup_str:
            return "Unknown"

        next_cleanup = datetime.fromisoformat(next_cleanup_str.replace("Z", "+00:00"))
        if next_cleanup.tzinfo is None:
            next_cleanup = next_cleanup.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        diff = next_cleanup - now
        secs = int(diff.total_seconds())

        if secs <= 0:
            return "Soon"

        if secs < 60:
            return "in a moment"
        elif secs < 3600:
            m = secs // 60
            return f"in {m} minute{'s' if m != 1 else ''}"
        elif secs < 86400:
            h = secs // 3600
            return f"in {h} hour{'s' if h != 1 else ''}"
        else:
            d = secs // 86400
            return f"in {d} day{'s' if d != 1 else ''}"
    except Exception:
        return "Unknown"


def is_future(dt_str):
    """Return True if the given ISO datetime string is in the future."""
    if not dt_str:
        return False
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) < dt
    except (ValueError, TypeError):
        return False
