"""astrbot_plugin_mail_memory

Poll a mailbox (IMAP) read-only (headers only by default), let an LLM decide whether
each new mail matters to you, and push the relevant ones to your chat.
"""

from __future__ import annotations

import asyncio
import os

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .core import LLMError, redact
from .service import MailService

PLUGIN_NAME = "astrbot_plugin_mail_memory"
FIRST_DELAY_SECONDS = 5


class MailNotifyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.service: MailService | None = None
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------- lifecycle
    async def initialize(self) -> None:
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.service = MailService(
            get_config=lambda: self.config,
            state_path=os.path.join(str(data_dir), "state.json"),
            send=self._send,
            write_history=self._write_history,
            provider_chat=self._call_provider,
            default_timezone=self._astrbot_timezone,
            persona_lookup=self._persona_prompt,
            log=logger,
        )
        # Cancel any leftover task from a previous load of this instance.
        await self._stop_task()
        self._task = asyncio.create_task(self._loop(), name=f"{PLUGIN_NAME}-loop")
        logger.info("邮件提醒插件已启动")

    async def terminate(self) -> None:
        await self._stop_task()
        logger.info("邮件提醒插件已停止")

    async def _stop_task(self) -> None:
        task, self._task = self._task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    async def _loop(self) -> None:
        assert self.service is not None
        try:
            await asyncio.sleep(FIRST_DELAY_SECONDS)
            while True:
                try:
                    if self.service.should_run_scheduled():
                        await self.service.run_round()
                    if self.service.superseded:
                        return  # a newer instance took over; this one just exits
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # never let the loop die
                    try:
                        secrets = self.service.settings().secrets()
                    except Exception:
                        secrets = []
                    logger.error(f"邮件检查出现未预期的错误：{type(e).__name__}: {redact(str(e), secrets)}")
                await asyncio.sleep(self.service.settings().check_interval_minutes * 60)
        except asyncio.CancelledError:
            raise

    # --------------------------------------------------- platform integration
    async def _persona_prompt(self, umo: str) -> str:
        """System prompt of the persona currently in force for this chat ("" if none)."""
        cm = self.context.conversation_manager
        persona_id = None
        cid = await cm.get_curr_conversation_id(umo)
        if cid:
            conv = await cm.get_conversation(umo, cid)
            persona_id = getattr(conv, "persona_id", None)
        _, persona, _, _ = await self.context.persona_manager.resolve_selected_persona(
            umo=umo,
            conversation_persona_id=persona_id,
            platform_name=umo.split(":")[0],
            provider_settings={},
        )
        return str((persona or {}).get("prompt", "") or "")

    def _astrbot_timezone(self) -> str:
        """Follow AstrBot's own global time zone instead of a separate menu item."""
        return str(self.context.get_config().get("timezone") or "")

    async def _call_provider(self, provider_id: str, system: str, user: str) -> str:
        """Classify through a model provider the user already configured in AstrBot."""
        prov = self.context.get_provider_by_id(provider_id)
        if prov is None or not hasattr(prov, "text_chat"):
            raise LLMError(f"找不到可用的模型提供商：{provider_id!r}")
        resp = await prov.text_chat(prompt=user, system_prompt=system)
        text = (getattr(resp, "completion_text", "") or "").strip()
        if getattr(resp, "role", "") == "err":
            raise LLMError(f"模型提供商返回错误：{text[:120]}")
        return text

    async def _send(self, umo: str, text: str) -> bool:
        return bool(await self.context.send_message(umo, MessageChain().message(text)))

    async def _write_history(self, umo: str, user_text: str, assistant_text: str) -> None:
        cm = self.context.conversation_manager
        cid = await cm.get_curr_conversation_id(umo)
        if not cid:
            cid = await cm.new_conversation(umo)
        await cm.add_message_pair(
            cid,
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": assistant_text},
        )

    # --------------------------------------------------------------- commands
    @filter.command_group("mail")
    def mail(self):
        """邮件提醒"""

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mail.command("check")
    async def mail_check(self, event: AstrMessageEvent):
        """立即检查一次邮箱"""
        if not event.is_admin():
            yield event.plain_result("需要管理员权限。")
            return
        report = await self.service.run_round(manual=True)
        yield event.plain_result(report.text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mail.command("status")
    async def mail_status(self, event: AstrMessageEvent):
        """查看运行状态"""
        if not event.is_admin():
            yield event.plain_result("需要管理员权限。")
            return
        yield event.plain_result(self.service.status_text())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mail.command("test")
    async def mail_test(self, event: AstrMessageEvent, n: int = 5):
        """对最近 N 封邮件试判断，不发通知"""
        if not event.is_admin():
            yield event.plain_result("需要管理员权限。")
            return
        n = max(1, min(int(n), 20))
        yield event.plain_result(await self.service.dry_run(n))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mail.command("selftest")
    async def mail_selftest(self, event: AstrMessageEvent):
        """给自己发一封测试邮件，检查 邮箱 → 机器人 → 聊天 是否通畅"""
        if not event.is_admin():
            yield event.plain_result("需要管理员权限。")
            return
        yield event.plain_result("开始自测：正在给自己发一封测试邮件，最多等一分钟左右……")
        yield event.plain_result(await self.service.selftest())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mail.command("folders")
    async def mail_folders(self, event: AstrMessageEvent):
        """列出邮箱里的文件夹名（用来填“要监控的文件夹”）"""
        if not event.is_admin():
            yield event.plain_result("需要管理员权限。")
            return
        yield event.plain_result(await self.service.folders())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mail.command("pause")
    async def mail_pause(self, event: AstrMessageEvent):
        """暂停自动检查"""
        if not event.is_admin():
            yield event.plain_result("需要管理员权限。")
            return
        self.service.set_paused(True)
        yield event.plain_result("已暂停自动检查。发送 /mail resume 恢复。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mail.command("resume")
    async def mail_resume(self, event: AstrMessageEvent):
        """恢复自动检查"""
        if not event.is_admin():
            yield event.plain_result("需要管理员权限。")
            return
        self.service.set_paused(False)
        yield event.plain_result("已恢复自动检查。")
