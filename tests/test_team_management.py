import asyncio
import unittest
from types import SimpleNamespace

from bot.core.card_kit import guide_card
from bot.core.models import Contestant, Team
from bot.modules.team_management.cards import add_form_card, delete_form_card, no_team_card
from bot.modules.team_management.service import TeamManagementService


class FakeContestants:
    def __init__(self, contestants):
        self.rows = {c.record_id: c for c in contestants}

    async def get_by_open_id(self, open_id):
        return next((c for c in self.rows.values() if c.open_id == open_id), None)

    async def find_by_phone(self, phone):
        return next((c for c in self.rows.values() if c.phone == phone), None)

    async def get(self, record_id):
        return self.rows.get(record_id)

    async def list_all(self):
        return list(self.rows.values())


class FakeTeams:
    def __init__(self, teams=None):
        self.rows = {t.record_id: t for t in (teams or [])}
        self.created = []
        self.updates = []

    async def list_all(self):
        return list(self.rows.values())

    async def create_many(self, rows):
        record_id = f"team-{len(self.rows) + 1}"
        row = Team(record_id=record_id, **rows[0])
        self.rows[record_id] = row
        self.created.append(row)
        return [record_id]

    async def batch_update(self, items):
        for record_id, fields in items:
            team = self.rows[record_id]
            for key, value in fields.items():
                setattr(team, key, list(value) if isinstance(value, list) else value)
            self.updates.append((record_id, fields))


def contestant(rid, open_id, phone, *, name=None, grade="大二", verified=True,
               approved=True):
    return Contestant(
        record_id=rid,
        contestant_no=rid.upper(),
        name=name or rid,
        open_id=open_id,
        phone=phone,
        grade=grade,
        verify_status="已验证" if verified else "未验证",
        audit_status="审核通过" if approved else "未审核",
    )


