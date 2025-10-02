import os
import json
import logging
import logging.handlers
import base64
import time
import re
import random
import httpx 
import sys
import math
import concurrent.futures 
import warnings # 用于忽略已知的警告

from datetime import datetime, timedelta, timezone
from functools import wraps

# 忽略特定的 DeprecationWarning
warnings.filterwarnings("ignore", category=DeprecationWarning) 

# --- v13 兼容性依赖 ---
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackContext,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    Filters,
)

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- 全局变量和常量 ---
CONFIG_FILE = 'config.json'
HISTORY_FILE = 'history.json'
LOG_FILE = 'fofa_bot.log'
MAX_HISTORY_SIZE = 50
DEFAULT_TIMEOUT_SEC = 30 # 设置默认的 httpx 请求超时时间
DEFAULT_MAX_THREADS = 5 # 默认并发线程数
FOFA_PAGE_SIZE = 100 # FOFA API 每页最大结果数

# --- 全局停止标志 (用于中断耗时操作) ---
stop_flag = False

# --- Telegram Bot Token (已设置) ---
BOT_TOKEN = "8325002891:AAHzYRlWn2Tq_lMyzbfBbkhPC-vX8LS6kw"

# --- 日志配置 ---
logger = logging.getLogger('fofa_bot')
logger.setLevel(logging.INFO)
if not logger.handlers:
    # 简易控制台输出配置
    stream_handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)


# --- 配置文件/历史记录操作 (关键修复区域) ---
def load_config():
    """加载配置，兼容旧的 'apis' 列表格式，并返回标准格式。"""
    default_config = {
        "api_keys": [], # 格式: [{"email": "user@example.com", "key": "xxxxxxxxxxxxxxxx"}, ...]
        "user_agent": "FofaBot/1.0 (httpx/Concurrent)",
        "proxy": None,
        "owner_id": None,
        "max_threads": DEFAULT_MAX_THREADS
    }
    
    if not os.path.exists(CONFIG_FILE):
        return default_config
        
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
            
            # --- 核心修复逻辑：兼容旧的 'apis' 格式 ---
            if "apis" in config and isinstance(config["apis"], list):
                # 如果发现旧的 'apis' 字段，将其转换为新的 'api_keys' 格式
                # 注意：由于旧格式缺少邮箱，我们必须使用占位符
                new_api_keys = []
                for i, key in enumerate(config["apis"]):
                    new_api_keys.append({"email": f"placeholder{i}@fofa.api", "key": key})
                config["api_keys"] = new_api_keys
                # 移除旧的 'apis' 字段
                del config["apis"] 
                # 提示用户配置已转换
                logger.warning(f"检测到旧版配置，已将 {len(new_api_keys)} 个Key转换为新的 'api_keys' 格式。请在设置中更新邮箱。")
                
            # 确保新配置项存在
            if 'max_threads' not in config:
                config['max_threads'] = DEFAULT_MAX_THREADS
                
            # 确保 api_keys 列表存在
            if 'api_keys' not in config:
                 config['api_keys'] = []
                 
            return config
            
    except Exception as e:
        logger.error(f"加载或解析 {CONFIG_FILE} 失败: {e}. 使用默认配置。")
        return default_config

def save_config(config_data):
    """保存配置。"""
    # 在保存时，只保留脚本内部使用的 'api_keys' 格式，不写回 'apis'
    # 这样可以逐步淘汰旧格式
    if 'apis' in config_data:
        del config_data['apis']
        
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config_data, f, indent=4, ensure_ascii=False)


def load_history():
    """加载查询历史记录。"""
    if not os.path.exists(HISTORY_FILE):
        return []
    with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_history(history_data):
    """保存查询历史记录，限制最大数量。"""
    if len(history_data) > MAX_HISTORY_SIZE:
        history_data = history_data[-MAX_HISTORY_SIZE:]
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history_data, f, indent=4, ensure_ascii=False)


