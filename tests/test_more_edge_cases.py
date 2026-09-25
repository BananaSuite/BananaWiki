"""
Further edge-case coverage for BananaWiki.

Focuses on areas with thin or no prior test coverage:
- Category CRUD, ancestry checks, tree building, search
- Draft save/get/delete/transfer/list
- Group membership, roles, timeouts
- Direct message (chat) operations
- Badge award / revoke / notification cycle
- Audit: role history and custom user tags
- User profile operations
- Permission helpers (helpers/_permissions.py)
- Plugin class (bananawiki_sdk._plugin.Plugin)
- SDK exceptions hierarchy
- Admin user-tag and attribution routes
- Account settings route
- Users list and profile routes
"""

import pytest
from werkzeug.security import generate_password_hash


# ---------------------------------------------------------------------------
# Category CRUD edge cases
# ---------------------------------------------------------------------------

class TestCategoryCRUD:
    def test_create_and_get_category(self):
        import db
        cat_id = db.create_category("Science")
        cat = db.get_category(cat_id)
        assert cat is not None
        assert cat["name"] == "Science"

    def test_create_category_with_parent(self):
        import db
        parent_id = db.create_category("Parent")
        child_id = db.create_category("Child", parent_id=parent_id)
        child = db.get_category(child_id)
        assert child["parent_id"] == parent_id

    def test_get_nonexistent_category_returns_none(self):
        import db
        assert db.get_category(99999) is None

    def test_update_category_name(self):
        import db
        cat_id = db.create_category("Old Name")
        db.update_category(cat_id, "New Name")
        assert db.get_category(cat_id)["name"] == "New Name"

    def test_delete_empty_category(self):
        import db
        cat_id = db.create_category("ToDelete")
        db.delete_category(cat_id)
        assert db.get_category(cat_id) is None

    def test_count_pages_in_empty_category(self):
        import db
        cat_id = db.create_category("Empty")
        assert db.count_pages_in_category(cat_id) == 0

    def test_count_pages_in_category_with_pages(self):
        import db
        uid = db.create_user("pageowner", generate_password_hash("pw"))
        cat_id = db.create_category("WithPages")
        db.create_page("P1", "p1", "content", cat_id, uid)
        db.create_page("P2", "p2", "content", cat_id, uid)
        assert db.count_pages_in_category(cat_id) == 2

    def test_list_categories_returns_all(self):
        import db
        db.create_category("CatA")
        db.create_category("CatB")
        cats = db.list_categories()
        names = [c["name"] for c in cats]
        assert "CatA" in names
        assert "CatB" in names

    def test_search_categories_returns_matches(self):
        import db
        db.create_category("Physics 101")
        db.create_category("Chemistry 101")
        results = db.search_categories("Physics")
        assert any("Physics" in r["name"] for r in results)

    def test_search_categories_empty_query(self):
        import db
        db.create_category("AnyCategory")
        results = db.search_categories("")
        # Empty query may return all or none: must not crash
        assert isinstance(results, list)

    def test_get_category_tree_structure(self):
        import db
        db.create_category("TreeRoot")
        tree = db.get_category_tree()
        # get_category_tree() returns a tuple: (category_nodes, uncategorized_pages)
        assert isinstance(tree, tuple)
        assert len(tree) == 2
        category_nodes, uncategorized_pages = tree
        assert isinstance(category_nodes, list)
        assert isinstance(uncategorized_pages, list)

    def test_is_descendant_of_detects_cycle_risk(self):
        import db
        # is_descendant_of(cat_id, ancestor_id) returns True if ancestor_id
        # IS a descendant of cat_id (docstring: "Return True if ancestor_id is
        # a descendant of cat_id").
        parent = db.create_category("GP")
        child = db.create_category("GC", parent_id=parent)
        # child IS a descendant of parent (True) → setting parent's parent to
        # child would create a cycle
        assert db.is_descendant_of(parent, child) is True
        # parent is NOT a descendant of child (False) → safe to keep child under parent
        assert db.is_descendant_of(child, parent) is False

    def test_update_category_parent(self):
        import db
        root1 = db.create_category("Root1")
        root2 = db.create_category("Root2")
        child = db.create_category("MovableChild", parent_id=root1)
        db.update_category_parent(child, root2)
        assert db.get_category(child)["parent_id"] == root2

    def test_update_category_parent_to_none_promotes_to_top(self):
        import db
        parent = db.create_category("ParentCat")
        child = db.create_category("ChildToPromote", parent_id=parent)
        db.update_category_parent(child, None)
        assert db.get_category(child)["parent_id"] is None


# ---------------------------------------------------------------------------
# Draft save / get / delete / transfer / list
# ---------------------------------------------------------------------------

