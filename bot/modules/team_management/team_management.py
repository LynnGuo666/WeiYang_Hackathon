from __future__ import annotations

import logging

from ...core.card_kit import result_card
from ...core.lark_client import send_card
from ...core.plugin import Plugin
from ...core.registry import REGISTRY, CardCtx, MsgCtx
from ...core.service import SVC
from .cards import (add_form_card, change_confirmation_card, delete_form_card,
                    no_team_card, team_card)
from .service import ActionResult, TeamManagementService

log = logging.getLogger(__name__)
_SERVICE: TeamManagementService | None = None


def _service() -> TeamManagementService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = TeamManagementService(SVC.contestants, SVC.teams)
    return _SERVICE


def _action_data(ctx: CardCtx) -> dict:
    value = (ctx.raw.get("action") or {}).get("value") or {}
    return value if isinstance(value, dict) else {}


async def _team_lines(service: TeamManagementService, team) -> tuple[list[str], bool, bool]:
    name_by_id = {c.record_id: c for c in await service.contestants.list_all()}
    captain_ids = set(team.captain_ids)
    lines = [f"**队伍编号：{team.team_no or team.record_id}**（{len(team.all_member_ids)}/5）", "", "**队伍成员**"]
    for rid in team.all_member_ids:
        member = name_by_id.get(rid)
        if member is None:
            continue
        role = "队长" if rid in captain_ids else "队员"
        freshman = " · 大一" if member.grade == "大一" else ""
        lines.append(f"- {member.name or member.contestant_no}（{member.contestant_no}，{role}{freshman}）")
    valid = len(team.all_member_ids) >= 3 and len(team.all_member_ids) <= 5 and any(
        name_by_id.get(rid) and name_by_id[rid].grade == "大一" for rid in team.all_member_ids)
    if not valid:
        lines += ["", "⚠️ 当前队伍仍未满足最终组队要求：至少 3 人，且至少 1 名大一新生。"]
    return lines, bool(team.captain_ids), bool(team.all_member_ids[1:])


async def _send_result(open_id: str, result: ActionResult) -> None:
    await send_card(open_id, result_card(result.title, result.ok,
                                         [result.reason] if result.reason else ["操作已完成。"]))


async def handle_team(ctx: MsgCtx) -> None:
    service = _service()
    contestant, team = await service.team_for_open_id(ctx.open_id)
    if contestant is None or not contestant.verified or not contestant.approved:
        await send_card(ctx.open_id, result_card("无法使用组队功能", False, ["请先完成选手验证和审核。"]))
        return
    if team is None:
        await send_card(ctx.open_id, no_team_card())
        return
    lines, _captain, has_members = await _team_lines(service, team)
    pending = next((dict(c, change_id=cid) for cid, c in service.pending.items()
                    if c["team_id"] == team.record_id), None)
    await send_card(ctx.open_id, team_card(lines,
                                           can_manage=contestant.record_id in team.captain_ids,
                                           has_members=has_members, pending=pending))


async def handle_create(ctx: MsgCtx | CardCtx) -> None:
    result = await _service().create_team(ctx.open_id)
    if not result.ok:
        await _send_result(ctx.open_id, result)
        return
    await handle_team(MsgCtx(ctx.open_id, getattr(ctx, "chat_id", ""), "p2p", "", {}, False))


async def handle_add_form(ctx: CardCtx) -> None:
    contestant, team = await _service().team_for_open_id(ctx.open_id)
    if contestant is None or team is None or contestant.record_id not in team.captain_ids:
        await send_card(ctx.open_id, result_card("无法添加成员", False, ["只有当前队长可以添加成员。"]))
        return
    await send_card(ctx.open_id, add_form_card())


async def _start_add(open_id: str, phone: str) -> None:
    service = _service()
    result = await service.start_add(open_id, phone)
    if not result.ok:
        await _send_result(open_id, result)
        return
    change = service.pending[result.change_id]
    captain = await service.contestants.get(change["captain_id"])
    target = await service.contestants.get(change["target_id"])
    if captain is None or target is None:
        await _send_result(open_id, ActionResult(False, "邀请失败", "队伍成员状态已发生变化。"))
        return
    await send_card(result.target_open_id, change_confirmation_card("组队邀请",
        [f"队长 **{captain.name}** 邀请你加入队伍 **{result.team.team_no}**。",
         f"确认后你将成为该队伍成员。邀请 15 分钟内有效。"], result.change_id))
    lines, _captain, has_members = await _team_lines(service, result.team)
    await send_card(open_id, team_card(lines, can_manage=True, has_members=has_members,
                                       pending=dict(change, change_id=result.change_id)))


async def handle_add_submit(ctx: CardCtx) -> None:
    await _start_add(ctx.open_id, str((ctx.form_value or {}).get("phone") or ""))


async def handle_add_command(ctx: MsgCtx) -> None:
    parts = ctx.text.split(maxsplit=1)
    await _start_add(ctx.open_id, parts[1] if len(parts) == 2 else "")