# --- FOFA 接口客户端类 (封装请求逻辑) ---
class FofaAPIClient:
    def __init__(self, config):
        self.config = config
        self.user_agent = config.get("user_agent", "FofaBot/1.0 (httpx/Concurrent)")
        self.default_proxy = config.get("proxy")

    def get_available_keys(self):
        """获取所有可用的 FOFA API Key 列表。"""
        return self.config.get("api_keys", [])

    def _make_request_sync(self, url, method='GET', data=None, proxy=None, timeout=DEFAULT_TIMEOUT_SEC):
        """同步执行 HTTP 请求 (使用 httpx)。"""
        proxies = {"all": proxy} if proxy else None
        
        try:
            with httpx.Client(proxies=proxies, verify=False, timeout=timeout) as client:
                response = client.request(
                    method,
                    url,
                    data=data,
                    headers={'User-Agent': self.user_agent},
                )
            return response.status_code, response.text
            
        except httpx.TimeoutException:
            return 408, f"请求超时: FOFA 服务器响应超过 {timeout} 秒"
        except httpx.RequestError as e:
            return 500, f"网络或连接错误: {e.__class__.__name__}: {e}"
        except Exception as e:
            return 500, f"发生未知错误: {e.__class__.__name__}: {e}"

    def _build_fofa_url(self, query_str, email, key, size, fields, page=1):
        """构造完整的 FOFA API URL (支持分页)。"""
        base_url = "https://fofa.info/api/v1/search/all"
        query_hash = base64.b64encode(query_str.encode()).decode()
        
        url = (
            f"{base_url}?qbase64={query_hash}"
            f"&email={email}&key={key}"
            f"&size={size}&fields={fields}&page={page}"
        )
        return url

    def execute_query_with_key_fallback(self, query_details, context: CallbackContext):
        """
        使用 Key 回退机制执行 Page 1 查询，并返回成功 Key 的信息和总结果数。
        """
        global stop_flag
        
        available_keys = self.get_available_keys() 
        if not available_keys:
             return None, 0, None, "NO_KEYS"

        random.shuffle(available_keys) 
        chat_id = context.effective_chat.id
        
        for i, api_key_info in enumerate(available_keys):
            if stop_flag:
                logger.info(f"Chat {chat_id}: 任务因 stop_flag 启用而取消。")
                context.bot.send_message(
                    chat_id=chat_id, 
                    text="✅ 任务已成功停止。", 
                    parse_mode=ParseMode.MARKDOWN
                )
                stop_flag = False
                return None, 0, None, "STOPPED"

            # 使用反引号包裹 email，避免 Markdown 渲染错误
            email_display = f"`{api_key_info.get('email', 'N/A')}`"
            query_str = query_details.get('query', '')
            size = query_details.get('size', 100)
            fields = query_details.get('fields', 'host,ip,port')
            
            url = self._build_fofa_url(query_str, api_key_info.get('email'), api_key_info.get('key'), size, fields, page=1)
            
            context.bot.send_message(
                chat_id=chat_id, 
                text=f"🔑 正在使用 Key ({email_display} / 尝试 {i+1}/{len(available_keys)}) 尝试 Page 1 查询...",
                parse_mode=ParseMode.MARKDOWN
            )
            
            status_code, content = self._make_request_sync(
                url=url, 
                proxy=self.default_proxy, 
                timeout=DEFAULT_TIMEOUT_SEC
            )
            
            if status_code == 200:
                try:
                    result = json.loads(content)
                    if result.get('error'):
                        errmsg = result.get('errmsg', 'API 错误')
                        if 'balance is 0' in errmsg:
                            logger.error(f"Key {api_key_info.get('email')} 余额不足。")
                            context.bot.send_message(chat_id=chat_id, text="💰 API Key 余额不足，尝试下一个 Key...")
                            continue
                        logger.error(f"Key {api_key_info.get('email')} API 错误: {errmsg}")
                        context.bot.send_message(chat_id=chat_id, text=f"❌ Key API 错误: {errmsg[:20]}... 尝试下一个 Key...")
                        continue
                        
                    return result, result.get('size', 0), api_key_info, "SUCCESS"
                except json.JSONDecodeError:
                    return None, 0, None, "INVALID_JSON"
            
            context.bot.send_message(chat_id=chat_id, text=f"❌ 请求失败 (Code: {status_code})，尝试下一个 Key...")
                
        return None, 0, None, "FAILED_ALL"

    def _fetch_single_page(self, query_details, key_info, page_num):
        """用于线程池的单个页面抓取函数，包含 stop_flag 检查。"""
        global stop_flag
        if stop_flag:
            # 允许线程池优雅退出
            raise concurrent.futures.CancelledError("Thread stopped by user flag")
        
        email = key_info.get('email')
        key = key_info.get('key')
        query_str = query_details.get('query')
        size = query_details.get('size')
        fields = query_details.get('fields')

        # 检查是否已达到 FOFA 允许的最大查询页数 (假设 FOFA 允许 10000 条，即 100 页)
        if page_num > math.ceil(min(size, 10000) / FOFA_PAGE_SIZE):
             return []
             
        url = self._build_fofa_url(query_str, email, key, size, fields, page=page_num)
        
        status_code, content = self._make_request_sync(url=url, proxy=self.default_proxy)
        
        if status_code == 200:
            try:
                result = json.loads(content)
                if result.get('error'):
                    logger.error(f"Page {page_num} API 错误: {result.get('errmsg')}")
                    return []
                return result.get('results', [])
            except json.JSONDecodeError:
                logger.error(f"Page {page_num} 返回无效 JSON: {content[:100]}")
                return []
        else:
            logger.error(f"Page {page_num} 请求失败, Code: {status_code}")
            return []

    def fetch_all_pages_concurrently(self, context: CallbackContext, query_details, total_size, key_info):
        """并发请求除 Page 1 外的所有页面。"""
        global stop_flag
        chat_id = context.effective_chat.id
        
        size_limit = min(total_size, query_details.get('size', 10000), 10000)
        total_pages = math.ceil(size_limit / FOFA_PAGE_SIZE)
        pages_to_fetch = list(range(2, total_pages + 1))
        
        if not pages_to_fetch:
            return []
            
        max_workers = self.config.get('max_threads', DEFAULT_MAX_THREADS)
        
        context.bot.send_message(
            chat_id=chat_id,
            text=f"⚙️ 开始多线程抓取剩余 {len(pages_to_fetch)} 页，使用 **{max_workers}** 个并发线程...",
            parse_mode=ParseMode.MARKDOWN
        )

        all_results = []
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_page = {
                executor.submit(self._fetch_single_page, query_details, key_info, page): page 
                for page in pages_to_fetch
            }

            for future in concurrent.futures.as_completed(future_to_page):
                page = future_to_page[future]
                
                if stop_flag:
                    # 在 future 遍历中发现 stop_flag，立即停止线程池
                    executor.shutdown(wait=False, cancel_futures=True)
                    logger.info(f"Chat {chat_id}: 线程池被 stop_flag 中断。")
                    return all_results

                try:
                    page_results = future.result()
                    if page_results:
                        all_results.extend(page_results)
                        context.bot.send_message(
                             chat_id=chat_id,
                             text=f"✅ Page {page} 抓取成功，当前已收集 {len(all_results)} 条结果。"
                        )
                    else:
                        context.bot.send_message(chat_id=chat_id, text=f"⚠️ Page {page} 抓取失败或结果为空。")
                        
                except concurrent.futures.CancelledError:
                    # 这是由 stop_flag 触发的异常，静默处理
                    pass
                except Exception as exc:
                    logger.error(f"Page {page} 抓取时发生异常: {exc}")
                    context.bot.send_message(chat_id=chat_id, text=f"❌ Page {page} 抓取过程中发生错误: {exc.__class__.__name__}")

        return all_results


