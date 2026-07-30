"""新成员欢迎：目的选择 + 私密指引。"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
import discord

import tag_browser
import app_emojis


def _emo(text: str, *, scenario: str | None = None, emotion: str | None = None) -> str:
    return app_emojis.decorate(text, scenario=scenario, emotion=emotion)

_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "onboarding_config.json")
_VIEW_TIMEOUT = None  # 持久 View，重启后在 on_ready 重新注册
_CUSTOM_NOTIFY_COOLDOWN = 86400  # 24h 内同一人只通知管理员一次
_BUTTONS_PER_ROW = 3

_config: dict | None = None
_custom_notify_at: dict[int, float] = {}
_openai_client = None
_model_name: str | None = None


def _admin_id() -> int | None:
    raw = os.getenv("ONBOARDING_ADMIN_ID", "1413949409917534268").strip()
    return int(raw) if raw.isdigit() else None


def load_config(*, force_reload: bool = False) -> dict:
    global _config
    if _config is not None and not force_reload:
        return _config
    with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
        _config = json.load(f)
    return _config


def reload_config() -> dict:
    return load_config(force_reload=True)


def get_purpose(purpose_id: str) -> dict | None:
    for item in load_config().get("purposes", []):
        if item.get("id") == purpose_id:
            return item
    return None


def _build_purpose_view(purpose: dict) -> discord.ui.View | None:
    url = (purpose.get("url") or "").strip()
    extra_url = (purpose.get("extra_url") or "").strip()
    need_notify_confirm = bool(purpose.get("notify_admin"))
    if not url and not extra_url and not need_notify_confirm:
        return None

    view = discord.ui.View(timeout=_VIEW_TIMEOUT)
    if url:
        view.add_item(
            discord.ui.Button(
                label=(purpose.get("button_label") or "打开链接")[:80],
                url=url,
                style=discord.ButtonStyle.link,
            )
        )
    if extra_url:
        view.add_item(
            discord.ui.Button(
                label=(purpose.get("extra_button_label") or "打开链接")[:80],
                url=extra_url,
                style=discord.ButtonStyle.link,
            )
        )
    if need_notify_confirm:
        view.add_item(OnboardingNotifyAdminButton())
    return view


async def _send_dm(client: discord.Client, admin_id: int, text: str, guild: discord.Guild | None) -> bool:
    """尝试多种方式给管理员发 DM。"""
    try:
        admin_user = await client.fetch_user(admin_id)
        await admin_user.send(text)
        return True
    except discord.Forbidden:
        print(f"⚠️ fetch_user DM 被拒（管理员 {admin_id} 可能关闭了服务器成员私信）")
    except Exception as e:
        print(f"⚠️ fetch_user DM 失败: {e}")

    if guild:
        member = guild.get_member(admin_id)
        if member is None:
            try:
                member = await guild.fetch_member(admin_id)
            except (discord.NotFound, discord.HTTPException):
                member = None
        if member:
            try:
                dm = await member.create_dm()
                await dm.send(text)
                return True
            except discord.Forbidden:
                print(f"⚠️ member.create_dm 被拒（管理员 {admin_id}）")
            except Exception as e:
                print(f"⚠️ member.create_dm 失败: {e}")

    return False


async def _notify_admin_custom(
    client: discord.Client,
    user: discord.abc.User,
    guild: discord.Guild | None,
    fallback_channel: discord.abc.Messageable | None = None,
) -> str:
    """返回 ok / cooldown / fail。"""
    admin_id = _admin_id()
    if not admin_id:
        print("⚠️ 未配置 ONBOARDING_ADMIN_ID，跳过定制通知")
        return "fail"

    now = time.time()
    last = _custom_notify_at.get(user.id, 0)
    if now - last < _CUSTOM_NOTIFY_COOLDOWN:
        print(f"ℹ️ 定制通知冷却中，跳过用户 {user.id}（24h 内已通知过）")
        return "cooldown"
    _custom_notify_at[user.id] = now

    guild_name = guild.name if guild else "未知服务器"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    display = getattr(user, "display_name", None) or user.name
    text = (
        "🔔 **新用户确认需要「免费定制」对接**\n\n"
        f"用户：{user.mention} (`{user.id}`)\n"
        f"用户名：{display}\n"
        f"服务器：**{guild_name}**\n"
        f"时间：{ts}\n\n"
        "对方已二次确认通知；请主动私聊对接~"
    )

    if await _send_dm(client, admin_id, text, guild):
        print(f"✅ 定制通知已 DM 管理员 {admin_id}（来自用户 {user.id}）")
        return "ok"

    if fallback_channel and guild:
        admin_member = guild.get_member(admin_id)
        admin_mention = admin_member.mention if admin_member else f"<@{admin_id}>"
        try:
            await fallback_channel.send(
                f"🔔 {admin_mention} **有新「免费定制」咨询**\n"
                f"用户：{user.mention} (`{user.id}`)\n"
                f"Bot 私信未能送达，请检查 Discord 隐私设置（允许服务器成员私信），请在此对接~"
            )
            print(f"⚠️ DM 失败，已在频道 fallback @ 管理员 {admin_id}")
            return "ok"
        except Exception as e:
            print(f"❌ 频道 fallback 也失败: {e}")

    _custom_notify_at.pop(user.id, None)
    print(f"❌ 定制通知完全失败 admin={admin_id} user={user.id}")
    return "fail"


async def _open_tag_browser(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        await tag_browser.open_browser(
            interaction.channel,
            interaction.user.id,
            _openai_client,
            _model_name,
        )
        await interaction.followup.send(_emo("📖 标签面板已打开，请在下方操作~", scenario="info"), ephemeral=True)
    except FileNotFoundError:
        await interaction.followup.send(
            _emo("❌ 缺少 `danbooru_category_map.json`，无法打开浏览面板。", scenario="error"),
            ephemeral=True,
        )
    except Exception as e:
        await interaction.followup.send(_emo(f"❌ 打开浏览面板失败：{e}", scenario="error"), ephemeral=True)


async def _respond_purpose(interaction: discord.Interaction, purpose_id: str) -> None:
    purpose = get_purpose(purpose_id)
    if not purpose:
        await interaction.response.send_message(_emo("❌ 选项无效，请重试。", scenario="error"), ephemeral=True)
        return

    if purpose.get("action") == "tag_browser":
        await _open_tag_browser(interaction)
        return

    guide = (purpose.get("guide") or "").strip()
    view = _build_purpose_view(purpose)

    kwargs: dict = {"ephemeral": True}
    if view is not None:
        kwargs["view"] = view
    await interaction.response.send_message(_emo(guide, scenario="help"), **kwargs)


def _button_style(purpose: dict) -> discord.ButtonStyle:
    purpose_id = purpose.get("id", "")
    if purpose.get("featured"):
        return discord.ButtonStyle.success
    if purpose_id == "overview":
        return discord.ButtonStyle.secondary
    return discord.ButtonStyle.primary


class OnboardingNotifyAdminButton(discord.ui.Button):
    """弹窗内二次确认后，才通知管理员。"""

    def __init__(self):
        super().__init__(
            custom_id="onboarding:notify_admin",
            label="通知管理员对接",
            emoji="🔔",
            style=discord.ButtonStyle.danger,
        )

    async def callback(self, interaction: discord.Interaction):
        status = await _notify_admin_custom(
            interaction.client,
            interaction.user,
            interaction.guild,
            fallback_channel=interaction.channel,
        )
        if status == "ok":
            await interaction.response.send_message(
                _emo(
                    "✅ 已通知管理员。请稍等对方私聊你；"
                    "也可先在成员列表 **主动私聊管理员** 说明需求。",
                    scenario="info",
                ),
                ephemeral=True,
            )
        elif status == "cooldown":
            await interaction.response.send_message(
                _emo(
                    "ℹ️ 24 小时内已通知过管理员啦。"
                    "请直接私聊管理员，或稍后再试。",
                    scenario="info",
                ),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                _emo(
                    "⚠️ 通知失败：管理员暂时收不到 Bot 私信。"
                    "请直接在成员列表 **私聊管理员**，或在此频道 @管理员 说明需求。",
                    scenario="info",
                ),
                ephemeral=True,
            )


class OnboardingNotifyAdminView(discord.ui.View):
    """仅用于注册持久化「通知管理员」按钮。"""

    def __init__(self):
        super().__init__(timeout=_VIEW_TIMEOUT)
        self.add_item(OnboardingNotifyAdminButton())


class OnboardingPurposeButton(discord.ui.Button):
    def __init__(self, purpose: dict, *, row: int):
        purpose_id = purpose["id"]
        label = (purpose.get("label") or purpose_id)[:80]
        emoji = purpose.get("emoji")
        super().__init__(
            custom_id=f"onboarding:btn:{purpose_id}",
            label=label,
            emoji=emoji,
            style=_button_style(purpose),
            row=row,
        )
        self._purpose_id = purpose_id

    async def callback(self, interaction: discord.Interaction):
        await _respond_purpose(interaction, self._purpose_id)


class OnboardingWelcomeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=_VIEW_TIMEOUT)
        purposes = load_config().get("purposes", [])
        for index, purpose in enumerate(purposes):
            row = index // _BUTTONS_PER_ROW
            self.add_item(OnboardingPurposeButton(purpose, row=row))


def register_views(client: discord.Client, *, openai_client=None, model_name: str | None = None):
    global _openai_client, _model_name
    _openai_client = openai_client
    _model_name = model_name
    client.add_view(OnboardingWelcomeView())
    client.add_view(OnboardingNotifyAdminView())


def _purpose_panel_text() -> str:
    lines = ["**📋 功能面板 — 点下方按钮查看指引：**", ""]
    for item in load_config().get("purposes", []):
        emoji = item.get("emoji") or "•"
        label = item.get("label") or item.get("id", "")
        desc = (item.get("description") or "").strip()
        featured = " ⭐**【主推】**" if item.get("featured") else ""
        lines.append(f"{emoji} **{label}**{featured}" + (f" — {desc}" if desc else ""))
    return "\n".join(lines)


def _picker_body(bot_name: str) -> str:
    master = os.getenv("MASTER_NAME", "璐瑶").strip() or "璐瑶"
    return (
        f"我是 **{bot_name}** 🐺 — {master} 家的哈士奇，会写 prompt、会反推、会查 Danbooru 标签、"
        f"会看图锐评/彩虹屁，还能签到换视频码。\n"
        f"Web 端可 **绘画 / 写卡 / 查提示词**，也能指引你 **本地装酒馆玩角色卡**（不想部署有云酒馆）。\n\n"
        f"{_purpose_panel_text()}\n\n"
        "👇 **直接点按钮**，本哈给你专属指引（仅你可见）\n"
        "🟢 **绿色按钮** = 主推（绘画，写卡，查提示词 → ComfyUI Web）"
    )


def build_welcome_content(member: discord.Member, bot_name: str) -> str:
    return f"🎉 欢迎 {member.mention} 加入！\n\n{_picker_body(bot_name)}"


def build_picker_content(user: discord.abc.User, bot_name: str) -> str:
    mention = getattr(user, "mention", None) or f"**{getattr(user, 'display_name', user.name)}**"
    return f"👋 {mention}，本哈再给你指路一次！\n\n{_picker_body(bot_name)}"


async def send_purpose_picker(
    channel: discord.abc.Messageable,
    user: discord.abc.User,
    bot_name: str,
) -> None:
    reload_config()
    await channel.send(_emo(build_picker_content(user, bot_name), scenario="welcome"), view=OnboardingWelcomeView())


async def send_member_welcome(
    member: discord.Member,
    channel: discord.abc.Messageable,
    bot_name: str,
) -> None:
    reload_config()
    await channel.send(_emo(build_welcome_content(member, bot_name), scenario="welcome"), view=OnboardingWelcomeView())