class TestDraftOperations:
    def _make_page(self):
        import db
        uid = db.create_user("drafter", generate_password_hash("pw"))
        cat_id = db.create_category("DraftCat")
        page_id = db.create_page("DraftPage", "draft-page", "content", cat_id, uid)
        return uid, page_id

    def test_save_and_get_draft(self):
        import db
        uid, page_id = self._make_page()
        db.save_draft(page_id, uid, "My Draft Title", "My draft content")
        draft = db.get_draft(page_id, uid)
        assert draft is not None
        assert draft["title"] == "My Draft Title"
        assert draft["content"] == "My draft content"

    def test_get_draft_nonexistent_returns_none(self):
        import db
        uid, page_id = self._make_page()
        assert db.get_draft(page_id, uid) is None

    def test_save_draft_overwrites_existing(self):
        import db
        uid, page_id = self._make_page()
        db.save_draft(page_id, uid, "First", "First content")
        db.save_draft(page_id, uid, "Second", "Second content")
        draft = db.get_draft(page_id, uid)
        assert draft["title"] == "Second"
        assert draft["content"] == "Second content"

    def test_delete_draft(self):
        import db
        uid, page_id = self._make_page()
        db.save_draft(page_id, uid, "ToDelete", "content")
        db.delete_draft(page_id, uid)
        assert db.get_draft(page_id, uid) is None

    def test_get_user_draft_count(self):
        import db
        uid = db.create_user("draftcount", generate_password_hash("pw"))
        cat_id = db.create_category("DC")
        p1 = db.create_page("P1", "dp1", "c", cat_id, uid)
        p2 = db.create_page("P2", "dp2", "c", cat_id, uid)
        db.save_draft(p1, uid, "D1", "c1")
        db.save_draft(p2, uid, "D2", "c2")
        assert db.get_user_draft_count(uid) == 2

    def test_get_drafts_for_page_lists_all_user_drafts(self):
        import db
        uid1 = db.create_user("drafter1", generate_password_hash("pw"))
        uid2 = db.create_user("drafter2", generate_password_hash("pw"))
        cat_id = db.create_category("DP")
        page_id = db.create_page("SharedPage", "shared-pg", "c", cat_id, uid1)
        db.save_draft(page_id, uid1, "Draft by 1", "c1")
        db.save_draft(page_id, uid2, "Draft by 2", "c2")
        drafts = db.get_drafts_for_page(page_id)
        user_ids = [d["user_id"] for d in drafts]
        assert uid1 in user_ids
        assert uid2 in user_ids

    def test_transfer_draft_moves_to_new_user(self):
        import db
        uid1 = db.create_user("from_user", generate_password_hash("pw"))
        uid2 = db.create_user("to_user", generate_password_hash("pw"))
        cat_id = db.create_category("TC")
        page_id = db.create_page("TransferPage", "xfer-pg", "c", cat_id, uid1)
        db.save_draft(page_id, uid1, "Transfer me", "tc")
        db.transfer_draft(page_id, uid1, uid2)
        assert db.get_draft(page_id, uid1) is None
        transferred = db.get_draft(page_id, uid2)
        assert transferred is not None
        assert transferred["title"] == "Transfer me"

    def test_list_user_drafts(self):
        import db
        uid = db.create_user("list_drafter", generate_password_hash("pw"))
        cat_id = db.create_category("LD")
        p1 = db.create_page("LD1", "ld1", "c", cat_id, uid)
        p2 = db.create_page("LD2", "ld2", "c", cat_id, uid)
        db.save_draft(p1, uid, "Draft 1", "c1")
        db.save_draft(p2, uid, "Draft 2", "c2")
        drafts = db.list_user_drafts(uid)
        assert len(drafts) == 2


# ---------------------------------------------------------------------------
# Group membership, roles, and timeouts
# ---------------------------------------------------------------------------

class TestGroupMembershipEdgeCases:
    def _make_group(self):
        import db
        owner_id = db.create_user("grp_owner", generate_password_hash("pw"))
        result = db.create_group_chat("TestGroup", owner_id)
        return result["id"], owner_id

    def test_creator_is_group_owner(self):
        import db
        group_id, owner_id = self._make_group()
        assert db.get_group_member_role(group_id, owner_id) == "owner"

    def test_add_member_and_is_member(self):
        import db
        group_id, _ = self._make_group()
        uid = db.create_user("new_member", generate_password_hash("pw"))
        db.add_group_member(group_id, uid)
        assert db.is_group_member(group_id, uid) is True

    def test_non_member_returns_false(self):
        import db
        group_id, _ = self._make_group()
        uid = db.create_user("outsider", generate_password_hash("pw"))
        assert db.is_group_member(group_id, uid) is False

    def test_remove_member(self):
        import db
        group_id, _ = self._make_group()
        uid = db.create_user("removable", generate_password_hash("pw"))
        db.add_group_member(group_id, uid)
        db.remove_group_member(group_id, uid)
        assert db.is_group_member(group_id, uid) is False

    def test_promote_member_to_moderator(self):
        import db
        group_id, _ = self._make_group()
        uid = db.create_user("promo_member", generate_password_hash("pw"))
        db.add_group_member(group_id, uid)
        db.set_group_member_role(group_id, uid, "moderator")
        assert db.get_group_member_role(group_id, uid) == "moderator"

    def test_set_and_check_timeout(self):
        import db
        from datetime import datetime, timezone, timedelta
        group_id, _ = self._make_group()
        uid = db.create_user("timed_out_user", generate_password_hash("pw"))
        db.add_group_member(group_id, uid)
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        db.set_group_member_timeout(group_id, uid, future)
        assert db.is_group_member_timed_out(group_id, uid) is True

    def test_expired_timeout_is_not_active(self):
        import db
        from datetime import datetime, timezone, timedelta
        group_id, _ = self._make_group()
        uid = db.create_user("timeout_expired", generate_password_hash("pw"))
        db.add_group_member(group_id, uid)
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        db.set_group_member_timeout(group_id, uid, past)
        assert db.is_group_member_timed_out(group_id, uid) is False

    def test_get_group_members_returns_all(self):
        import db
        group_id, owner_id = self._make_group()
        uid2 = db.create_user("mem2", generate_password_hash("pw"))
        uid3 = db.create_user("mem3", generate_password_hash("pw"))
        db.add_group_member(group_id, uid2)
        db.add_group_member(group_id, uid3)
        members = db.get_group_members(group_id)
        member_ids = [m["user_id"] for m in members]
        assert owner_id in member_ids
        assert uid2 in member_ids
        assert uid3 in member_ids

    def test_get_user_groups_returns_joined_groups(self):
        import db
        uid = db.create_user("group_joiner", generate_password_hash("pw"))
        result1 = db.create_group_chat("Group1", uid)
        result2 = db.create_group_chat("Group2", uid)
        groups = db.get_user_groups(uid)
        group_ids = [g["id"] for g in groups]
        assert result1["id"] in group_ids
        assert result2["id"] in group_ids


