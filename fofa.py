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
import concurrent.futures # 引入并发执行器

from datetime import datetime, timedelta, timezone
from functools import wraps

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
TELEGRAM_BOT_UPLOAD_LIMIT = 45 * 1024 * 1024
LOCAL_CACHE_DIR = "fofa_cache"
DEFAULT_TIMEOUT_SEC = 30 # 设置默认的 httpx 请求超时时间
DEFAULT_MAX_THREADS = 5 # 默认并发线程数
FOFA_PAGE_SIZE = 100 # FOFA API 每页最大结果数

# --- 全局停止标志 (用于中断耗时操作) ---
stop_flag = False

# --- Telegram Bot Token (已设置) ---
BOT_TOKEN = "8325002891:AAHzYRlWn2Tq_lMyzbfBbkhPC-vX8LqS6kw"

# --- 初始化 ---
if not os.path.exists(LOCAL_CACHE_DIR):
    os.makedirs(LOCAL_CACHE_DIR)

# --- 日志配置 ---
logger = logging.getLogger('fofa_bot')
logger.setLevel(logging.INFO)
# (省略日志处理器配置，假设已配置)
if not logger.handlers:
    # 简易控制台输出配置
    stream_handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)


# --- 配置文件/历史记录操作 ---
def load_config():
    """加载配置，如果文件不存在则返回默认配置，新增 max_threads。"""
    default_config = {
        "api_keys": [], # 格式: [{"email": "user@example.com", "key": "xxxxxxxxxxxxxxxx"}, ...]
        "user_agent": "FofaBot/1.0 (httpx/Concurrent)",
        "proxy": None,
        "owner_id": None,
        "max_threads": DEFAULT_MAX_THREADS # 新增配置项
    }
    if not os.path.exists(CONFIG_FILE):
        return default_config
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
            # 确保新配置项存在
            if 'max_threads' not in config:
                config['max_threads'] = DEFAULT_MAX_THREADS
            return config
    except Exception:
        return default_config

