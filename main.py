from datetime import datetime, time, timedelta
import asyncio
import json
import os
from typing import List, Optional, Dict, Any
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api.message_components import Plain
from astrbot.api import logger, AstrBotConfig
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger


@register("astrbot_plugin_group_signin", "Foolllll", "QQ群打卡插件，支持自动定时打卡和手动打卡", "0.1")
class GroupSigninPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.scheduler = AsyncIOScheduler()
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_group_signin")
        os.makedirs(self.data_dir, exist_ok=True)
        self.data_file = os.path.join(self.data_dir, "signin_data.json")
        
        # 插件配置
        self.bot_instance = None
        self.platform_name = ""
        self.admin_user_id = None  # 管理员ID，用于发送通知
        self.signin_jobs: List[Dict] = []  # 定时任务列表
        self.statistics: Dict[str, Any] = {
            "total_signs": 0,
            "success_count": 0,
            "fail_count": 0,
            "last_sign_time": None,
            "daily_stats": {}  # 每日统计 {date: {total, success, fail}}
        }
        
        # 通知配置（从配置文件读取）
        self.notify_on_success = self.config.get("notify_on_success", True)
        self.daily_report_time = self.config.get("daily_report_time", "")
        self.batch_interval = self.config.get("batch_signin_interval", 2)
        
        logger.info(f"[GroupSignin] 插件初始化开始")

    async def initialize(self):
        """初始化插件，启动调度器"""
        self._load_data()
        self._restore_jobs()
        self._setup_daily_report()
        self.scheduler.start()
        logger.info(f"[GroupSignin] 插件启动成功，已加载 {len(self.signin_jobs)} 个定时任务")

    def _load_data(self):
        """从文件加载数据"""
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.signin_jobs = data.get('signin_jobs', [])
                    self.statistics = data.get('statistics', self.statistics)
                    self.notify_on_success = data.get('notify_on_success', True)
                    self.daily_report_time = data.get('daily_report_time', "00:00:00")
                    self.admin_user_id = data.get('admin_user_id', None)
                logger.info(f"[GroupSignin] 成功加载数据文件: 定时任务 {len(self.signin_jobs)} 个")
            except Exception as e:
                logger.error(f"[GroupSignin] 加载数据失败: {e}")
                self.signin_jobs = []
        else:
            logger.info(f"[GroupSignin] 数据文件不存在，使用默认配置")

    def _save_data(self):
        """保存数据到文件"""
        try:
            data = {
                'signin_jobs': self.signin_jobs,
                'statistics': self.statistics,
                'notify_on_success': self.notify_on_success,
                'daily_report_time': self.daily_report_time,
                'admin_user_id': self.admin_user_id
            }
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info(f"[GroupSignin] 数据已保存")
        except Exception as e:
            logger.error(f"[GroupSignin] 保存数据失败: {e}")

    def _restore_jobs(self):
        """恢复所有定时任务到调度器"""
        for job in self.signin_jobs:
            try:
                self._add_scheduler_job(job)
                logger.info(f"[GroupSignin] 恢复定时任务: 群 {job['group_id']}, 时间: {job['time']}")
            except Exception as e:
                logger.error(f"[GroupSignin] 恢复定时任务失败: {e}, 任务: {job}")
    
    def _setup_daily_report(self):
        """设置每日报告定时任务"""
        if self.daily_report_time:
            try:
                time_parts = self.daily_report_time.split(":")
                hour = int(time_parts[0])
                minute = int(time_parts[1])
                
                self.scheduler.add_job(
                    self._send_daily_report,
                    CronTrigger(hour=hour, minute=minute),
                    id="daily_report",
                    replace_existing=True
                )
                logger.info(f"[GroupSignin] 每日报告已设置: {self.daily_report_time}")
            except Exception as e:
                logger.error(f"[GroupSignin] 设置每日报告失败: {e}")

    def _add_scheduler_job(self, job: Dict):
        """添加任务到调度器"""
        group_id = job['group_id']
        time_str = job['time']  # 格式: HH:MM
        
        # 解析时间
        time_parts = time_str.split(":")
        hour = int(time_parts[0])
        minute = int(time_parts[1])
        
        self.scheduler.add_job(
            self._execute_signin_job,
            CronTrigger(hour=hour, minute=minute),
            args=[job],
            id=f"signin_{group_id}",
            replace_existing=True
        )

    async def _perform_group_signin(self, group_id: str) -> dict:
        """执行群打卡操作"""
        logger.info(f"[GroupSignin] 开始为群 {group_id} 执行打卡操作")
        
        if not self.bot_instance:
            error_msg = f"Bot实例未捕获，无法执行打卡"
            logger.error(f"[GroupSignin] {error_msg}")
            return {"success": False, "message": error_msg, "group_id": group_id}
        
        try:
            # 尝试方法1: send_group_sign
            try:
                logger.info(f"[GroupSignin] 尝试使用 send_group_sign API 为群 {group_id} 打卡")
                result = await self.bot_instance.api.call_action(
                    'send_group_sign',
                    group_id=str(group_id)
                )
                logger.info(f"[GroupSignin] 群 {group_id} 使用 send_group_sign 打卡成功，返回: {result}")
                return {"success": True, "message": "打卡成功 (send_group_sign)", "result": result, "group_id": group_id}
            except Exception as e1:
                logger.warning(f"[GroupSignin] send_group_sign API 调用失败: {e1}，尝试 set_group_sign")
                
                # 尝试方法2: set_group_sign
                try:
                    logger.info(f"[GroupSignin] 尝试使用 set_group_sign API 为群 {group_id} 打卡")
                    result = await self.bot_instance.api.call_action(
                        'set_group_sign',
                        group_id=str(group_id)
                    )
                    logger.info(f"[GroupSignin] 群 {group_id} 使用 set_group_sign 打卡成功，返回: {result}")
                    return {"success": True, "message": "打卡成功 (set_group_sign)", "result": result, "group_id": group_id}
                except Exception as e2:
                    error_msg = f"两种API均失败 - send_group_sign: {e1}, set_group_sign: {e2}"
                    logger.error(f"[GroupSignin] 群 {group_id} 打卡失败: {error_msg}")
                    return {"success": False, "message": error_msg, "group_id": group_id}
                    
        except Exception as e:
            error_msg = f"执行打卡时发生异常: {str(e)}"
            logger.error(f"[GroupSignin] 群 {group_id} 打卡失败: {error_msg}", exc_info=True)
            return {"success": False, "message": error_msg, "group_id": group_id}

    async def _signin_multiple_groups(self, group_list: List[str], interval: float = 0) -> Dict:
        """批量打卡多个群组
        Args:
            group_list: 群组列表
            interval: 打卡间隔（秒），用于防风控
        """
        if not group_list:
            logger.warning(f"[GroupSignin] 批量打卡: 群组列表为空")
            return {"total": 0, "success": 0, "fail": 0, "results": []}
        
        logger.info(f"[GroupSignin] 开始批量打卡，共 {len(group_list)} 个群组，间隔 {interval} 秒")
        
        success_count = 0
        fail_count = 0
        detail_results = []
        
        for i, group_id in enumerate(group_list):
            try:
                result = await self._perform_group_signin(group_id)
                if result.get("success", False):
                    success_count += 1
                else:
                    fail_count += 1
                detail_results.append(result)
                
                # 添加间隔，最后一个不需要等待
                if interval > 0 and i < len(group_list) - 1:
                    await asyncio.sleep(interval)
                    
            except Exception as e:
                fail_count += 1
                logger.error(f"[GroupSignin] 打卡异常: {str(e)}", exc_info=True)
                detail_results.append({"success": False, "message": str(e), "group_id": group_id})
        
        # 更新统计信息
        today = datetime.now().strftime("%Y-%m-%d")
        if today not in self.statistics["daily_stats"]:
            self.statistics["daily_stats"][today] = {"total": 0, "success": 0, "fail": 0}
        
        self.statistics["total_signs"] += len(group_list)
        self.statistics["success_count"] += success_count
        self.statistics["fail_count"] += fail_count
        self.statistics["last_sign_time"] = datetime.now().isoformat()
        self.statistics["daily_stats"][today]["total"] += len(group_list)
        self.statistics["daily_stats"][today]["success"] += success_count
        self.statistics["daily_stats"][today]["fail"] += fail_count
        self._save_data()
        
        logger.info(f"[GroupSignin] 批量打卡完成: 总计 {len(group_list)}，成功 {success_count}，失败 {fail_count}")
        
        return {
            "total": len(group_list),
            "success": success_count,
            "fail": fail_count,
            "results": detail_results
        }

    async def _execute_signin_job(self, job: Dict):
        """执行定时打卡任务"""
        group_id = job.get('group_id')
        logger.info(f"[GroupSignin] 开始执行定时任务: 群 {group_id}")
        
        has_error = False
        error_msg = ""
        
        try:
            result = await self._perform_group_signin(group_id)
            logger.info(f"[GroupSignin] 定时任务 群 {group_id} 执行完成: {result}")
            
            # 判断是否失败
            if not result.get('success', False):
                has_error = True
                error_msg = result.get('message', '未知错误')
            
            # 发送通知
            await self._send_job_notification(group_id, result, has_error, error_msg)
            
        except Exception as e:
            has_error = True
            error_msg = str(e)
            logger.error(f"[GroupSignin] 定时任务 群 {group_id} 执行失败: {e}", exc_info=True)
            # 异常时强制通知管理员
            await self._send_error_notification(group_id, error_msg)
    
    async def _send_job_notification(self, group_id: str, result: Dict, has_error: bool, error_msg: str):
        """发送任务执行通知"""
        # 如果有错误，强制通知
        if has_error:
            message = f"⚠️ 定时打卡异常\n"
            message += f"群号: {group_id}\n"
            message += f"执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            message += f"错误: {error_msg}"
            await self._send_admin_message(message)
            return
        
        # 如果开启了成功通知，则发送
        if self.notify_on_success:
            message = f"✅ 定时打卡成功\n"
            message += f"群号: {group_id}\n"
            message += f"执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            await self._send_admin_message(message)
    
    async def _send_error_notification(self, group_id: str, error_msg: str):
        """发送错误通知（强制）"""
        message = f"❌ 定时打卡失败\n"
        message += f"群号: {group_id}\n"
        message += f"执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        message += f"错误信息: {error_msg}"
        await self._send_admin_message(message)
    
    async def _send_daily_report(self):
        """发送每日打卡报告"""
        # 仅在成功通知关闭时生效
        if self.notify_on_success:
            logger.info(f"[GroupSignin] 成功通知已开启，跳过每日报告")
            return
        
        today = datetime.now().strftime("%Y-%m-%d")
        stats = self.statistics["daily_stats"].get(today, {"total": 0, "success": 0, "fail": 0})
        
        message = f"📊 每日打卡报告 ({today})\n\n"
        message += f"总打卡次数: {stats['total']}\n"
        message += f"成功: {stats['success']}\n"
        message += f"失败: {stats['fail']}"
        
        logger.info(f"[GroupSignin] 发送每日报告: {message}")
        await self._send_admin_message(message)
    
    async def _send_admin_message(self, message: str):
        """发送消息给管理员"""
        if not self.bot_instance or not self.admin_user_id:
            logger.warning(f"[GroupSignin] 无法发送通知: Bot实例或管理员ID未设置")
            return
        
        try:
            await self.bot_instance.api.call_action(
                'send_private_msg',
                user_id=str(self.admin_user_id),
                message=message
            )
            logger.info(f"[GroupSignin] 已发送通知给管理员 {self.admin_user_id}")
        except Exception as e:
            logger.error(f"[GroupSignin] 发送通知失败: {e}")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=999)
    async def _capture_bot_instance(self, event: AstrMessageEvent):
        """捕获机器人实例和管理员ID"""
        if self.bot_instance is None and event.get_platform_name() == "aiocqhttp":
            try:
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
                if isinstance(event, AiocqhttpMessageEvent):
                    self.bot_instance = event.bot
                    self.platform_name = "aiocqhttp"
                    logger.info(f"[GroupSignin] 成功捕获 aiocqhttp 机器人实例")
            except ImportError:
                logger.warning(f"[GroupSignin] 无法导入 AiocqhttpMessageEvent")
        
        # 捕获管理员ID
        if self.admin_user_id is None and event.is_admin():
            self.admin_user_id = event.get_sender_id()
            self._save_data()
            logger.info(f"[GroupSignin] 已记录管理员ID: {self.admin_user_id}")

    @filter.command("立即打卡")
    async def instant_signin(self, event: AstrMessageEvent, group_id: str = ""):
        """立即打卡指令
        用法: /立即打卡 [群号]
        不提供群号则打卡当前群
        """
        if not event.is_admin():
            logger.warning(f"[GroupSignin] 非管理员尝试使用立即打卡: {event.get_sender_id()}")
            yield event.plain_result("❌ 此指令仅限Bot管理员使用")
            return
        
        # 如果没有提供群号，使用当前群
        if not group_id.strip():
            group_id = event.get_group_id()
            if not group_id:
                yield event.plain_result("❌ 请在群聊中使用此命令，或提供群号参数\n用法: /立即打卡 [群号]")
                return
        else:
            group_id = group_id.strip()
        
        logger.info(f"[GroupSignin] 管理员 {event.get_sender_id()} 执行立即打卡: 群 {group_id}")
        result = await self._perform_group_signin(group_id)
        
        if result["success"]:
            today = datetime.now().strftime("%Y-%m-%d")
            if today not in self.statistics["daily_stats"]:
                self.statistics["daily_stats"][today] = {"total": 0, "success": 0, "fail": 0}
            
            self.statistics["total_signs"] += 1
            self.statistics["success_count"] += 1
            self.statistics["last_sign_time"] = datetime.now().isoformat()
            self.statistics["daily_stats"][today]["total"] += 1
            self.statistics["daily_stats"][today]["success"] += 1
            self._save_data()
            
            logger.info(f"[GroupSignin] 群 {group_id} 立即打卡成功")
            yield event.plain_result(f"✅ 打卡成功\n群号: {group_id}\n方法: {result['message']}")
        else:
            today = datetime.now().strftime("%Y-%m-%d")
            if today not in self.statistics["daily_stats"]:
                self.statistics["daily_stats"][today] = {"total": 0, "success": 0, "fail": 0}
            
            self.statistics["total_signs"] += 1
            self.statistics["fail_count"] += 1
            self.statistics["daily_stats"][today]["total"] += 1
            self.statistics["daily_stats"][today]["fail"] += 1
            self._save_data()
            
            logger.error(f"[GroupSignin] 群 {group_id} 立即打卡失败: {result['message']}")
            yield event.plain_result(f"❌ 打卡失败\n群号: {group_id}\n原因: {result['message']}")

    @filter.command("一键打卡")
    async def batch_signin_all(self, event: AstrMessageEvent):
        """对所有设置了定时任务的群组执行一次打卡"""
        if not event.is_admin():
            logger.warning(f"[GroupSignin] 非管理员尝试使用一键打卡: {event.get_sender_id()}")
            yield event.plain_result("❌ 此指令仅限Bot管理员使用")
            return
        
        # 收集所有定时任务中的群组（去重）
        all_groups = set()
        for job in self.signin_jobs:
            all_groups.add(job.get('group_id'))
        
        all_groups = list(all_groups)
        
        if not all_groups:
            logger.warning(f"[GroupSignin] 一键打卡: 没有配置定时任务的群组")
            yield event.plain_result("❌ 没有配置定时任务的群组")
            return
        
        logger.info(f"[GroupSignin] 管理员 {event.get_sender_id()} 执行一键打卡，共 {len(all_groups)} 个群组")
        yield event.plain_result(f"🔄 正在为 {len(all_groups)} 个群组执行打卡，间隔{self.batch_interval}秒防风控...")
        
        result = await self._signin_multiple_groups(all_groups, interval=float(self.batch_interval))
        
        message = f"📊 一键打卡完成\n"
        message += f"总计: {result['total']}\n"
        message += f"成功: {result['success']}\n"
        message += f"失败: {result['fail']}"
        
        logger.info(f"[GroupSignin] 一键打卡完成: {result}")
        yield event.plain_result(message)

    @filter.command("添加定时打卡")
    async def add_signin_job(self, event: AstrMessageEvent):
        """添加定时打卡任务
        格式: /添加定时打卡 <时间HH:MM> <群号>
        示例: /添加定时打卡 09:00 123456789
        """
        if not event.is_admin():
            logger.warning(f"[GroupSignin] 非管理员尝试添加定时打卡: {event.get_sender_id()}")
            yield event.plain_result("❌ 此指令仅限Bot管理员使用")
            return
        
        try:
            parts = event.message_str.strip().split()
            if len(parts) < 2:
                yield event.plain_result(
                    "格式错误！\n"
                    "用法: /添加定时打卡 <时间HH:MM> <群号>\n"
                    "示例: /添加定时打卡 09:00 123456789"
                )
                return
            
            time_str = parts[0]
            group_id = parts[1]
            
            # 检查群号是否已存在定时任务
            for job in self.signin_jobs:
                if job['group_id'] == group_id:
                    yield event.plain_result(f"❌ 群 {group_id} 已设置定时打卡\n当前时间: {job['time']}")
                    return
            
            # 验证时间格式
            try:
                time_parts = time_str.split(":")
                if len(time_parts) != 2:
                    raise ValueError("时间格式错误")
                hour = int(time_parts[0])
                minute = int(time_parts[1])
                
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    raise ValueError("时间超出范围")
                
                # 标准化时间格式
                time_str = f"{hour:02d}:{minute:02d}"
                
            except Exception as e:
                logger.error(f"[GroupSignin] 时间格式验证失败: {e}")
                yield event.plain_result(f"时间格式无效: {e}\n请使用 HH:MM 格式，如 09:30")
                return
            
            # 创建任务
            job = {
                'group_id': group_id,
                'time': time_str,
                'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'created_by': event.get_sender_id()
            }
            
            # 添加到调度器
            self._add_scheduler_job(job)
            
            # 保存到列表
            self.signin_jobs.append(job)
            self._save_data()
            
            logger.info(f"[GroupSignin] 成功添加定时任务: 群 {group_id}, 时间: {time_str}")
            yield event.plain_result(
                f"✅ 定时打卡任务已添加！\n"
                f"群号: {group_id}\n"
                f"时间: 每日 {time_str}"
            )
            
        except Exception as e:
            logger.error(f"[GroupSignin] 添加定时任务失败: {e}", exc_info=True)
            yield event.plain_result(f"添加任务失败: {e}")

    @filter.command("查看定时打卡")
    async def list_signin_jobs(self, event: AstrMessageEvent):
        """查看所有定时打卡任务"""
        logger.info(f"[GroupSignin] 查看定时任务列表")
        
        if not self.signin_jobs:
            yield event.plain_result("当前没有定时打卡任务")
            return
        
        result = "📋 定时打卡任务列表:\n\n"
        for job in self.signin_jobs:
            result += f"群号: {job['group_id']}\n"
            result += f"时间: 每日 {job['time']}\n"
            result += f"创建时间: {job['created_at']}\n\n"
        
        yield event.plain_result(result)

    @filter.command("删除定时打卡")
    async def delete_signin_job(self, event: AstrMessageEvent, group_id: str = ""):
        """删除定时打卡任务
        用法: /删除定时打卡 <群号>
        """
        if not event.is_admin():
            logger.warning(f"[GroupSignin] 非管理员尝试删除定时打卡: {event.get_sender_id()}")
            yield event.plain_result("❌ 此指令仅限Bot管理员使用")
            return
        
        try:
            if not group_id.strip():
                yield event.plain_result("❌ 请提供群号\n用法: /删除定时打卡 <群号>")
                return
            
            group_id = group_id.strip()
            
            if len(self.signin_jobs) == 0:
                yield event.plain_result("❌ 当前没有定时打卡任务")
                return
            
            # 查找对应群号的任务
            job_to_delete = None
            for job in self.signin_jobs:
                if job['group_id'] == group_id:
                    job_to_delete = job
                    break
            
            if not job_to_delete:
                yield event.plain_result(f"❌ 群 {group_id} 没有设置定时打卡任务")
                return
            
            # 从调度器移除
            try:
                self.scheduler.remove_job(f"signin_{group_id}")
                logger.info(f"[GroupSignin] 从调度器移除任务: 群 {group_id}")
            except Exception as e:
                logger.warning(f"[GroupSignin] 从调度器移除任务失败: {e}")
            
            # 从列表移除
            self.signin_jobs.remove(job_to_delete)
            self._save_data()
            
            logger.info(f"[GroupSignin] 已删除定时任务: 群 {group_id}")
            yield event.plain_result(f"✅ 已删除群 {group_id} 的定时打卡任务")
            
        except Exception as e:
            logger.error(f"[GroupSignin] 删除定时任务失败: {e}")
            yield event.plain_result(f"删除任务失败: {e}")

    @filter.command("打卡统计")
    async def show_statistics(self, event: AstrMessageEvent):
        """查看打卡统计信息"""
        logger.info(f"[GroupSignin] 查看打卡统计")
        
        stats = self.statistics
        today = datetime.now().strftime("%Y-%m-%d")
        today_stats = stats.get("daily_stats", {}).get(today, {"total": 0, "success": 0, "fail": 0})
        
        message = "📊 打卡统计信息\n\n"
        message += f"【总计统计】\n"
        message += f"总打卡次数: {stats['total_signs']}\n"
        message += f"成功次数: {stats['success_count']}\n"
        message += f"失败次数: {stats['fail_count']}\n"
        
        if stats.get('last_sign_time'):
            message += f"上次打卡: {stats['last_sign_time']}\n"
        
        message += f"\n【今日统计 {today}】\n"
        message += f"今日打卡: {today_stats['total']} 次\n"
        message += f"成功: {today_stats['success']} 次\n"
        message += f"失败: {today_stats['fail']} 次\n"
        
        message += f"\n【配置信息】\n"
        message += f"定时任务: {len(self.signin_jobs)} 个\n"
        message += f"成功通知: {'开启' if self.notify_on_success else '关闭'}\n"
        message += f"每日报告: {self.daily_report_time if self.daily_report_time else '未启用'}"
        
        yield event.plain_result(message)
    

    @filter.command("打卡帮助")
    async def show_help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        help_text = """📖 群打卡插件使用帮助 v0.1

🔹 管理员打卡指令
/立即打卡 [群号] - 立即对指定群打卡（不提供群号则打卡当前群）
/一键打卡 - 对所有设置了定时任务的群组执行一次打卡（可配置间隔防风控）

🔹 定时打卡管理 (仅管理员)
/添加定时打卡 <时间HH:MM> <群号>
  示例: /添加定时打卡 09:00 123456789
  说明: 每日固定时间执行，时间格式为24小时制
  
/查看定时打卡 - 查看所有定时任务
/删除定时打卡 <群号> - 删除指定群的定时任务

🔹 统计信息
/打卡统计 - 查看打卡统计数据（包含今日和总计）

💡 特性说明:
- 定时任务每日固定时间执行，使用简单的HH:MM格式
- 每个群只能设置一个定时任务，群号作为唯一标识
- 一键打卡会自动对所有配置的群组打卡，可在配置文件设置间隔防风控
- 异常情况（打卡失败、任务错误）会强制通知管理员
- 支持两种通知模式：实时成功通知 或 每日定时报告（配置文件设置）
- 所有操作都有详细日志记录
"""
        yield event.plain_result(help_text)

    async def terminate(self):
        """插件卸载时关闭调度器"""
        if self.scheduler.running:
            self.scheduler.shutdown()
        logger.info(f"[GroupSignin] 插件已卸载")