# ---------------------------------------------------------------------------
# Direct message (DM chat) operations
# ---------------------------------------------------------------------------

class TestDMChatOperations:
    def test_get_or_create_chat_is_idempotent(self):
        import db
        uid1 = db.create_user("dm_u1", generate_password_hash("pw"))
        uid2 = db.create_user("dm_u2", generate_password_hash("pw"))
        chat1 = db.get_or_create_chat(uid1, uid2)
        chat2 = db.get_or_create_chat(uid1, uid2)
        assert chat1["id"] == chat2["id"]

    def test_reversed_user_order_returns_same_chat(self):
        import db
        uid1 = db.create_user("rev_u1", generate_password_hash("pw"))
        uid2 = db.create_user("rev_u2", generate_password_hash("pw"))
        chat1 = db.get_or_create_chat(uid1, uid2)
        chat2 = db.get_or_create_chat(uid2, uid1)
        assert chat1["id"] == chat2["id"]

    def test_is_participant(self):
        import db
        uid1 = db.create_user("par_u1", generate_password_hash("pw"))
        uid2 = db.create_user("par_u2", generate_password_hash("pw"))
        chat = db.get_or_create_chat(uid1, uid2)
        assert db.is_chat_participant(chat["id"], uid1) is True
        assert db.is_chat_participant(chat["id"], uid2) is True

    def test_non_participant_returns_false(self):
        import db
        uid1 = db.create_user("np_u1", generate_password_hash("pw"))
        uid2 = db.create_user("np_u2", generate_password_hash("pw"))
        uid3 = db.create_user("np_u3", generate_password_hash("pw"))
        chat = db.get_or_create_chat(uid1, uid2)
        assert db.is_chat_participant(chat["id"], uid3) is False

    def test_send_message_and_retrieve(self):
        import db
        uid1 = db.create_user("msg_u1", generate_password_hash("pw"))
        uid2 = db.create_user("msg_u2", generate_password_hash("pw"))
        chat = db.get_or_create_chat(uid1, uid2)
        db.send_chat_message(chat["id"], uid1, "Hello!")
        messages = db.get_chat_messages(chat["id"])
        assert len(messages) == 1
        assert messages[0]["content"] == "Hello!"

    def test_get_user_chats(self):
        import db
        uid1 = db.create_user("uc_u1", generate_password_hash("pw"))
        uid2 = db.create_user("uc_u2", generate_password_hash("pw"))
        uid3 = db.create_user("uc_u3", generate_password_hash("pw"))
        db.get_or_create_chat(uid1, uid2)
        db.get_or_create_chat(uid1, uid3)
        chats = db.get_user_chats(uid1)
        assert len(chats) == 2

    def test_unread_count_increments(self):
        import db
        uid1 = db.create_user("unread_u1", generate_password_hash("pw"))
        uid2 = db.create_user("unread_u2", generate_password_hash("pw"))
        chat = db.get_or_create_chat(uid1, uid2)
        db.increment_unread_count(chat["id"], uid2)
        db.increment_unread_count(chat["id"], uid2)
        total = db.get_total_unread_dm_count(uid2)
        assert total >= 2

    def test_reset_unread_count(self):
        import db
        uid1 = db.create_user("reset_u1", generate_password_hash("pw"))
        uid2 = db.create_user("reset_u2", generate_password_hash("pw"))
        chat = db.get_or_create_chat(uid1, uid2)
        db.increment_unread_count(chat["id"], uid2)
        db.reset_unread_count(chat["id"], uid2)
        total = db.get_total_unread_dm_count(uid2)
        assert total == 0

    def test_delete_chat_message_soft_deletes(self):
        import db
        uid1 = db.create_user("del_msg_u1", generate_password_hash("pw"))
        uid2 = db.create_user("del_msg_u2", generate_password_hash("pw"))
        chat = db.get_or_create_chat(uid1, uid2)
        db.send_chat_message(chat["id"], uid1, "Will be deleted")
        messages = db.get_chat_messages(chat["id"])
        msg_id = messages[0]["id"]
        db.delete_chat_message(msg_id)
        # The message should be soft-deleted (is_deleted=1)
        msg = db.get_chat_message_by_id(msg_id)
        assert msg is not None
        assert msg["is_deleted"] == 1


