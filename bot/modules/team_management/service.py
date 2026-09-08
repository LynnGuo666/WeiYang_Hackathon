"""选手手动组队的领域用例服务。

卡片和文本指令都通过本服务执行；本模块不依赖飞书字段名，只依赖 core
Repository 接口，便于在内存仓库上验证组队行为。
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from ...core.models import Contestant, Team

CHANGE_TIMEOUT_SECONDS = 15 * 60


@dataclass
class ActionResult:
    ok: bool
    title: str
    reason: str = ""
    change_id: str = ""
    target_open_id: str = ""
    team: Team | None = None


class TeamManagementService:
    """执行创建、添加、删除及待确认变更。

    pending 是进程内的短期状态，与现有验证卡片过期机制保持一致；确认时
    始终重新读取队伍和选手，旧卡片不会携带可直接写入的成员列表。
    """

    def __init__(self, contestants, teams, *, clock: Callable[[], float] | None = None):
        self.contestants = contestants
        self.teams = teams
        self.clock = clock or time.time
        self.pending: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    async def _contestant_by_open_id(self, open_id: str) -> Contestant | None:
        return await self.contestants.get_by_open_id(open_id)

    async def _all_teams(self) -> list[Team]:
        return await self.teams.list_all()

    @staticmethod
    def _normalize_phone(phone: str) -> str:
        digits = re.sub(r"\D", "", phone or "")
        if digits.startswith("86") and len(digits) == 13:
            digits = digits[2:]
        return digits

    async def _find_phone_matches(self, phone: str) -> list[Contestant]:
        normalized = self._normalize_phone(phone)
        return [c for c in await self.contestants.list_all()
                if self._normalize_phone(c.phone) == normalized and normalized]

    async def _team_for(self, contestant_id: str) -> Team | None:
        for team in await self._all_teams():
            if contestant_id in team.all_member_ids:
                return team
        return None

    async def _purge_expired(self) -> None:
        now = self.clock()
        for change_id, change in list(self.pending.items()):
            if change["expires_at"] <= now:
                self.pending.pop(change_id, None)

    def _has_pending_for(self, team_id: str, target_id: str) -> bool:
        return any(c["team_id"] == team_id or c["target_id"] == target_id
                   for c in self.pending.values())

    async def create_team(self, captain_open_id: str) -> ActionResult:
        captain = await self._contestant_by_open_id(captain_open_id)
        if captain is None or not captain.verified or not captain.approved:
            return ActionResult(False, "无法创建队伍", "请先完成选手验证和审核。")
        async with self._lock_for(f"contestant:{captain.record_id}"):
            if await self._team_for(captain.record_id):
                return ActionResult(False, "无法创建队伍", "你已经在其他队伍中。")
            created = await self.teams.create_many([{
                "team_no": "",
                "captain_ids": [captain.record_id],
                "member_ids": [],
                "manual_member_ids": [],
            }])
            record_id = created[0] if created else ""
            if not record_id:
                return ActionResult(False, "创建队伍失败", "队伍记录创建失败，请稍后重试。")
            team_no = f"T-{record_id[-8:]}"
            await self.teams.batch_update([(record_id, {"team_no": team_no})])
            team = next((t for t in await self._all_teams() if t.record_id == record_id), None)
            return ActionResult(True, "队伍创建成功", change_id="", team=team)

    async def start_add(self, captain_open_id: str, phone: str) -> ActionResult:
        await self._purge_expired()
        captain = await self._contestant_by_open_id(captain_open_id)
        if captain is None or not captain.verified or not captain.approved:
            return ActionResult(False, "添加失败", "请先完成选手验证和审核。")
        team = await self._team_for(captain.record_id)
        if team is None or captain.record_id not in team.captain_ids:
            return ActionResult(False, "添加失败", "只有当前队长可以添加成员。")
        if len(team.all_member_ids) >= 5:
            return ActionResult(False, "添加失败", "队伍已满 5 人。")
        if self._has_pending_for(team.record_id, ""):
            return ActionResult(False, "添加失败", "当前队伍已有待确认变更，请稍后再试。")
        matches = await self._find_phone_matches(phone)
        if not matches:
            return ActionResult(False, "添加失败", "手机号无法匹配选手。")
        if len(matches) > 1:
            return ActionResult(False, "添加失败", "手机号匹配到多个选手，请联系管理员。")
        target = matches[0]
        if not target.verified or not target.approved:
            return ActionResult(False, "添加失败", "该选手尚未完成验证和审核。")
        if not target.open_id:
            return ActionResult(False, "添加失败", "该选手尚未绑定飞书账号，暂时无法发送组队确认卡片。")
        if target.record_id in team.all_member_ids:
            return ActionResult(False, "添加失败", "该选手已经在当前队伍中。")
        existing_team = await self._team_for(target.record_id)
        if existing_team is not None and len(existing_team.all_member_ids) != 1:
            return ActionResult(False, "添加失败", "该选手已经在其他队伍中。")
        if not await self._team_has_freshman_async(team) and target.grade != "大一":
            return ActionResult(False, "添加失败", "当前队伍缺少大一新生，必须先添加大一新生。")
        if any(c["target_id"] == target.record_id for c in self.pending.values()):
            return ActionResult(False, "添加失败", "该选手已有待确认变更。")
        change_id = uuid.uuid4().hex
        self.pending[change_id] = {
            "kind": "add", "team_id": team.record_id,
            "captain_id": captain.record_id, "target_id": target.record_id,
            "created_at": self.clock(),
            "expires_at": self.clock() + CHANGE_TIMEOUT_SECONDS,
        }
        return ActionResult(True, "等待确认", change_id=change_id,
                            target_open_id=target.open_id, team=team)

    async def start_remove(self, captain_open_id: str, target_id: str) -> ActionResult:
        await self._purge_expired()
        captain = await self._contestant_by_open_id(captain_open_id)
        team = await self._team_for(captain.record_id) if captain else None
        if captain is None or team is None or captain.record_id not in team.captain_ids:
            return ActionResult(False, "删除失败", "只有当前队长可以删除成员。")
        if target_id not in team.all_member_ids or target_id in team.captain_ids:
            return ActionResult(False, "删除失败", "请选择当前队员。")
        if self._has_pending_for(team.record_id, ""):
            return ActionResult(False, "删除失败", "当前队伍已有待确认变更，请稍后再试。")
        target = await self.contestants.get(target_id)
        if target is None:
            return ActionResult(False, "删除失败", "找不到要删除的队员。")
        if not target.open_id:
            return ActionResult(False, "删除失败", "该队员尚未绑定飞书账号，暂时无法发送确认卡片。")
        change_id = uuid.uuid4().hex
        self.pending[change_id] = {
            "kind": "remove", "team_id": team.record_id,
            "captain_id": captain.record_id, "target_id": target.record_id,
            "created_at": self.clock(),
            "expires_at": self.clock() + CHANGE_TIMEOUT_SECONDS,
        }
        return ActionResult(True, "等待确认", change_id=change_id,
                            target_open_id=target.open_id, team=team)

    async def confirm(self, change_id: str, actor_open_id: str) -> ActionResult:
        await self._purge_expired()
        change = self.pending.get(change_id)
        if change is None:
            return ActionResult(False, "变更已失效", "该组队变更已失效，请重新发起。")
        actor = await self._contestant_by_open_id(actor_open_id)
        if actor is None or actor.record_id != change["target_id"]:
            return ActionResult(False, "操作失败", "只有被操作成员可以确认该变更。")
        async with self._lock_for(f"team:{change['team_id']}"):
            team = next((t for t in await self._all_teams()
                          if t.record_id == change["team_id"]), None)
            target = await self.contestants.get(change["target_id"])
            if team is None or target is None:
                self.pending.pop(change_id, None)
                return ActionResult(False, "变更失败", "队伍或成员状态已发生变化。")
            if change["kind"] == "add":
                if not target.verified or not target.approved:
                    self.pending.pop(change_id, None)
                    return ActionResult(False, "变更失败", "该选手的验证或审核状态已发生变化。")
                existing_team = await self._team_for(target.record_id)
                if (existing_team is not None
                        and existing_team.record_id != team.record_id
                        and len(existing_team.all_member_ids) != 1):
                    self.pending.pop(change_id, None)
                    return ActionResult(False, "变更失败", "该选手已经加入其他队伍。")
                if len(team.all_member_ids) >= 5:
                    self.pending.pop(change_id, None)
                    return ActionResult(False, "变更失败", "队伍已满 5 人。")
                if not await self._team_has_freshman_async(team) and target.grade != "大一":
                    self.pending.pop(change_id, None)
                    return ActionResult(False, "变更失败", "当前队伍缺少大一新生，必须先添加大一新生。")
                updates = []
                if existing_team is not None and existing_team.record_id != team.record_id:
                    # 一人临时队伍允许被新队伍接收；移除其唯一队长后，旧队伍自然变为空队伍。
                    updates.append((existing_team.record_id, {
                        "captain_ids": [],
                        "member_ids": [rid for rid in existing_team.member_ids
                                       if rid != target.record_id],
                        "manual_member_ids": [rid for rid in existing_team.manual_member_ids
                                               if rid != target.record_id],
                    }))
                updates.append((team.record_id, {
                    "manual_member_ids": list(dict.fromkeys(
                        team.manual_member_ids + [target.record_id])),
                    "removed_member_ids": [rid for rid in team.removed_member_ids
                                           if rid != target.record_id],
                }))
                await self.teams.batch_update(updates)
            else:
                if target.record_id not in team.all_member_ids or target.record_id in team.captain_ids:
                    self.pending.pop(change_id, None)
                    return ActionResult(False, "变更失败", "该成员已经不在队伍中。")
                await self.teams.batch_update([(team.record_id, {
                    "member_ids": [rid for rid in team.member_ids if rid != target.record_id],
                    "manual_member_ids": [rid for rid in team.manual_member_ids
                                           if rid != target.record_id],
                    "removed_member_ids": list(dict.fromkeys(
                        team.removed_member_ids + [target.record_id])),
                })])
            self.pending.pop(change_id, None)
            updated = next((t for t in await self._all_teams()
                            if t.record_id == team.record_id), team)
            return ActionResult(True, "组队变更成功", team=updated)

    async def reject(self, change_id: str, actor_open_id: str) -> ActionResult:
        await self._purge_expired()
        change = self.pending.get(change_id)
        actor = await self._contestant_by_open_id(actor_open_id)
        if change is None:
            return ActionResult(False, "变更已失效", "该组队变更已失效，请重新发起。")
        if actor is None or actor.record_id != change["target_id"]:
            return ActionResult(False, "操作失败", "只有被操作成员可以拒绝该变更。")
        self.pending.pop(change_id, None)
        return ActionResult(True, "已拒绝组队变更")

    async def cancel(self, change_id: str, captain_open_id: str) -> ActionResult:
        await self._purge_expired()
        change = self.pending.get(change_id)
        captain = await self._contestant_by_open_id(captain_open_id)
        if change is None:
            return ActionResult(False, "变更已失效", "该组队变更已失效。")
        if captain is None or captain.record_id != change["captain_id"]:
            return ActionResult(False, "操作失败", "只有发起队长可以取消该变更。")
        self.pending.pop(change_id, None)
        return ActionResult(True, "已取消组队变更")

    async def expire(self) -> int:
        before = len(self.pending)
        await self._purge_expired()
        return before - len(self.pending)

    async def team_for_open_id(self, open_id: str) -> tuple[Contestant | None, Team | None]:
        contestant = await self._contestant_by_open_id(open_id)
        return contestant, await self._team_for(contestant.record_id) if contestant else None

    async def member_options(self, team: Team) -> list[Contestant]:
        members = []
        for rid in team.all_member_ids:
            if rid in team.captain_ids:
                continue
            member = await self.contestants.get(rid)
            if member is not None:
                members.append(member)
        return members

    async def _team_has_freshman_async(self, team: Team) -> bool:
        for rid in team.all_member_ids:
            member = await self.contestants.get(rid)
            if member is not None and member.grade == "大一":
                return True
        return False
