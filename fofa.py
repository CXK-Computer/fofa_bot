import os
import json
import logging
import base64
import time
import re
import random
import httpx 
import sys
import math
import concurrent.futures 
import warnings
import subprocess 

from datetime import datetime, timedelta, timezone
from functools import wraps

# 忽略特定的 DeprecationWarning (如 set_default_dispatcher_args)
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
CURRENT_SCRIPT_PATH = os.path.abspath(__file__) 
MAX_HISTORY_SIZE = 50
DEFAULT_TIMEOUT_SEC = 30
DEFAULT_MAX_THREADS = 5
FOFA_PAGE_SIZE = 100

# --- 全局停止标志 (用于中断耗时操作) ---
stop_flag = False

# --- Telegram Bot Token (已设置) ---
BOT_TOKEN = "8325002891:AAHzYRlWn2Tq_lMyzbfBbkhPC-vX8LqS6kw"

# --- 日志配置 ---
logger = logging.getLogger('fofa_bot')
logger.setLevel(logging.INFO)
if not logger.handlers:
    stream_handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)


# --- 配置和历史操作 (关键修改区域) ---
def load_config():
    """加载配置，兼容所有旧格式，并统一返回 Key 字符串列表。"""
    default_config = {
        "api_keys": [], # 格式: ["key1", "key2", "key3", ...]
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
            
            new_api_keys = []
            
            # 兼容旧的 "apis" 字段 (字符串列表)
            if "apis" in config and isinstance(config["apis"], list):
                new_api_keys.extend(config["apis"])
                del config["apis"] 
                logger.warning("检测到旧版 'apis' 字段已转换。")
            
            # 兼容旧的 "api_keys" 字段 (字典列表 [{"email":..., "key":...}] 或 字符串列表)
            if "api_keys" in config and isinstance(config["api_keys"], list):
                for item in config["api_keys"]:
                    if isinstance(item, str):
                        # 已经是 Key 字符串
                        new_api_keys.append(item)
                    elif isinstance(item, dict) and 'key' in item:
                        # 是旧的 Email/Key 字典格式
                        new_api_keys.append(item['key'])
                
            # 去重并更新到标准格式
            config["api_keys"] = list(set(new_api_keys))

            if 'max_threads' not in config:
                config['max_threads'] = DEFAULT_MAX_THREADS
                 
            if 'owner_id' not in config and 'admins' in config and config['admins']:
                 config['owner_id'] = int(config['admins'][0])

            return config
            
    except Exception as e:
        logger.error(f"加载或解析 {CONFIG_FILE} 失败: {e}. 使用默认配置。")
        return default_config

def save_config(config_data):
    """保存配置。"""
    # 确保保存时只保留新的 key 字符串列表
    if 'apis' in config_data:
        del config_data['apis']
        
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config_data, f, indent=4, ensure_ascii=False)

def load_history():
    """加载历史记录 (为简洁省略实现)。"""
    return []

def save_history(history_data):
    """保存历史记录 (为简洁省略实现)。"""
    pass


# --- 管理员权限检查装饰器 ---
def is_owner(func):
    """确保只有 owner_id 才能执行的命令"""
    @wraps(func)
    def wrapper(update: Update, context: CallbackContext):
        config = load_config()
        owner_id = config.get("owner_id")
        user_id = update.effective_user.id
        
        if owner_id is None:
            update.message.reply_text("❌ 尚未设置 Bot Owner ID，请在 config.json 中手动设置 'owner_id'。")
            return
            
        if user_id != owner_id:
            update.message.reply_text(f"🛑 权限不足。只有 Bot Owner (ID: `{owner_id}`) 可以执行此操作。", parse_mode=ParseMode.MARKDOWN)
            return

        return func(update, context)
    return wrapper