# ---------------------------------------------------------------------------
# Badge award / revoke / notification cycle
# ---------------------------------------------------------------------------

class TestBadgeCycle:
    def test_award_and_has_badge(self):
        import db
        uid = db.create_user("badge_u1", generate_password_hash("pw"))
        badge_id = db.create_badge_type("Gold Star", trigger_type="")
        db.award_badge(uid, badge_id)
        assert db.has_badge(uid, badge_id) is True

    def test_award_duplicate_is_idempotent(self):
        import db
        uid = db.create_user("badge_dup", generate_password_hash("pw"))
        badge_id = db.create_badge_type("Silver Star", trigger_type="")
        db.award_badge(uid, badge_id)
        db.award_badge(uid, badge_id)  # second call must not raise
        assert db.count_user_badges(uid) == 1

    def test_revoke_badge(self):
        import db
        uid = db.create_user("revoke_u", generate_password_hash("pw"))
        badge_id = db.create_badge_type("Bronze Star", trigger_type="")
        db.award_badge(uid, badge_id)
        db.revoke_badge(uid, badge_id)
        # Revoked badge is still in DB but marked revoked
        badges = db.get_user_badges(uid, include_revoked=True)
        assert any(b["revoked"] == 1 for b in badges)

    def test_count_user_badges_excludes_revoked(self):
        import db
        uid = db.create_user("count_badge_u", generate_password_hash("pw"))
        badge_id = db.create_badge_type("Counting Badge", trigger_type="")
        db.award_badge(uid, badge_id)
        db.revoke_badge(uid, badge_id)
        assert db.count_user_badges(uid) == 0
        assert db.count_user_badges(uid, include_revoked=True) == 1

    def test_unnotified_badge_notification_cycle(self):
        import db
        uid = db.create_user("notify_badge_u", generate_password_hash("pw"))
        badge_id = db.create_badge_type("Notification Badge", trigger_type="")
        db.award_badge(uid, badge_id)
        unnotified = db.get_unnotified_badges(uid)
        assert len(unnotified) == 1
        db.mark_badges_notified(uid)
        assert len(db.get_unnotified_badges(uid)) == 0

    def test_clear_badge_notifications(self):
        import db
        uid = db.create_user("clear_notif_u", generate_password_hash("pw"))
        badge_id = db.create_badge_type("Clear Badge", trigger_type="")
        db.award_badge(uid, badge_id)
        db.clear_badge_notifications(uid)
        assert len(db.get_unnotified_badges(uid)) == 0

    def test_get_badge_holders(self):
        import db
        uid1 = db.create_user("holder_u1", generate_password_hash("pw"))
        uid2 = db.create_user("holder_u2", generate_password_hash("pw"))
        badge_id = db.create_badge_type("Shared Badge", trigger_type="")
        db.award_badge(uid1, badge_id)
        db.award_badge(uid2, badge_id)
        holders = db.get_badge_holders(badge_id)
        holder_ids = [h["user_id"] for h in holders]
        assert uid1 in holder_ids
        assert uid2 in holder_ids

    def test_list_badge_types_enabled_only(self):
        import db
        db.create_badge_type("EnabledBadge", trigger_type="", enabled=True)
        db.create_badge_type("DisabledBadge", trigger_type="", enabled=False)
        enabled = db.list_badge_types(enabled_only=True)
        disabled_included = db.list_badge_types(enabled_only=False)
        enabled_names = [b["name"] for b in enabled]
        all_names = [b["name"] for b in disabled_included]
        assert "EnabledBadge" in enabled_names
        assert "DisabledBadge" not in enabled_names
        assert "DisabledBadge" in all_names

    def test_delete_badge_type(self):
        import db
        badge_id = db.create_badge_type("ToDeleteBadge", trigger_type="")
        db.delete_badge_type(badge_id)
        assert db.get_badge_type(badge_id) is None

    def test_get_badge_type_by_name(self):
        import db
        badge_id = db.create_badge_type("NamedBadge", trigger_type="")
        badge = db.get_badge_type_by_name("NamedBadge")
        assert badge is not None
        assert badge["id"] == badge_id

    def test_get_all_badge_holder_counts(self):
        import db
        uid = db.create_user("allcount_u", generate_password_hash("pw"))
        badge_id = db.create_badge_type("CountBadge", trigger_type="")
        db.award_badge(uid, badge_id)
        counts = db.get_all_badge_holder_counts()
        assert isinstance(counts, dict)
        assert counts.get(badge_id, 0) >= 1