def save_config(config_data):
    """保存配置。"""
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
        """
        同步执行 HTTP 请求 (使用 httpx)。
        """
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

    # =================================================================
    # V2: 仅用于首个请求 (Page 1) 和 Key 回退
    # =================================================================
    def execute_query_with_key_fallback(self, query_details, context: CallbackContext):
        """
        使用 Key 回退机制执行 Page 1 查询，并返回成功 Key 的信息和总结果数。
        
        :return: (page1_content_json, total_size, successful_key_info, status)
        """
        global stop_flag
        
        available_keys = self.get_available_keys() 
        if not available_keys:
             return None, 0, None, "NO_KEYS"

        random.shuffle(available_keys) 
        chat_id = context.effective_chat.id
        
        for i, api_key_info in enumerate(available_keys):
            # ！！！关键改进：在尝试新 Key 之前检查停止标志 ！！！
            if stop_flag:
                logger.info(f"Chat {chat_id}: 任务因 stop_flag 启用而取消。")
                context.bot.send_message(
                    chat_id=chat_id, 
                    text="✅ 任务已成功停止。", 
                    parse_mode=ParseMode.MARKDOWN
                )
                stop_flag = False
                return None, 0, None, "STOPPED"

            email = api_key_info.get('email', 'N/A')
            query_str = query_details.get('query', '')
            size = query_details.get('size', 100)
            fields = query_details.get('fields', 'host,ip,port')
            
            url = self._build_fofa_url(query_str, email, api_key_info.get('key'), size, fields, page=1)
            
            context.bot.send_message(
                chat_id=chat_id, 
                text=f"🔑 正在使用 Key ({email[:5]}...) 尝试 Page 1 (尝试 {i+1}/{len(available_keys)})...",
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
                            logger.error(f"Key {email} 余额不足。")
                            context.bot.send_message(chat_id=chat_id, text="💰 API Key 余额不足，尝试下一个 Key...")
                            continue
                        # 其他 API 级别错误
                        logger.error(f"Key {email} API 错误: {errmsg}")
                        context.bot.send_message(chat_id=chat_id, text=f"❌ Key API 错误: {errmsg[:20]}... 尝试下一个 Key...")
                        continue
                        
                    return result, result.get('size', 0), api_key_info, "SUCCESS"
                except json.JSONDecodeError:
                    return None, 0, None, "INVALID_JSON"
            
            # 其他 HTTP 错误处理...
            context.bot.send_message(chat_id=chat_id, text=f"❌ 请求失败 (Code: {status_code})，尝试下一个 Key...")
                
        return None, 0, None, "FAILED_ALL"

    # =================================================================
    # V2: 多线程并发请求剩余页面
    # =================================================================
    def _fetch_single_page(self, query_details, key_info, page_num):
        """用于线程池的单个页面抓取函数，包含 stop_flag 检查。"""
        global stop_flag
        if stop_flag:
            raise Exception("Thread stopped by user flag")
        
        email = key_info.get('email')
        key = key_info.get('key')
        query_str = query_details.get('query')
        size = query_details.get('size')
        fields = query_details.get('fields')

        # 检查是否已达到 FOFA 允许的最大查询页数 (假设 FOFA 允许 10000 条，即 100 页)
        if page_num > math.ceil(min(size, 10000) / FOFA_PAGE_SIZE):
             return None # 超过最大页数限制，停止
             
        url = self._build_fofa_url(query_str, email, key, size, fields, page=page_num)
        
        status_code, content = self._make_request_sync(url=url, proxy=self.default_proxy)
        
        if status_code == 200:
            try:
                result = json.loads(content)
                if result.get('error'):
                    # 即使 Key 成功，特定页数也可能因频率或其他原因失败
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
        """
        并发请求除 Page 1 外的所有页面。
        """
        global stop_flag
        chat_id = context.effective_chat.id
        
        # FOFA API 的 max_size 默认是 10000，且每页 100
        size_limit = min(total_size, query_details.get('size', 10000), 10000)
        
        # 需要抓取的总页数 (Page 1 已经抓取，所以从 Page 2 开始)
        total_pages = math.ceil(size_limit / FOFA_PAGE_SIZE)
        pages_to_fetch = list(range(2, total_pages + 1))
        
        if not pages_to_fetch:
            return [] # 只有一页数据
            
        max_workers = self.config.get('max_threads', DEFAULT_MAX_THREADS)
        
        context.bot.send_message(
            chat_id=chat_id,
            text=f"⚙️ 开始多线程抓取剩余 {len(pages_to_fetch)} 页，使用 **{max_workers}** 个并发线程...",
            parse_mode=ParseMode.MARKDOWN
        )

        all_results = []
        
        # 使用 ThreadPoolExecutor 实现并发
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            # 提交所有页面的抓取任务
            future_to_page = {
                executor.submit(self._fetch_single_page, query_details, key_info, page): page 
                for page in pages_to_fetch
            }

            for future in concurrent.futures.as_completed(future_to_page):
                page = future_to_page[future]
                
                # 再次检查停止标志，用于中断线程池的运行
                if stop_flag:
                    executor.shutdown(wait=False, cancel_futures=True)
                    logger.info(f"Chat {chat_id}: 线程池被 stop_flag 中断。")
                    return all_results # 返回目前已成功抓取的部分结果

                try:
                    # 获取线程的结果 (即该页面的结果列表)
                    page_results = future.result()
                    if page_results:
                        all_results.extend(page_results)
                        context.bot.send_message(
                             chat_id=chat_id,
                             text=f"✅ Page {page} 抓取成功，当前已收集 {len(all_results)} 条结果。"
                        )
                    else:
                        context.bot.send_message(chat_id=chat_id, text=f"⚠️ Page {page} 抓取失败或结果为空。")
                        
                except Exception as exc:
                    logger.error(f"Page {page} 抓取时发生异常: {exc}")
                    if "Thread stopped by user flag" not in str(exc):
                        context.bot.send_message(chat_id=chat_id, text=f"❌ Page {page} 抓取过程中发生错误: {exc.__class__.__name__}")

        return all_results


# --- Telegram Bot 状态常量 ---
STATE_KKFOFA_QUERY = 1
STATE_KKFOFA_MODE = 2
STATE_SETTINGS_MAIN = 3
STATE_ADD_KEY_EMAIL = 4
STATE_ADD_KEY_KEY = 5
STATE_SET_THREADS = 6 # 新增设置线程数状态


# --- 统一的停止指令处理函数 (改进用户反馈) ---
def unified_stop_handler(update: Update, context: CallbackContext) -> int:
    """
    处理 /stop 指令，设置全局停止标志并提供即时用户反馈。
    """
    global stop_flag
    
    if update.callback_query:
        # 如果是回调查询，需要先回答
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
        
    update.message.reply_text("请输入 **FOFA 查询语句** (例如：`title=\"xxx\" && country=\"CN\"`)：")
    return STATE_KKFOFA_QUERY

def process_fofa_query(update: Update, context: CallbackContext) -> int:
    """接收查询语句并启动 FOFA 查询任务。"""
    fofa_query = update.message.text
    
    # 存储查询参数到 context.user_data
    context.user_data['fofa_query_str'] = fofa_query
    context.user_data['fofa_query_size'] = 1000 # 默认查询 1000 条
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
    # 默认模式使用 STATE_KKFOFA_QUERY 中设置的值

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
    
    # 检查 Key 回退结果
    if status != "SUCCESS":
        # 错误或停止，结果处理已在 execute_query_with_key_fallback 完成
        return ConversationHandler.END
        
    # 2. Page 1 成功，开始多线程抓取
    total_results = page1_result.get('results', [])
    
    # 检查是否只有一页或结果太少，无需多线程
    if total_size <= FOFA_PAGE_SIZE or total_size == len(total_results):
        context.bot.send_message(
            chat_id=context.effective_chat.id, 
            text=f"✅ 总结果数 {total_size}，只有 1 页数据，无需多线程。",
        )
        remaining_results = []
    else:
        # 多线程抓取剩余页面
        remaining_results = client.fetch_all_pages_concurrently(context, query_details, total_size, key_info)
        
    # 3. 合并所有结果
    all_results = total_results + remaining_results
    final_count = len(all_results)
    
    # 4. 最终结果处理
    if final_count > 0:
        # 记录历史
        history = load_history()
        history.append({
            'user_id': context.effective_user.id,
            'username': context.effective_user.username,
            'query': query_str,
            'size': final_count,
            'time': datetime.now(timezone.utc).isoformat()
        })
        save_history(history)
        
        # 简单结果格式化
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
    
    global stop_flag # 确保停止标志被重置
    stop_flag = False
        
    # 清理 user_data
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
        query.edit_message_text("🔑 **API Key 管理**\n请输入 **FOFA 邮箱** 来添加或管理 Key：")
        return STATE_ADD_KEY_EMAIL
    
    elif action == 'proxy':
        query.edit_message_text("🌐 **代理设置**\n请输入代理地址 (如：`http://user:pass@host:port`)，输入 `None` 清除：")
        context.user_data['settings_mode'] = 'proxy'
        return STATE_ADD_KEY_EMAIL # 复用状态
        
    elif action == 'exit':
        query.edit_message_text("✅ 已退出设置菜单。")
        return ConversationHandler.END
        
    return STATE_SETTINGS_MAIN # 保持在当前状态

def set_threads_handler(update: Update, context: CallbackContext) -> int:
    """接收用户输入的线程数。"""
    try:
        new_threads = int(update.message.text.strip())
        if new_threads < 1 or new_threads > 50: # 限制线程数范围
            raise ValueError("线程数不在合理范围")
            
        config = load_config()
        config['max_threads'] = new_threads
        save_config(config)
        
        update.message.reply_text(f"✅ **并发线程数已成功设置为**：`{new_threads}`", parse_mode=ParseMode.MARKDOWN)
        
    except ValueError:
        update.message.reply_text("❌ 输入无效。请输一个介于 1 到 50 之间的整数作为线程数。")
        return STATE_SET_THREADS # 保持在当前状态，让用户重新输入
        
    # 返回主设置菜单
    return settings_command(update, context) 


# --- 其他命令的占位符实现 ---
def start_command(update: Update, context: CallbackContext):
    update.message.reply_text("欢迎使用 FOFA 搜索机器人！输入 /kkfofa 开始查询，/settings 配置 API Key。")

def help_command(update: Update, context: CallbackContext):
    update.message.reply_text("这是一个用于 FOFA 资产搜索的 Telegram 机器人。\n"
                              "主要命令:\n"
                              "/kkfofa - 开始 FOFA 查询。\n"
                              "/settings - 管理 API Key、代理和**并发线程数**。\n"
                              "/history - 查看查询历史。\n"
                              "/stop - 停止当前正在执行的查询任务 (关键功能)。")

def history_command(update: Update, context: CallbackContext):
    """显示最近的查询历史。"""
    history = load_history()
    if not history:
        update.message.reply_text("🕰️ 暂无查询历史记录。")
        return

    # 只显示最近 10 条
    recent_history = history[-10:]
    
    output = "🕰️ **最近 10 条查询历史:**\n"
    for item in reversed(recent_history):
        dt = datetime.fromisoformat(item['time']).astimezone(timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')
        output += f"• **[{dt}]** 结果:{item['size']:<5} | `{item['query'][:50]}...`\n"

    update.message.reply_text(output, parse_mode=ParseMode.MARKDOWN)


# --- 主函数和 Bot 启动 ---
def main():
    """主函数，负责启动 Bot。"""
    if BOT_TOKEN == 'YOUR_TELEGRAM_BOT_TOKEN':
        logger.error("BOT_TOKEN 未设置。请检查代码中的 BOT_TOKEN 变量。")
        sys.exit(1)
        
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
            STATE_ADD_KEY_EMAIL: [MessageHandler(Filters.text & ~Filters.command, lambda update, context: update.message.reply_text("暂未实现 Key/代理输入逻辑。"), pass_user_data=True)], # 简化Key/Proxy输入
            STATE_SET_THREADS: [MessageHandler(Filters.text & ~Filters.command, set_threads_handler)],
            
            # STATE_ADD_KEY_EMAIL/STATE_ADD_KEY_KEY/STATE_SET_PROXY 的完整逻辑在此被省略，只保留框架
        },
        fallbacks=[unified_stop_handler]
    )

    # 3. 注册所有 Handler
    dispatcher.add_handler(CommandHandler("start", start_command))
    dispatcher.add_handler(CommandHandler("help", help_command))
    dispatcher.add_handler(CommandHandler("history", history_command))
    dispatcher.add_handler(CommandHandler("stop", unified_stop_handler)) # /stop 命令单独注册
    dispatcher.add_handler(settings_conv)
    dispatcher.add_handler(kkfofa_conv)

    try:
        updater.bot.set_my_commands([
            BotCommand("kkfofa", "🔍 资产搜索"),
            BotCommand("settings", "⚙️ 设置与管理"),
            BotCommand("stop", "🛑 停止/取消"),
            BotCommand("help", "❓ 帮助手册"),
            BotCommand("history", "🕰️ 查询历史"),
        ])
    except Exception as e:
        logger.warning(f"设置机器人命令失败: {e}")

    logger.info("🚀 机器人已启动并开始轮询...")
    updater.start_polling()
    updater.idle()
    logger.info("机器人已安全关闭。")

if __name__ == '__main__':
    # 确保 config.json 存在
    if not os.path.exists(CONFIG_FILE):
        save_config(load_config())
    
    main()
