import asyncio
import shutil

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api import logger

from .config import PluginConfig


class CacheCleaner:
    """
    每天固定时间自动清理插件缓存目录的调度器封装。
    """

    JOBNAME = "CacheCleaner"

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.scheduler = AsyncIOScheduler(timezone=self.cfg.timezone)
        self.scheduler.start()

        self.register_task()

        logger.info(f"{self.JOBNAME} 已启动，任务周期：{self.cfg.clean_cron}")

    def register_task(self):
        try:
            self.trigger = CronTrigger.from_crontab(self.cfg.clean_cron)
            self.scheduler.add_job(
                func=self._clean_plugin_cache,
                trigger=self.trigger,
                name=f"{self.JOBNAME}_scheduler",
                max_instances=1,
            )
        except Exception as e:
            logger.error(f"[{self.JOBNAME}] Cron 格式错误：{e}")

    async def _clean_plugin_cache(self) -> None:
        """清理缓存内容，保留缓存根目录及其媒体读取 ACL。"""
        loop = asyncio.get_running_loop()

        def clear_contents():
            self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)
            for child in self.cfg.cache_dir.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink(missing_ok=True)

        try:
            await loop.run_in_executor(None, clear_contents)
            logger.info("Cache contents cleaned; root directory and ACL preserved.")
        except Exception:
            logger.exception("Error while cleaning cache directory.")

    async def stop(self):
        self.scheduler.remove_all_jobs()
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        logger.info(f"[{self.JOBNAME}] 已停止")