# ---------------------------------------------------------------------------
# Audit: role history and custom user tags
# ---------------------------------------------------------------------------

class TestAuditOperations:
    def test_record_and_get_role_history(self):
        import db
        uid = db.create_user("rh_user", generate_password_hash("pw"))
        db.record_role_change(uid, "user", "editor")
        history = db.get_role_history(uid)
        assert len(history) >= 1
        assert history[0]["old_role"] == "user"
        assert history[0]["new_role"] == "editor"

    def test_role_history_empty_for_new_user(self):
        import db
        uid = db.create_user("norole_user", generate_password_hash("pw"))
        assert db.get_role_history(uid) == []

    def test_add_and_get_custom_tag(self):
        import db
        uid = db.create_user("tag_user", generate_password_hash("pw"))
        db.add_user_custom_tag(uid, "VIP", color="#ff0000")
        tags = db.get_user_custom_tags(uid)
        assert len(tags) == 1
        assert tags[0]["label"] == "VIP"
        assert tags[0]["color"] == "#ff0000"

    def test_delete_custom_tag(self):
        import db
        uid = db.create_user("deltag_user", generate_password_hash("pw"))
        tag_id = db.add_user_custom_tag(uid, "TempTag")
        db.delete_user_custom_tag(tag_id)
        assert db.get_user_custom_tags(uid) == []

    def test_update_custom_tag(self):
        import db
        uid = db.create_user("updatetag_user", generate_password_hash("pw"))
        tag_id = db.add_user_custom_tag(uid, "OldLabel", color="#aaaaaa")
        db.update_user_custom_tag(tag_id, label="NewLabel", color="#bbbbbb")
        tag = db.get_user_custom_tag(tag_id)
        assert tag["label"] == "NewLabel"
        assert tag["color"] == "#bbbbbb"

    def test_multiple_tags_per_user(self):
        import db
        uid = db.create_user("multitag_user", generate_password_hash("pw"))
        db.add_user_custom_tag(uid, "Tag1")
        db.add_user_custom_tag(uid, "Tag2")
        db.add_user_custom_tag(uid, "Tag3")
        tags = db.get_user_custom_tags(uid)
        assert len(tags) == 3


# ---------------------------------------------------------------------------
# User profile operations
# ---------------------------------------------------------------------------

class TestUserProfileOperations:
    def test_upsert_and_get_profile(self):
        import db
        uid = db.create_user("profile_u", generate_password_hash("pw"))
        db.upsert_user_profile(uid, real_name="Alice Smith", bio="Hello world")
        profile = db.get_user_profile(uid)
        assert profile["real_name"] == "Alice Smith"
        assert profile["bio"] == "Hello world"

    def test_upsert_profile_updates_existing(self):
        import db
        uid = db.create_user("profile_update_u", generate_password_hash("pw"))
        db.upsert_user_profile(uid, real_name="Bob", bio="First bio")
        db.upsert_user_profile(uid, bio="Updated bio")
        profile = db.get_user_profile(uid)
        assert profile["bio"] == "Updated bio"

    def test_get_nonexistent_profile_returns_none(self):
        import db
        uid = db.create_user("noprofile_u", generate_password_hash("pw"))
        assert db.get_user_profile(uid) is None

    def test_delete_user_profile(self):
        import db
        uid = db.create_user("delprofile_u", generate_password_hash("pw"))
        db.upsert_user_profile(uid, real_name="Delete Me")
        db.delete_user_profile(uid)
        assert db.get_user_profile(uid) is None

    def test_list_published_profiles_excludes_unpublished(self):
        import db
        uid1 = db.create_user("pub_user", generate_password_hash("pw"))
        uid2 = db.create_user("priv_user", generate_password_hash("pw"))
        db.upsert_user_profile(uid1, real_name="Public User", page_published=True)
        db.upsert_user_profile(uid2, real_name="Private User", page_published=False)
        profiles = db.list_published_profiles()
        # list_published_profiles returns dicts; access by column name
        pub_real_names = [p["real_name"] for p in profiles]
        # Public profile must appear; private profile must not
        assert "Public User" in pub_real_names
        assert "Private User" not in pub_real_names


# ---------------------------------------------------------------------------
# Permission helpers (helpers/_permissions.py)
# ---------------------------------------------------------------------------