async def handle_delete_form(ctx: CardCtx) -> None:
    service = _service()
    contestant, team = await service.team_for_open_id(ctx.open_id)
    if contestant is None or team is None or contestant.record_id not in team.captain_ids:
        await send_card(ctx.open_id, result_card("无法删除成员", False, ["只有当前队长可以删除成员。"]))
        return
    options = []
    for member in await service.member_options(team):
        options.append({"text": {"tag": "plain_text", "content": member.name or member.contestant_no},
                        "value": member.record_id})
    if not options:
        await send_card(ctx.open_id, result_card("无法删除成员", False, ["当前队伍没有可删除的队员。"]))
        return
    await send_card(ctx.open_id, delete_form_card(options))


async def _start_delete(open_id: str, target_id: str) -> None:
    service = _service()
    result = await service.start_remove(open_id, target_id)
    if not result.ok:
        await _send_result(open_id, result)
        return
    captain = await service.contestants.get(result.team.captain_ids[0])
    target = await service.contestants.get(target_id)
    if captain is None or target is None:
        await _send_result(open_id, ActionResult(False, "删除失败", "队伍成员状态已发生变化。"))
        return
    await send_card(result.target_open_id, change_confirmation_card("删除队员确认",
        [f"队长 **{captain.name}** 请求将你从队伍 **{result.team.team_no}** 中删除。",
         "确认后你将退出该队伍。请求 15 分钟内有效。"], result.change_id))
    lines, _captain, has_members = await _team_lines(service, result.team)
    await send_card(open_id, team_card(
        lines, can_manage=True, has_members=has_members,
        pending=dict(service.pending[result.change_id], change_id=result.change_id)))


async def handle_delete_submit(ctx: CardCtx) -> None:
    await _start_delete(ctx.open_id, str((ctx.form_value or {}).get("member_id") or ""))


async def _finish_change(ctx: CardCtx, accepted: bool) -> None:
    service = _service()
    data = _action_data(ctx)
    change_id = str(data.get("change_id") or "")
    change = service.pending.get(change_id)
    if change is None:
        await _send_result(ctx.open_id, ActionResult(False, "变更已失效", "该组队变更已失效，请重新发起。"))
        return
    captain = await service.contestants.get(change["captain_id"])
    if accepted:
        result = await service.confirm(change_id, ctx.open_id)
        if not result.ok:
            await _send_result(captain.open_id, result)
            return
        await send_card(ctx.open_id, result_card("组队变更成功", True, ["你的队伍关系已更新。"]))
        await send_card(captain.open_id, result_card("组队变更成功", True, ["队伍关系已更新。"]))
        if result.team is not None:
            lines, _captain, has_members = await _team_lines(service, result.team)
            await send_card(captain.open_id, team_card(
                lines, can_manage=True, has_members=has_members))
            if change["kind"] == "add":
                await send_card(ctx.open_id, team_card(
                    lines, can_manage=False, has_members=has_members))
    else:
        result = await service.reject(change_id, ctx.open_id)
        if result.ok:
            await send_card(ctx.open_id, result_card("已拒绝组队变更", True, ["队伍关系未改变。"]))
            await send_card(captain.open_id, result_card("成员拒绝组队变更", False, ["本次组队变更未生效。"]))
        else:
            await _send_result(ctx.open_id, result)


async def handle_cancel(ctx: CardCtx) -> None:
    service = _service()
    data = _action_data(ctx)
    change_id = str(data.get("change_id") or "")
    change = service.pending.get(change_id)
    result = await service.cancel(change_id, ctx.open_id)
    await _send_result(ctx.open_id, result)
    if result.ok and change:
        target = await service.contestants.get(change["target_id"])
        if target and target.open_id:
            await send_card(target.open_id, result_card("组队变更已取消", False, ["本次组队变更未生效。"]))


async def expire_changes() -> None:
    try:
        await _service().expire()
    except Exception:
        log.exception("组队变更过期检查失败")


class TeamManagementPlugin(Plugin):
    name = "team_management"
    dependencies = ()

    def setup(self) -> None:
        REGISTRY.user_command("组队", "组队管理", "我的队伍", plugin=self.name)(handle_team)
        REGISTRY.user_command("创建队伍", plugin=self.name)(handle_create)
        REGISTRY.user_command("添加队员", plugin=self.name, accepts_args=True)(handle_add_command)
        REGISTRY.on_card("team", scope="user", plugin=self.name)(handle_team)
        REGISTRY.on_card("team_create", scope="user", plugin=self.name)(handle_create)
        REGISTRY.on_card("team_add", scope="user", plugin=self.name)(handle_add_form)
        REGISTRY.on_card("team_add_submit", scope="user", plugin=self.name)(handle_add_submit)
        REGISTRY.on_card("team_delete", scope="user", plugin=self.name)(handle_delete_form)
        REGISTRY.on_card("team_delete_submit", scope="user", plugin=self.name)(handle_delete_submit)
        REGISTRY.on_card("team_change_confirm", scope="user", plugin=self.name)(
            lambda ctx: _finish_change(ctx, True))
        REGISTRY.on_card("team_change_reject", scope="user", plugin=self.name)(
            lambda ctx: _finish_change(ctx, False))
        REGISTRY.on_card("team_change_cancel", scope="user", plugin=self.name)(handle_cancel)
        REGISTRY.job("组队变更过期检查", 1, plugin=self.name)(expire_changes)