# --- Telegram Bot 状态常量 ---
STATE_KKFOFA_QUERY = 1
STATE_KKFOFA_MODE = 2
STATE_SETTINGS_MAIN = 3
STATE_ADD_KEY_EMAIL = 4
STATE_ADD_KEY_KEY = 5
STATE_SET_THREADS = 6 


# --- 统一的停止指令处理函数 ---
def unified_stop_handler(update: Update, context: CallbackContext) -> int:
    """处理 /stop 指令，设置全局停止标志并提供即时用户反馈。"""
    global stop_flag
    
    if update.callback_query:
        update.callback_query.answer("停止指令已收到。")
        update.callback_query.edit_message_text(
            "🛑 **收到停止指令**。任务将在执行完当前请求或线程后尽快停止。请稍候...",
            parse_mode=ParseMode.MARKDOWN
        )
    elif update.message:
        update.message.reply_text(
            "🛑 **收到停止指令**。当前任务（如 FOFA 查询）将在执行完当前请求或线程后尽快停止。请稍候...", 
            parse_mode=ParseMode.MARKDOWN
        )
    
    stop_flag = True
    return ConversationHandler.END


# --- FOFA 查询对话处理 ---
def kkfofa_query_command(update: Update, context: CallbackContext) -> int:
    """进入 FOFA 查询流程，提示用户输入查询语句。"""
    config = load_config()
    if not config.get("api_keys"):
        update.message.reply_text("❌ 您尚未配置 FOFA API Key。请使用 /settings 配置。")
        return ConversationHandler.END
        
    update.message.reply_text("请输入 **FOFA 查询语句** (例如：`title=\"xxx\" && country=\"CN\"`)：", parse_mode=ParseMode.MARKDOWN)
    return STATE_KKFOFA_QUERY