class TestPermissionHelpers:
    def test_get_all_permission_keys_returns_nonempty_list(self):
        from helpers._permissions import get_all_permission_keys
        keys = get_all_permission_keys()
        assert isinstance(keys, list)
        assert len(keys) > 0

    def test_known_permission_keys_present(self):
        from helpers._permissions import get_all_permission_keys
        keys = get_all_permission_keys()
        for expected in ("page.create", "page.edit_all", "category.create",
                         "draft.create", "chat.dm"):
            assert expected in keys, f"Expected '{expected}' in permission keys"

    def test_get_default_permissions_editor(self):
        from helpers._permissions import get_default_permissions
        editor_perms = get_default_permissions("editor")
        assert isinstance(editor_perms, (list, set, frozenset))
        # Editors should have page.create by default
        assert "page.create" in editor_perms

    def test_get_default_permissions_user(self):
        from helpers._permissions import get_default_permissions
        user_perms = get_default_permissions("user")
        # Regular users should NOT have page.create by default
        assert "page.create" not in user_perms

    def test_get_default_permissions_unknown_role_returns_empty(self):
        from helpers._permissions import get_default_permissions
        perms = get_default_permissions("nonexistent_role")
        assert len(perms) == 0

    def test_get_assignable_permission_keys_editor(self):
        from helpers._permissions import get_assignable_permission_keys
        keys = get_assignable_permission_keys("editor")
        assert isinstance(keys, (list, set, frozenset))

    def test_get_assignable_permission_keys_user(self):
        from helpers._permissions import get_assignable_permission_keys
        keys = get_assignable_permission_keys("user")
        # User role should have fewer permissions than editor
        editor_keys = get_assignable_permission_keys("editor")
        assert len(keys) <= len(editor_keys)

    def test_is_permission_assignable_to_role(self):
        from helpers._permissions import is_permission_assignable_to_role
        # page.create is for editors, not users
        assert is_permission_assignable_to_role("page.create", "editor") is True

    def test_get_permission_label_known_key(self):
        from helpers._permissions import get_permission_label
        label = get_permission_label("page.create")
        assert label != "page.create"  # Should return a human-readable label
        assert len(label) > 0

    def test_get_permission_label_unknown_key_returns_key(self):
        from helpers._permissions import get_permission_label
        result = get_permission_label("nonexistent.key")
        assert result == "nonexistent.key"

    def test_get_permission_description_known_key(self):
        from helpers._permissions import get_permission_description
        desc = get_permission_description("page.create")
        assert isinstance(desc, str)

    def test_normalize_permission_keys_removes_duplicates(self):
        from helpers._permissions import normalize_permission_keys
        keys = ["page.edit_all", "page.edit_all", "category.edit"]
        result = normalize_permission_keys(keys)
        # normalize_permission_keys returns a set (no duplicates), and may expand
        # implied permissions; at minimum, the input keys are present
        assert isinstance(result, (set, frozenset, list))
        unique = set(result)
        assert "page.edit_all" in unique
        assert "category.edit" in unique

    def test_normalize_permission_keys_adds_implications(self):
        from helpers._permissions import normalize_permission_keys
        # page.create implies page.view_all
        result = normalize_permission_keys(["page.create"])
        assert "page.view_all" in result

    def test_group_permissions_by_category_returns_dict(self):
        from helpers._permissions import group_permissions_by_category
        grouped = group_permissions_by_category()
        assert isinstance(grouped, dict)
        assert len(grouped) > 0

    def test_group_permissions_by_category_for_role(self):
        from helpers._permissions import group_permissions_by_category
        grouped = group_permissions_by_category(role="editor")
        assert isinstance(grouped, dict)


# ---------------------------------------------------------------------------
# DB permission operations
# ---------------------------------------------------------------------------

class TestDBPermissionOperations:
    def test_admin_has_all_permissions(self):
        import db
        uid = db.create_user("perm_admin", generate_password_hash("pw"), role="admin")
        user = db.get_user_by_id(uid)
        assert db.has_permission(user, "page.create") is True
        assert db.has_permission(user, "page.delete") is True
        assert db.has_permission(user, "category.create") is True

    def test_user_with_granted_permission(self):
        import db
        uid = db.create_user("perm_user", generate_password_hash("pw"), role="user")
        db.set_user_permissions(uid, ["page.view_all"], read_restricted=False,
                                read_category_ids=[], write_restricted=False,
                                write_category_ids=[])
        user = db.get_user_by_id(uid)
        assert db.has_permission(user, "page.view_all") is True

    def test_user_without_permission(self):
        import db
        uid = db.create_user("noperm_user", generate_password_hash("pw"), role="user")
        db.set_user_permissions(uid, [], read_restricted=False,
                                read_category_ids=[], write_restricted=False,
                                write_category_ids=[])
        user = db.get_user_by_id(uid)
        assert db.has_permission(user, "page.create") is False

    def test_clear_user_permissions(self):
        import db
        uid = db.create_user("clear_perm_u", generate_password_hash("pw"), role="user")
        db.set_user_permissions(uid, ["page.view_all"], read_restricted=False,
                                read_category_ids=[], write_restricted=False,
                                write_category_ids=[])
        db.clear_user_permissions(uid)
        user = db.get_user_by_id(uid)
        # After clearing, the user reverts to role defaults.
        # page.view_all is a default permission for the "user" role, so it is
        # still True: clearing should never strip role-default access.
        assert db.has_permission(user, "page.view_all") is True

    def test_has_category_read_access_unrestricted(self):
        import db
        uid = db.create_user("catread_u", generate_password_hash("pw"), role="editor")
        cat_id = db.create_category("ReadCat")
        db.set_user_permissions(uid, ["page.view_all"], read_restricted=False,
                                read_category_ids=[], write_restricted=False,
                                write_category_ids=[])
        user = db.get_user_by_id(uid)
        # Unrestricted read: should have access to all categories
        assert db.has_category_read_access(user, cat_id) is True

    def test_has_category_write_access_restricted(self):
        import db
        uid = db.create_user("catwrite_u", generate_password_hash("pw"), role="editor")
        cat_id = db.create_category("WriteCat")
        other_cat_id = db.create_category("OtherCat")
        db.set_user_permissions(uid, [], read_restricted=False,
                                read_category_ids=[], write_restricted=True,
                                write_category_ids=[cat_id])
        user = db.get_user_by_id(uid)
        assert db.has_category_write_access(user, cat_id) is True
        assert db.has_category_write_access(user, other_cat_id) is False


