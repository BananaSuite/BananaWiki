"""User Profiles: first-party BananaWiki plugin.

Includes profile fields (merged from the former user_profile_fields plugin).
"""

from bananawiki_sdk import Plugin
from bananawiki_sdk._database import db_execute, db_query

plugin = Plugin("user_profiles")

FIELD_DEFINITIONS = [
    ("location",       "Location",                          "text"),
    ("height_cm",      "Height (cm)",                       "number"),
    ("website",        "Website",                           "url"),
    ("occupation",     "Occupation",                        "text"),
    ("education",      "Education",                         "text"),
    ("gender",         "Gender",                            "text"),
    ("pronouns",       "Pronouns",                          "text"),
    ("languages",      "Languages",                         "text"),
    ("interests",      "Interests",                         "text"),
    ("favorite_movie", "Favorite Movie",                    "text"),
    ("favorite_book",  "Favorite Book",                     "text"),
    ("favorite_music", "Favorite Music / Bands",            "text"),
    ("favorite_game",  "Favorite Game",                     "text"),
    ("custom_status",  "Custom Status",                     "text"),
    ("social_media",   "Social Media Handles",              "text"),
    ("dream_dest",     "Dream Destination",                 "text"),
    ("favorite_animal","Favorite Animal",                   "text"),
    ("favorite_color", "Favorite Color",                    "text"),
    ("favorite_cuisine","Favorite Cuisine",                  "text"),
    ("motto",          "Motto / Life Philosophy",           "text"),
    ("currently_read", "Currently Reading",                 "text"),
    ("currently_watch","Currently Watching",                "text"),
    ("talent",         "Hidden Talent",                     "text"),
    ("about_me",       "About Me",                          "longtext"),
]


def _create_and_seed_tables():
    db_execute(
        "CREATE TABLE IF NOT EXISTS user_profile_fields__definitions ("
        "  id         INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  key        TEXT    NOT NULL UNIQUE,"
        "  label      TEXT    NOT NULL,"
        "  field_type TEXT    NOT NULL DEFAULT 'text',"
        "  sort_order INTEGER NOT NULL DEFAULT 0,"
        "  created_at TEXT    NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    db_execute(
        "CREATE TABLE IF NOT EXISTS user_profile_fields__values ("
        "  id         INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  user_id    TEXT    NOT NULL,"
        "  field_id   INTEGER NOT NULL,"
        "  value      TEXT    NOT NULL DEFAULT '',"
        "  visible    INTEGER NOT NULL DEFAULT 0,"
        "  updated_at TEXT    NOT NULL DEFAULT (datetime('now')),"
        "  FOREIGN KEY (field_id) REFERENCES user_profile_fields__definitions(id) ON DELETE CASCADE,"
        "  UNIQUE(user_id, field_id)"
        ")"
    )
    for idx, (key, label, ftype) in enumerate(FIELD_DEFINITIONS):
        existing = db_query(
            "SELECT id FROM user_profile_fields__definitions WHERE key=?",
            (key,),
        )
        if not existing:
            db_execute(
                "INSERT INTO user_profile_fields__definitions "
                "(key, label, field_type, sort_order) VALUES (?, ?, ?, ?)",
                (key, label, ftype, idx),
            )


@plugin.on_load
def setup(app):
    _create_and_seed_tables()
    # Remove legacy fields that duplicate core profile data or are no longer included
    removed_keys = ("birthday", "age", "hobbies", "nationality", "timezone",
                    "favorite_quote", "favorite_food", "skills", "pet_peeves",
                    "favorite_sport", "currently_play", "dislikes")
    for key in removed_keys:
        try:
            db_execute("DELETE FROM user_profile_fields__values WHERE field_id IN (SELECT id FROM user_profile_fields__definitions WHERE key=?)", (key,))
            db_execute("DELETE FROM user_profile_fields__definitions WHERE key=?", (key,))
        except Exception:
            pass