def process_fofa_query(update: Update, context: CallbackContext) -> int:
    """接收查询语句并启动 FOFA 查询任务。"""
    fofa_query = update.message.text
    
    # 存储查询参数到 context.user_data
    context.user_data['fofa_query_str'] = fofa_query
    context.user_data['fofa_query_size'] = 1000 
    context.user_data['fofa_query_fields'] = 'host,ip,port,title,country'
    
    # 提示用户选择模式
    keyboard = [
        [
            InlineKeyboardButton("默认模式 (1000条)", callback_data="mode_default"),
            InlineKeyboardButton("精简模式 (500条, host,ip)", callback_data="mode_simple")
        ],
        [InlineKeyboardButton("取消", callback_data="mode_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    update.message.reply_text(
        f"✅ 已接收查询语句：`{fofa_query}`\n\n请选择查询模式：", 
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    return STATE_KKFOFA_MODE

def query_mode_callback(update: Update, context: CallbackContext) -> int:
    """根据用户选择的模式设置查询参数并执行查询。"""
    query = update.callback_query
    query.answer()
    
    mode = query.data.split('_')[1]
    
    if mode == 'cancel':
        query.edit_message_text("查询已取消。")
        return ConversationHandler.END

    if mode == 'simple':
        context.user_data['fofa_query_size'] = 500
        context.user_data['fofa_query_fields'] = 'host,ip'
    
    query_str = context.user_data.get('fofa_query_str')
    query_details = {
        'query': query_str,
        'size': context.user_data.get('fofa_query_size'),
        'fields': context.user_data.get('fofa_query_fields'),
    }

    query.edit_message_text(f"🚀 开始执行 FOFA Page 1 查询...\n查询语句：`{query_str}`", parse_mode=ParseMode.MARKDOWN)

    # 实例化客户端
    config = load_config()
    client = FofaAPIClient(config)
    
    # 1. 执行 Page 1 查询和 Key 回退
    page1_result, total_size, key_info, status = client.execute_query_with_key_fallback(query_details, context)
    
    if status != "SUCCESS":
        return ConversationHandler.END
        
    # 2. Page 1 成功，开始多线程抓取
    total_results = page1_result.get('results', [])
    
    if total_size > FOFA_PAGE_SIZE and total_size != len(total_results):
        remaining_results = client.fetch_all_pages_concurrently(context, query_details, total_size, key_info)
    else:
        remaining_results = []
        context.bot.send_message(
            chat_id=context.effective_chat.id, 
            text=f"✅ 总结果数 {total_size}，只有 1 页数据或已抓取完毕，无需多线程。",
        )
        
    # 3. 合并所有结果
    all_results = total_results + remaining_results
    final_count = len(all_results)
    
    # 4. 最终结果处理
    if final_count > 0:
        history = load_history()
        history.append({
            'user_id': context.effective_user.id,
            'username': context.effective_user.username,
            'query': query_str,
            'size': final_count,
            'time': datetime.now(timezone.utc).isoformat()
        })
        save_history(history)
        
        # 结果格式化，避免 Key 中的特殊字符引起渲染错误
        first_results_str = "\n".join([f"| {r[0]:<40} | {r[1]:<15} |" for r in all_results[:5]])
        output = (
            f"🎉 **任务完成！** 抓取结果 **{final_count}** 条 (目标 {total_size} 条)。\n\n"
            f"**查询语句:** `{query_str}`\n"
            f"**模式/字段:** {query_details['fields']}\n"
            f"**--- 示例结果 (前 5 条) ---**\n"
            f"```\n| Host (部分)                           | IP/Port         |\n"
            f"|---------------------------------------|-----------------|\n"
            + first_results_str + 
            "\n```"
        )
    else:
        output = f"⚠️ **任务完成**：未发现任何有效结果。\n查询语句：`{query_str}`"
        
    context.bot.send_message(
        chat_id=context.effective_chat.id, 
        text=output,
        parse_mode=ParseMode.MARKDOWN
    )
    
    global stop_flag 
    stop_flag = False
        
    context.user_data.pop('fofa_query_str', None)
    context.user_data.pop('fofa_query_size', None)
    context.user_data.pop('fofa_query_fields', None)
        
    return ConversationHandler.END

# --- 设置对话处理 ---
def settings_command(update: Update, context: CallbackContext) -> int:
    """进入设置主菜单。"""
    config = load_config()
    key_count = len(config.get("api_keys", []))
    max_threads = config.get("max_threads", DEFAULT_MAX_THREADS)
    
    keyboard = [
        [InlineKeyboardButton(f"🔑 管理 API Key ({key_count} 个)", callback_data="set_keys")],
        [InlineKeyboardButton(f"🔗 并发线程数 ({max_threads})", callback_data="set_threads")],
        [InlineKeyboardButton("🌐 配置代理", callback_data="set_proxy")],
        [InlineKeyboardButton("🔙 返回", callback_data="set_exit")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    update.message.reply_text("⚙️ **设置与管理**\n请选择操作：", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    return STATE_SETTINGS_MAIN

def settings_callback(update: Update, context: CallbackContext) -> int:
    """处理设置菜单回调。"""
    query = update.callback_query
    query.answer()
    
    action = query.data.split('_')[1]
    
    if action == 'threads':
        config = load_config()
        current_threads = config.get("max_threads", DEFAULT_MAX_THREADS)
        query.edit_message_text(f"🔗 **设置并发线程数**\n当前值：`{current_threads}`。请输入新的线程数（建议 1 - 20 之间）：", parse_mode=ParseMode.MARKDOWN)
        return STATE_SET_THREADS
    
    elif action == 'keys':
        # 这里的消息修复了 Markdown 潜在的渲染问题
        query.edit_message_text("🔑 **API Key 管理**\n请输入 **FOFA 邮箱** 来添加或管理 Key：")
        return STATE_ADD_KEY_EMAIL
    
    elif action == 'proxy':
        query.edit_message_text("🌐 **代理设置**\n请输入代理地址 (如：`http://user:pass@host:port`)，输入 `None` 清除：")
        context.user_data['settings_mode'] = 'proxy'
        return STATE_ADD_KEY_EMAIL # 复用状态
        
    elif action == 'exit':
        query.edit_message_text("✅ 已退出设置菜单。")
        return ConversationHandler.END
        
    return STATE_SETTINGS_MAIN

def set_threads_handler(update: Update, context: CallbackContext) -> int:
    """接收用户输入的线程数。"""
    try:
        new_threads = int(update.message.text.strip())
        if new_threads < 1 or new_threads > 50:
            raise ValueError("线程数不在合理范围")
            
        config = load_config()
        config['max_threads'] = new_threads
        save_config(config)
        
        update.message.reply_text(f"✅ **并发线程数已成功设置为**：`{new_threads}`", parse_mode=ParseMode.MARKDOWN)
        
    except ValueError:
        update.message.reply_text("❌ 输入无效。请输一个介于 1 到 50 之间的整数作为线程数。")
        return STATE_SET_THREADS
        
    return settings_command(update, context) 


# --- 主函数和 Bot 启动 ---
def main():
    """主函数，负责启动 Bot。"""
    if BOT_TOKEN == 'YOUR_TELEGRAM_BOT_TOKEN':
        logger.error("BOT_TOKEN 未设置。请检查代码中的 BOT_TOKEN 变量。")
        sys.exit(1)
        
    # 确保 config.json 存在且格式正确
    config = load_config()
    save_config(config) 
        
    try:
        # 这里的 updater 初始化是 NameError 问题的关键，必须确保成功
        updater = Updater(BOT_TOKEN, use_context=True)
        dispatcher = updater.dispatcher
        
        # 1. FOFA 查询对话
        kkfofa_conv = ConversationHandler(
            entry_points=[CommandHandler("kkfofa", kkfofa_query_command)],
            states={
                STATE_KKFOFA_QUERY: [MessageHandler(Filters.text & ~Filters.command, process_fofa_query)],
                STATE_KKFOFA_MODE: [CallbackQueryHandler(query_mode_callback, pattern=r"^mode_")],
            },
            fallbacks=[unified_stop_handler] 
        )
        
        # 2. 设置对话
        settings_conv = ConversationHandler(
            entry_points=[CommandHandler("settings", settings_command)],
            states={
                STATE_SETTINGS_MAIN: [CallbackQueryHandler(settings_callback, pattern=r"^set_")],
                STATE_ADD_KEY_EMAIL: [MessageHandler(Filters.text & ~Filters.command, lambda update, context: update.message.reply_text("请输入您的 FOFA Key:"), pass_user_data=True)], # 简化Key/Proxy输入
                STATE_SET_THREADS: [MessageHandler(Filters.text & ~Filters.command, set_threads_handler)],
            },
            fallbacks=[unified_stop_handler]
        )

        # 3. 注册所有 Handler
        dispatcher.add_handler(CommandHandler("start", start_command))
        # ... (注册其他 handlers) ...
        dispatcher.add_handler(CommandHandler("stop", unified_stop_handler))
        dispatcher.add_handler(settings_conv)
        dispatcher.add_handler(kkfofa_conv)

        updater.bot.set_my_commands([
            BotCommand("kkfofa", "🔍 资产搜索"),
            BotCommand("settings", "⚙️ 设置与管理"),
            BotCommand("stop", "🛑 停止/取消"),
            BotCommand("help", "❓ 帮助手册"),
            BotCommand("history", "🕰️ 查询历史"),
        ])

        logger.info("🚀 机器人已启动并开始轮询...")
        updater.start_polling()
        # 正常退出
        updater.idle()
        logger.info("机器人已安全关闭。")

    except Exception as e:
        logger.error(f"机器人启动失败: {e}")
        # 在这里不调用 updater.idle() 或其他依赖 updater 的函数，从而避免 NameError

if __name__ == '__main__':
    # 在最外层确保配置的初始化和转换
    try:
        if not os.path.exists(CONFIG_FILE):
            save_config(load_config())
        else:
            # 运行一次加载和保存，以触发配置转换
            config = load_config()
            save_config(config)
    except Exception as e:
        print(f"配置文件预处理失败: {e}")
        
    main()