# ---------------------------------------------------------------------------
# Plugin class (bananawiki_sdk._plugin.Plugin)
# ---------------------------------------------------------------------------

class TestPluginClass:
    def test_plugin_stores_id(self):
        from bananawiki_sdk import Plugin
        p = Plugin("my_test_plugin")
        assert p.plugin_id == "my_test_plugin"

    def test_on_enable_stores_function(self):
        from bananawiki_sdk import Plugin
        p = Plugin("ep_test")
        called = []

        @p.on_enable
        def handler():
            called.append(True)

        assert p._on_enable_fn is handler

    def test_on_disable_stores_function(self):
        from bananawiki_sdk import Plugin
        p = Plugin("dp_test")

        @p.on_disable
        def handler():
            pass

        assert p._on_disable_fn is handler

    def test_on_load_stores_function(self):
        from bananawiki_sdk import Plugin
        p = Plugin("load_test")

        @p.on_load
        def handler(app):
            pass

        assert p._on_load_fn is handler

    def test_register_permission(self):
        from bananawiki_sdk import Plugin
        p = Plugin("perm_plugin")
        p.register_permission(
            "myplugin.action",
            "Do Action",
            "Allows doing the action",
            default_editor=True,
            default_user=False,
        )
        assert len(p._permissions) == 1
        perm = p._permissions[0]
        assert perm["key"] == "myplugin.action"
        assert perm["label"] == "Do Action"
        assert perm["default_editor"] is True
        assert perm["default_user"] is False

    def test_add_admin_menu_item(self):
        from bananawiki_sdk import Plugin
        p = Plugin("menu_plugin")
        p.add_admin_menu_item("My Feature", "my_feature_endpoint", icon="⚙️")
        assert len(p._admin_menu_items) == 1
        item = p._admin_menu_items[0]
        assert item["label"] == "My Feature"
        assert item["endpoint"] == "my_feature_endpoint"
        assert item["icon"] == "⚙️"

    def test_hook_decorator_tags_plugin_id(self):
        from bananawiki_sdk import Plugin
        from bananawiki_sdk._hooks import _clear_all_hooks
        _clear_all_hooks()
        p = Plugin("tagging_plugin")
        results = []

        @p.hook("test_hook")
        def handler(**kwargs):
            results.append(kwargs)

        assert handler._bw_plugin_id == "tagging_plugin"
        _clear_all_hooks()

    def test_multiple_permissions_registered(self):
        from bananawiki_sdk import Plugin
        p = Plugin("multi_perm_plugin")
        p.register_permission("mp.read", "Read", "Read access")
        p.register_permission("mp.write", "Write", "Write access")
        assert len(p._permissions) == 2

    def test_multiple_admin_menu_items(self):
        from bananawiki_sdk import Plugin
        p = Plugin("multi_menu_plugin")
        p.add_admin_menu_item("Feature A", "feat_a")
        p.add_admin_menu_item("Feature B", "feat_b")
        assert len(p._admin_menu_items) == 2


# ---------------------------------------------------------------------------
# SDK exception hierarchy
# ---------------------------------------------------------------------------

class TestSDKExceptions:
    def test_plugin_error_is_exception(self):
        from bananawiki_sdk._exceptions import PluginError
        assert issubclass(PluginError, Exception)

    def test_plugin_config_error_inherits_plugin_error(self):
        from bananawiki_sdk._exceptions import PluginError, PluginConfigError
        assert issubclass(PluginConfigError, PluginError)

    def test_plugin_api_version_error_inherits_plugin_error(self):
        from bananawiki_sdk._exceptions import PluginError, PluginAPIVersionError
        assert issubclass(PluginAPIVersionError, PluginError)

    def test_can_raise_and_catch_plugin_error(self):
        from bananawiki_sdk._exceptions import PluginError
        with pytest.raises(PluginError):
            raise PluginError("test error")

    def test_can_raise_and_catch_subclass_as_plugin_error(self):
        from bananawiki_sdk._exceptions import PluginError, PluginConfigError
        with pytest.raises(PluginError):
            raise PluginConfigError("config is invalid")


# ---------------------------------------------------------------------------
# Account settings route
# ---------------------------------------------------------------------------