# --- FOFA 接口客户端类 (关键修改区域：移除 email 参数) ---
class FofaAPIClient:
    def __init__(self, config):
        self.config = config
        self.user_agent = config.get("user_agent", "FofaBot/1.0 (httpx/Concurrent)")
        self.default_proxy = config.get("proxy")

    def get_available_keys(self):
        """获取所有可用的 FOFA API Key 列表 (现在是字符串列表)。"""
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

    def _build_fofa_url(self, query_str, key, size, fields, page=1):
        """构造完整的 FOFA API URL (已移除 email 参数)。"""
        base_url = "https://fofa.info/api/v1/search/all"
        query_hash = base64.b64encode(query_str.encode()).decode()
        
        url = (
            f"{base_url}?qbase64={query_hash}"
            f"&key={key}" # *** 关键修改：只使用 key ***
            f"&size={size}&fields={fields}&page={page}"
        )
        return url

    def execute_query_with_key_fallback(self, query_details, context: CallbackContext):
        """使用 Key 回退机制执行 Page 1 查询，并返回成功 Key 的信息和总结果数。"""
        global stop_flag
        
        available_keys = self.get_available_keys() 
        if not available_keys:
             return None, 0, None, "NO_KEYS"

        random.shuffle(available_keys) 
        chat_id = context.effective_chat.id
        
        for i, key_str in enumerate(available_keys): # 迭代 Key 字符串
            if stop_flag:
                context.bot.send_message(
                    chat_id=chat_id, 
                    text="✅ 任务已成功停止。", 
                    parse_mode=ParseMode.MARKDOWN
                )
                stop_flag = False
                return None, 0, None, "STOPPED"

            key_display = f"`{key_str[:6]}...`" # 只显示 Key 前六位
            query_str = query_details.get('query', '')
            size = query_details.get('size', 100)
            fields = query_details.get('fields', 'host,ip,port')
            
            url = self._build_fofa_url(query_str, key_str, size, fields, page=1) # 传入 key_str
            
            context.bot.send_message(
                chat_id=chat_id, 
                text=f"🔑 正在使用 Key ({key_display} / 尝试 {i+1}/{len(available_keys)}) 尝试 Page 1 查询...",
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
                        if 'balance is 0' in errmsg or 'Key invalid' in errmsg:
                            logger.error(f"Key {key_display} 失效或余额不足。")
                            context.bot.send_message(chat_id=chat_id, text="💰 API Key 失效或余额不足，尝试下一个 Key...")
                            continue
                        logger.error(f"Key {key_display} API 错误: {errmsg}")
                        context.bot.send_message(chat_id=chat_id, text=f"❌ Key API 错误: {errmsg[:20]}... 尝试下一个 Key...")
                        continue
                        
                    return result, result.get('size', 0), key_str, "SUCCESS" # 返回成功的 key_str
                except json.JSONDecodeError:
                    return None, 0, None, "INVALID_JSON"
            
            context.bot.send_message(chat_id=chat_id, text=f"❌ 请求失败 (Code: {status_code})，尝试下一个 Key...")
                
        return None, 0, None, "FAILED_ALL"

    def _fetch_single_page(self, query_details, key_str, page_num):
        """用于线程池的单个页面抓取函数。"""
        global stop_flag
        if stop_flag:
            raise concurrent.futures.CancelledError("Thread stopped by user flag")
        
        query_str = query_details.get('query')
        size = query_details.get('size')
        fields = query_details.get('fields')

        url = self._build_fofa_url(query_str, key_str, size, fields, page=page_num)
        
        status_code, content = self._make_request_sync(url=url, proxy=self.default_proxy)
        
        # ... (错误处理与解析逻辑与之前版本类似，确保使用 key_str)
        if status_code == 200:
            try:
                result = json.loads(content)
                return result.get('results', [])
            except json.JSONDecodeError:
                return []
        return []

    def fetch_all_pages_concurrently(self, context: CallbackContext, query_details, total_size, key_str):
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
                executor.submit(self._fetch_single_page, query_details, key_str, page): page 
                for page in pages_to_fetch
            }

            for future in concurrent.futures.as_completed(future_to_page):
                # ... (线程池处理逻辑，与之前版本类似)
                if stop_flag:
                    executor.shutdown(wait=False, cancel_futures=True)
                    return all_results

                try:
                    page = future_to_page[future]
                    page_results = future.result()
                    if page_results:
                        all_results.extend(page_results)
                        context.bot.send_message(
                             chat_id=chat_id,
                             text=f"✅ Page {page} 抓取成功，当前已收集 {len(all_results)} 条结果。"
                        )
                except Exception:
                    pass
                    
        return all_results


