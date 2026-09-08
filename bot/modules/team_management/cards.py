from __future__ import annotations


def _button(text: str, action: str, *, style: str = "default", **values) -> dict:
    value = {"action": action, **values}
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": style,
        "size": "medium",
        "behaviors": [{"type": "callback", "value": value}],
    }


def team_card(lines: list[str], *, can_manage: bool, has_members: bool,
              pending: dict | None = None) -> dict:
    elements: list[dict] = [{"tag": "markdown", "content": "\n".join(lines)}]
    if pending:
        kind = "添加成员" if pending["kind"] == "add" else "删除成员"
        elements += [
            {"tag": "hr"},
            {"tag": "markdown", "content": f"⏳ **待确认变更：{kind}**\n有效期 15 分钟。"},
            _button("取消变更", "team_change_cancel", change_id=pending["change_id"]),
        ]
    elif can_manage:
        elements += [{"tag": "hr"}, {"tag": "column_set", "flex_mode": "bisect", "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                _button("➕ 添加成员", "team_add", style="primary")]},
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                _button("➖ 删除成员", "team_delete")] if has_members else []},
        ]}]
    else:
        elements += [{"tag": "markdown", "content": "你是队员，只有队长可以管理成员。"}]
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": "我的队伍"}, "template": "blue"},
        "body": {"elements": elements},
    }


def no_team_card() -> dict:
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": "组队管理"}, "template": "blue"},
        "body": {"elements": [
            {"tag": "markdown", "content": "你当前还没有加入队伍。\n可以创建一支临时队伍，再继续添加成员。"},
            _button("创建队伍", "team_create", style="primary"),
        ]},
    }


def add_form_card() -> dict:
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": "添加成员"}, "template": "blue"},
        "body": {"elements": [{
            "tag": "form", "name": "team_add_form", "elements": [
                {"tag": "input", "name": "phone", "label": {"tag": "plain_text", "content": "选手手机号"},
                 "placeholder": {"tag": "plain_text", "content": "请输入报名手机号"}, "max_length": 20},
                {"tag": "button", "name": "submit", "text": {"tag": "plain_text", "content": "发送组队邀请"},
                 "type": "primary", "size": "medium", "form_action_type": "submit",
                 "behaviors": [{"type": "callback", "value": {"action": "team_add_submit"}}]},
            ],
        }]},
    }


def delete_form_card(options: list[dict]) -> dict:
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": "删除成员"}, "template": "blue"},
        "body": {"elements": [{
            "tag": "form", "name": "team_delete_form", "elements": [
                {"tag": "select_static", "name": "member_id",
                 "placeholder": {"tag": "plain_text", "content": "选择要删除的队员"},
                 "options": options},
                {"tag": "button", "name": "submit", "text": {"tag": "plain_text", "content": "发送删除请求"},
                 "type": "primary", "size": "medium", "form_action_type": "submit",
                 "behaviors": [{"type": "callback", "value": {"action": "team_delete_submit"}}]},
            ],
        }]},
    }


def change_confirmation_card(title: str, lines: list[str], change_id: str) -> dict:
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "orange"},
        "body": {"elements": [
            {"tag": "markdown", "content": "\n".join(lines)},
            {"tag": "column_set", "flex_mode": "bisect", "columns": [
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                    _button("确认", "team_change_confirm", style="primary", change_id=change_id)]},
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                    _button("拒绝", "team_change_reject", change_id=change_id)]},
            ]},
        ]},
    }