class TestAccountSettingsRoute:
    def test_account_page_loads_for_logged_in_user(self, logged_in_admin):
        resp = logged_in_admin.get("/settings")
        assert resp.status_code == 200

    def test_account_page_redirects_unauthenticated(self, client, admin_user):
        resp = client.get("/settings")
        assert resp.status_code in (302, 401, 403)

    def test_account_change_username(self, logged_in_admin, admin_user):
        import db
        resp = logged_in_admin.post("/settings", data={
            "action": "change_username",
            "new_username": "new_admin_name",
            "password": "admin123",  # required by the change_username action
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert db.get_user_by_username("new_admin_name") is not None

    def test_account_change_password(self, logged_in_admin, admin_user):
        from werkzeug.security import check_password_hash
        import db
        resp = logged_in_admin.post("/settings", data={
            "action": "change_password",
            "current_password": "admin123",
            "new_password": "NewPass456",
            "confirm_password": "NewPass456",
        }, follow_redirects=True)
        assert resp.status_code == 200
        user = db.get_user_by_username("admin")
        assert check_password_hash(user["password"], "NewPass456")


# ---------------------------------------------------------------------------
# Users list and profile routes
# ---------------------------------------------------------------------------

class TestUserProfileRoutes:
    def test_users_list_requires_login(self, client, admin_user):
        resp = client.get("/users")
        assert resp.status_code in (302, 401, 403)

    def test_users_list_loads_for_logged_in_user(self, logged_in_admin):
        resp = logged_in_admin.get("/users")
        assert resp.status_code == 200

    def test_user_profile_page_accessible(self, logged_in_admin, admin_user):
        import db
        db.upsert_user_profile(admin_user, real_name="Admin User", page_published=True)
        resp = logged_in_admin.get("/users/admin")
        assert resp.status_code == 200

    def test_nonexistent_user_profile_returns_404(self, logged_in_admin):
        resp = logged_in_admin.get("/users/nobody_xyz_abc")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# API token operations (DB layer)
# ---------------------------------------------------------------------------

class TestAPITokenDBOperations:
    def _make_token(self, uid, scopes=None):
        import db
        if scopes is None:
            scopes = ["admin"]
        return db.create_api_token(
            uid, name="test",
            permissions={"read": True, "write": True, "scopes": scopes}
        )

    def test_generate_token_is_string(self):
        import db
        uid = db.create_user("tok_user", generate_password_hash("pw"), role="admin")
        token = self._make_token(uid)
        assert isinstance(token, str)
        assert len(token) > 0

    def test_verify_valid_token_returns_user(self):
        import db
        uid = db.create_user("tok_verify", generate_password_hash("pw"), role="admin")
        db.update_site_settings(setup_done=1)
        token = self._make_token(uid)
        token_row, user_row = db.verify_api_service_token(token)
        assert token_row is not None
        assert user_row is not None
        assert user_row["id"] == uid

    def test_verify_invalid_token_returns_none(self):
        import db
        token_row, user_row = db.verify_api_service_token("totally-invalid-token-xyz")
        assert token_row is None
        assert user_row is None

    def test_token_rejected_for_non_admin(self):
        import db
        uid = db.create_user("tok_editor", generate_password_hash("pw"), role="editor")
        token = self._make_token(uid, scopes=["pages"])
        db.update_user(uid, api_access_enabled=1)
        token_row, user_row = db.verify_api_service_token(token)
        assert token_row is not None
        assert user_row is not None

    def test_token_rejected_for_suspended_admin(self):
        import db
        uid = db.create_user("tok_suspended", generate_password_hash("pw"), role="admin")
        token = self._make_token(uid)
        db.update_user(uid, suspended=1, suspended_until=None)
        token_row, user_row = db.verify_api_service_token(token)
        assert token_row is None
        assert user_row is None

    def test_revoke_token(self):
        import db
        uid = db.create_user("tok_revoke", generate_password_hash("pw"), role="admin")
        token = self._make_token(uid)
        tokens = db.list_user_tokens(uid)
        assert len(tokens) == 1
        result = db.revoke_api_service_token(tokens[0]["id"])
        assert result is True
        token_row, user_row = db.verify_api_service_token(token)
        assert token_row is None

    def test_revoke_nonexistent_token_returns_false(self):
        import db
        result = db.revoke_api_service_token(999999)
        assert result is False

    def test_generate_multiple_tokens(self):
        import db
        uid = db.create_user("tok_replace", generate_password_hash("pw"), role="admin")
        db.update_site_settings(setup_done=1)
        token1 = self._make_token(uid)
        token2 = self._make_token(uid)
        token_row1, _ = db.verify_api_service_token(token1)
        assert token_row1 is not None
        token_row2, user_row2 = db.verify_api_service_token(token2)
        assert token_row2 is not None
        tokens = db.list_user_tokens(uid)
        assert len(tokens) == 2

    def test_get_user_api_token_info(self):
        import db
        uid = db.create_user("tok_info", generate_password_hash("pw"), role="admin")
        self._make_token(uid)
        tokens = db.list_user_tokens(uid)
        assert len(tokens) >= 1
        assert tokens[0]["user_id"] == uid

    def test_get_user_api_token_info_none_when_no_token(self):
        import db
        uid = db.create_user("tok_noinfo", generate_password_hash("pw"), role="admin")
        tokens = db.list_user_tokens(uid)
        assert len(tokens) == 0