class TeamManagementServiceTests(unittest.TestCase):
    def setUp(self):
        self.captain = contestant("c1", "ou-captain", "13800000001")
        self.freshman = contestant("c2", "ou-freshman", "13800000002", grade="大一")
        self.other = contestant("c3", "ou-other", "13800000003")
        self.contestants = FakeContestants([self.captain, self.freshman, self.other])
        self.teams = FakeTeams()
        self.service = TeamManagementService(self.contestants, self.teams)

    def test_verified_unteamed_contestant_can_create_one_person_team(self):
        result = asyncio.run(self.service.create_team("ou-captain"))

        self.assertTrue(result.ok)
        self.assertEqual(len(self.teams.created), 1)
        self.assertEqual(self.teams.created[0].captain_ids, ["c1"])
        self.assertEqual(self.teams.created[0].all_member_ids, ["c1"])
        self.assertTrue(self.teams.created[0].team_no.startswith("T-"))

    def test_add_requires_freshman_when_current_team_has_none(self):
        self.teams = FakeTeams([Team(record_id="team-1", team_no="T-1", captain_ids=["c1"])])
        self.service = TeamManagementService(self.contestants, self.teams)

        result = asyncio.run(self.service.start_add("ou-captain", "13800000003"))

        self.assertFalse(result.ok)
        self.assertIn("大一新生", result.reason)
        self.assertEqual(self.service.pending, {})

    def test_add_creates_pending_change_and_confirmation_writes_member(self):
        self.teams = FakeTeams([Team(record_id="team-1", team_no="T-1", captain_ids=["c1"])])
        self.service = TeamManagementService(self.contestants, self.teams)

        start = asyncio.run(self.service.start_add("ou-captain", "13800000002"))
        self.assertTrue(start.ok)
        self.assertEqual(self.teams.rows["team-1"].all_member_ids, ["c1"])
        self.assertEqual(start.target_open_id, "ou-freshman")

        confirmed = asyncio.run(self.service.confirm(start.change_id, "ou-freshman"))

        self.assertTrue(confirmed.ok)
        self.assertEqual(self.teams.rows["team-1"].manual_member_ids, ["c2"])
        self.assertEqual(self.service.pending, {})

    def test_add_rejects_target_already_in_another_team(self):
        self.teams = FakeTeams([
            Team(record_id="team-1", team_no="T-1", captain_ids=["c1"]),
            Team(record_id="team-2", team_no="T-2", captain_ids=["c3"],
                 manual_member_ids=["c2"]),
        ])
        self.service = TeamManagementService(self.contestants, self.teams)
        result = asyncio.run(self.service.start_add("ou-captain", "13800000003"))

        self.assertFalse(result.ok)
        self.assertIn("其他队伍", result.reason)

    def test_add_can_transfer_contestant_from_one_person_team(self):
        solo_team = Team(record_id="solo-team", team_no="T-SOLO", captain_ids=["c2"])
        target_team = Team(record_id="team-1", team_no="T-1", captain_ids=["c1"],
                           manual_member_ids=["c3"])
        self.teams = FakeTeams([solo_team, target_team])
        self.service = TeamManagementService(self.contestants, self.teams)

        start = asyncio.run(self.service.start_add("ou-captain", "13800000002"))
        self.assertTrue(start.ok)
        confirmed = asyncio.run(self.service.confirm(start.change_id, "ou-freshman"))

        self.assertTrue(confirmed.ok)
        self.assertEqual(self.teams.rows["solo-team"].all_member_ids, [])
        self.assertEqual(self.teams.rows["team-1"].all_member_ids, ["c1", "c3", "c2"])

    def test_existing_freshman_allows_ordinary_member(self):
        self.teams = FakeTeams([Team(record_id="team-1", team_no="T-1",
                                     captain_ids=["c1"], manual_member_ids=["c2"])])
        self.service = TeamManagementService(self.contestants, self.teams)

        result = asyncio.run(self.service.start_add("ou-captain", "13800000003"))

        self.assertTrue(result.ok)

    def test_delete_requires_member_confirmation_and_allows_one_person_team(self):
        self.teams = FakeTeams([Team(record_id="team-1", team_no="T-1",
                                     captain_ids=["c1"], member_ids=["c2"])])
        self.service = TeamManagementService(self.contestants, self.teams)

        start = asyncio.run(self.service.start_remove("ou-captain", "c2"))
        self.assertTrue(start.ok)
        self.assertEqual(self.teams.rows["team-1"].all_member_ids, ["c1", "c2"])

        result = asyncio.run(self.service.confirm(start.change_id, "ou-freshman"))

        self.assertTrue(result.ok)
        self.assertEqual(self.teams.rows["team-1"].all_member_ids, ["c1"])
        self.assertEqual(self.teams.rows["team-1"].removed_member_ids, ["c2"])

    def test_reject_keeps_team_unchanged(self):
        self.teams = FakeTeams([Team(record_id="team-1", team_no="T-1", captain_ids=["c1"])])
        self.service = TeamManagementService(self.contestants, self.teams)

        start = asyncio.run(self.service.start_add("ou-captain", "13800000002"))
        result = asyncio.run(self.service.reject(start.change_id, "ou-freshman"))

        self.assertTrue(result.ok)
        self.assertEqual(self.teams.rows["team-1"].all_member_ids, ["c1"])

    def test_expired_change_cannot_be_confirmed(self):
        now = [100.0]
        self.teams = FakeTeams([Team(record_id="team-1", team_no="T-1", captain_ids=["c1"])])
        self.service = TeamManagementService(self.contestants, self.teams,
                                              clock=lambda: now[0])

        start = asyncio.run(self.service.start_add("ou-captain", "13800000002"))
        now[0] += 15 * 60
        result = asyncio.run(self.service.confirm(start.change_id, "ou-freshman"))

        self.assertFalse(result.ok)
        self.assertIn("失效", result.title)
        self.assertEqual(self.teams.rows["team-1"].all_member_ids, ["c1"])

    def test_cards_expose_expected_actions_and_do_not_expose_phone_options(self):
        create = no_team_card()
        self.assertEqual(create["body"]["elements"][1]["behaviors"][0]["value"]["action"],
                         "team_create")

        add = add_form_card()
        self.assertEqual(add["body"]["elements"][0]["elements"][0]["name"], "phone")

        delete = delete_form_card([{
            "text": {"tag": "plain_text", "content": "队员"}, "value": "c2"
        }])
        select = delete["body"]["elements"][0]["elements"][0]
        self.assertEqual(select["tag"], "select_static")
        self.assertEqual(select["options"][0]["value"], "c2")

    def test_guide_shortcuts_are_mobile_friendly_and_include_team_management(self):
        elements = guide_card()["body"]["elements"]
        shortcut_rows = [element for element in elements
                         if element.get("tag") == "column_set"]

        self.assertEqual(len(shortcut_rows), 3)
        self.assertTrue(all(len(row["columns"]) <= 2 for row in shortcut_rows))
        actions = [
            column["elements"][0]["behaviors"][0]["value"]["action"]
            for row in shortcut_rows
            for column in row["columns"]
            if column["elements"]
        ]
        self.assertEqual(actions, [
            "verify", "profile", "team", "vote", "activity", "votes_board",
        ])


if __name__ == "__main__":
    unittest.main()