# --- Telegram Bot 状态常量 ---
STATE_KKFOFA_QUERY = 1
STATE_KKFOFA_MODE = 2
STATE_SETTINGS_MAIN = 3
STATE_ADD_KEY = 4 # 状态简化：只输入 Key
STATE_SET_THREADS = 6
STATE_DOWNLOAD_SCRIPT = 7 


# --- 统一的停止指令处理函数 ---
def unified_stop_handler(update: Update, context: CallbackContext) -> int:
    """处理 /stop 指令，设置全局停止标志并提供即时用户反馈。"""
    global stop_flag
    
    if update.callback_query:
        query = update.callback_query
        query.answer("停止指令已收到。")
        query.edit_message_text(
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


# --- 脚本升级功能 ---
@is_owner
def upgrade_command(update: Update, context: CallbackContext) -> int:
    """启动脚本升级流程，提示用户输入新脚本链接。"""
    update.message.reply_text(
        f"⚙️ **脚本升级模式**\n请输入新脚本的 **完整下载链接** (例如：GitHub Gist 的 Raw 链接)：\n"
        f"当前脚本路径: `{CURRENT_SCRIPT_PATH}`",
        parse_mode=ParseMode.MARKDOWN
    )
    return STATE_DOWNLOAD_SCRIPT

@is_owner
def download_script_handler(update: Update, context: CallbackContext) -> int:
    """处理用户提供的下载链接，并替换脚本文件。"""
    url = update.message.text.strip()
    chat_id = update.effective_chat.id
    
    if not url.startswith('http'):
        context.bot.send_message(chat_id=chat_id, text="❌ URL 格式不正确，请确保它以 `http` 或 `https` 开头。")
        return STATE_DOWNLOAD_SCRIPT 

    context.bot.send_message(chat_id=chat_id, text=f"📥 正在尝试从 `{url}` 下载新脚本...")
    
    try:
        response = httpx.get(url, follow_redirects=True, timeout=10)
        response.raise_for_status()
        new_script_content = response.text
        
        backup_path = CURRENT_SCRIPT_PATH + ".bak." + datetime.now().strftime('%Y%m%d%H%M%S')
        os.rename(CURRENT_SCRIPT_PATH, backup_path)
        
        with open(CURRENT_SCRIPT_PATH, 'w', encoding='utf-8') as f:
            f.write(new_script_content)
            
        context.bot.send_message(
            chat_id=chat_id, 
            text=(
                "✅ **脚本更新成功！**\n"
                f"旧脚本已备份到: `{backup_path}`\n\n"
                "⚠️ **重要提示:** 为了使新代码生效，您需要**重启 Bot 进程**。"
            ),
            parse_mode=ParseMode.MARKDOWN
        )

    except httpx.HTTPStatusError as e:
        context.bot.send_message(chat_id=chat_id, text=f"❌ HTTP 下载错误 (Status {e.response.status_code})。请检查链接是否有效。")
        if 'backup_path' in locals(): os.rename(backup_path, CURRENT_SCRIPT_PATH)
    except Exception as e:
        context.bot.send_message(chat_id=chat_id, text=f"❌ 脚本更新失败: {e.__class__.__name__}: {e}")
        if 'backup_path' in locals(): os.rename(backup_path, CURRENT_SCRIPT_PATH)
        
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
    context.user_data['fofa_query_str'] = fofa_query
    
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

    query_size = 1000
    query_fields = 'host,ip,port,title,country'
    if mode == 'simple':
        query_size = 500
        query_fields = 'host,ip'
    
    query_str = context.user_data.get('fofa_query_str')
    query_details = {'query': query_str, 'size': query_size, 'fields': query_fields}

    query.edit_message_text(f"🚀 开始执行 FOFA Page 1 查询...\n查询语句：`{query_str}`", parse_mode=ParseMode.MARKDOWN)

    config = load_config()
    client = FofaAPIClient(config)
    
    # 1. 执行 Page 1 查询和 Key 回退
    page1_result, total_size, key_str, status = client.execute_query_with_key_fallback(query_details, context)
    
    if status != "SUCCESS":
        return ConversationHandler.END
        
    # 2. Page 启动多线程抓取
    total_results = page1_result.get('results', [])
    if total_size > FOFA_PAGE_SIZE and total_size != len(total_results):
        remaining_results = client.fetch_all_pages_concurrently(context, query_details, total_size, key_str)
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
        # (历史记录保存省略)
        key_display = f"`{key_str[:6]}...`"
        first_results_str = "\n".join([f"| {r[0]:<40} | {r[1]:<15} |" for r in all_results[:5]])
        output = (
            f"🎉 **任务完成！** 抓取结果 **{final_count}** 条 (目标 {total_size} 条)。\n\n"
            f"**使用 Key:** {key_display}\n"
            f"**查询语句:** `{query_str}`\n"
            f"**模式/字段:** {query_fields}\n"
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
        
    return ConversationHandler.END


# --- 设置对话处理 (关键修改区域：Key 管理简化) ---
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
        config = load_config()
        key_list = config.get("api_keys", [])
        key_count = len(key_list)
        
        key_info = "\n".join([f"`{k[:6]}...`" for k in key_list]) if key_list else "无"

        keyboard = [[InlineKeyboardButton("➕ 添加新 Key", callback_data="key_add")]]
        if key_count > 0:
            keyboard.append([InlineKeyboardButton("🗑️ 清空所有 Key", callback_data="key_clear")])
        keyboard.append([InlineKeyboardButton("🔙 返回设置", callback_data="key_back")])
        
        query.edit_message_text(
            f"🔑 **API Key 管理 ({key_count} 个)**\n"
            f"当前 Key 列表 (仅显示前 6 位):\n{key_info}\n\n"
            f"**请选择操作或直接回复 Key 字符串进行添加：**",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        return STATE_ADD_KEY
    
    elif action == 'proxy':
        query.edit_message_text("🌐 **代理设置**\n请输入代理地址 (如：`http://user:pass@host:port`)，输入 `None` 清除：")
        context.user_data['settings_mode'] = 'proxy'
        return STATE_ADD_KEY # 复用状态
        
    elif action == 'exit':
        query.edit_message_text("✅ 已退出设置菜单。")
        return ConversationHandler.END
        
    return STATE_SETTINGS_MAIN

def key_management_callback(update: Update, context: CallbackContext) -> int:
    """处理 Key 管理菜单的回调。"""
    query = update.callback_query
    query.answer()
    
    action = query.data.split('_')[1]
    config = load_config()
    
    if action == 'add':
        query.edit_message_text("请直接回复您完整的 **FOFA API Key** 字符串：")
        return STATE_ADD_KEY
    
    elif action == 'clear':
        config["api_keys"] = []
        save_config(config)
        query.edit_message_text("🗑️ **已清空所有 API Key。**")
        return settings_command(update, context) 

    elif action == 'back':
        # 模拟回到 settings_command
        query.message.text = "返回" # 临时设置 text 属性以复用 settings_command
        return settings_command(query.message, context)
        
    return STATE_ADD_KEY

def add_key_or_proxy_handler(update: Update, context: CallbackContext) -> int:
    """接收用户输入的 Key 或代理。"""
    input_text = update.message.text.strip()
    config = load_config()
    
    # 检查是否是代理设置模式 (STATE_ADD_KEY 与代理设置复用)
    if context.user_data.get('settings_mode') == 'proxy':
        if input_text.lower() == 'none':
            config['proxy'] = None
            update.message.reply_text("🌐 **代理已清除。**")
        else:
            config['proxy'] = input_text
            update.message.reply_text(f"🌐 **代理已设置为**：`{input_text}`", parse_mode=ParseMode.MARKDOWN)
        context.user_data.pop('settings_mode', None)
        save_config(config)
        return settings_command(update, context) 

    # Key 添加逻辑 (默认逻辑)
    if re.match(r"^[a-f0-9]{32}$", input_text): # 简单的 32 位 MD5 格式检查
        if input_text not in config["api_keys"]:
            config["api_keys"].append(input_text)
            save_config(config)
            update.message.reply_text(f"✅ **API Key 已添加！** (当前 {len(config['api_keys'])} 个 Key)")
        else:
            update.message.reply_text("⚠️ 此 Key 已存在，无需重复添加。")
    else:
        update.message.reply_text("❌ Key 格式不正确 (应为 32 位十六进制字符串)。请重新输入。")
        return STATE_ADD_KEY
        
    return settings_command(update, context) # 返回主设置菜单


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

def start_command(update: Update, context: CallbackContext):
    update.message.reply_text("欢迎使用 FOFA 搜索机器人！输入 /kkfofa 开始查询，/settings 配置 API Key 或代理。")

def help_command(update: Update, context: CallbackContext):
    update.message.reply_text("这是一个用于 FOFA 资产搜索的 Telegram 机器人。\n"
                              "主要命令:\n"
                              "/kkfofa - 开始 FOFA 查询。\n"
                              "/settings - 管理 API Key、代理和**并发线程数**。\n"
                              "/upgrade - 仅限 Owner，从外部链接升级脚本。\n"
                              "/stop - 停止当前正在执行的查询任务。")

# --- 主函数和 Bot 启动 ---
def main():
    """主函数，负责启动 Bot。"""
    if BOT_TOKEN == '8325002891:AAHzYRlWn2Tq_lMyzbfBbkhPC-vX8LqS6kw':
        logger.error("BOT_TOKEN 仍为默认值，请替换为您的 Bot Token。")
        # 尽管有警告，我们仍然允许运行以便测试其他功能
        
    config = load_config()
    save_config(config) 
    
    logger.info(f"当前运行脚本路径: {CURRENT_SCRIPT_PATH}")
    if config.get("owner_id"):
        logger.info(f"Bot Owner ID 已设置: {config['owner_id']}")
        
    try:
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
                STATE_ADD_KEY: [ # Key/代理输入的统一状态
                    CallbackQueryHandler(key_management_callback, pattern=r"^key_"),
                    MessageHandler(Filters.text & ~Filters.command, add_key_or_proxy_handler),
                ],
                STATE_SET_THREADS: [MessageHandler(Filters.text & ~Filters.command, set_threads_handler)],
            },
            fallbacks=[unified_stop_handler]
        )
        
        # 3. 脚本升级对话
        upgrade_conv = ConversationHandler(
            entry_points=[CommandHandler("upgrade", upgrade_command)],
            states={
                STATE_DOWNLOAD_SCRIPT: [MessageHandler(Filters.text & ~Filters.command, download_script_handler)],
            },
            fallbacks=[unified_stop_handler]
        )

        # 4. 注册所有 Handler
        dispatcher.add_handler(CommandHandler("start", start_command))
        dispatcher.add_handler(CommandHandler("help", help_command))
        dispatcher.add_handler(CommandHandler("stop", unified_stop_handler)) 
        dispatcher.add_handler(settings_conv)
        dispatcher.add_handler(kkfofa_conv)
        dispatcher.add_handler(upgrade_conv)

        updater.bot.set_my_commands([
            BotCommand("kkfofa", "🔍 资产搜索"),
            BotCommand("settings", "⚙️ 设置与管理"),
            BotCommand("upgrade", "⬆️ 升级脚本 (Owner Only)"),
            BotCommand("stop", "🛑 停止/取消"),
            BotCommand("help", "❓ 帮助手册"),
        ])

        logger.info("🚀 机器人已启动并开始轮询...")
        updater.start_polling()
        updater.idle()
        logger.info("机器人已安全关闭。")

    except Exception as e:
        logger.error(f"机器人启动失败: {e}")
        sys.exit(1)

if __name__ == '__main__':
    # 在最外层确保配置的初始化和转换
    try:
        if not os.path.exists(CONFIG_FILE):
            save_config(load_config())
        else:
            config = load_config()
            save_config(config)
    except Exception as e:
        print(f"配置文件预处理失败: {e}")
        
    main()
