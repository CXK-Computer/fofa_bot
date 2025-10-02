import os
import json
import logging
import logging.handlers
import base64
import time
import re
import random
import httpx # 引入 httpx
import sys
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

# --- 全局停止标志 (用于中断耗时操作) ---
stop_flag = False

# --- 初始化 ---
if not os.path.exists(LOCAL_CACHE_DIR):
    os.makedirs(LOCAL_CACHE_DIR)

# --- 日志配置 (每日轮换) ---
# 确保在运行前创建 logs 目录（如果需要）
# log_dir = "logs"
# if not os.path.exists(log_dir):
#     os.makedirs(log_dir)
# LOG_PATH = os.path.join(log_dir, LOG_FILE)

# 配置日志
logger = logging.getLogger('fofa_bot')
logger.setLevel(logging.INFO)

# 创建一个旋转文件处理器，每天轮换日志文件
handler = logging.handlers.TimedRotatingFileHandler(
    LOG_FILE, 
    when='midnight', 
    interval=1, 
    backupCount=7,
    encoding='utf-8'
)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# 同时输出到控制台
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)


# --- 配置文件/历史记录操作 ---
def load_config():
    """加载配置，如果文件不存在则返回默认配置。"""
    if not os.path.exists(CONFIG_FILE):
        return {
            "api_keys": [], # 格式: [{"email": "user@example.com", "key": "xxxxxxxxxxxxxxxx"}, ...]
            "user_agent": "FofaBot/1.0 (httpx)",
            "proxy": None,
            "owner_id": None
        }
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

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
        self.user_agent = config.get("user_agent", "FofaBot/1.0 (httpx)")
        self.default_proxy = config.get("proxy")

    def get_available_keys(self):
        """获取所有可用的 FOFA API Key 列表。"""
        return self.config.get("api_keys", [])

    # =================================================================
    # V2: 使用 httpx 替代 curl/os.popen，支持超时和更好的错误处理
    # =================================================================
    def _make_request_sync(self, url, method='GET', data=None, proxy=None, timeout=DEFAULT_TIMEOUT_SEC):
        """
        同步执行 HTTP 请求。

        :param url: 请求 URL
        :param method: HTTP 方法 (GET/POST)
        :param data: 请求体数据
        :param proxy: 代理字符串 (如 'http://user:pass@host:port')
        :param timeout: 请求超时时间（秒）
        :return: (status_code, content)
        """
        proxies = {"all": proxy} if proxy else None
        
        try:
            # 使用 httpx.Client 保持会话和配置，但这里直接用 request 也可以
            with httpx.Client(proxies=proxies, verify=False, timeout=timeout) as client:
                response = client.request(
                    method,
                    url,
                    data=data,
                    headers={'User-Agent': self.user_agent},
                )
            
            # 不直接抛出异常，而是返回状态码和内容，由上层函数处理 API 错误
            return response.status_code, response.text
            
        except httpx.TimeoutException:
            # 捕获超时错误 (408 Request Timeout)
            return 408, f"请求超时: FOFA 服务器响应超过 {timeout} 秒"
        except httpx.RequestError as e:
            # 捕获所有其他请求错误 (DNS 失败, 连接失败等)
            return 500, f"网络或连接错误: {e.__class__.__name__}: {e}"
        except Exception as e:
            # 捕获未知错误
            return 500, f"发生未知错误: {e.__class__.__name__}: {e}"

    # =================================================================
    # V2: 改进的带回退机制的执行查询函数 (关键优化)
    # =================================================================
    def execute_query_with_fallback(self, query_details, context: CallbackContext, query_timeout=DEFAULT_TIMEOUT_SEC):
        """
        使用所有可用的 API Key 尝试执行 FOFA 查询，并在每次尝试前检查 stop_flag。
        
        :param query_details: 查询参数字典 {'query': str, 'size': int, 'fields': str}
        :param context: Telegram CallbackContext
        :param query_timeout: 单次查询的超时时间
        :return: (API 响应内容, 状态字符串)
        """
        global stop_flag
        
        available_keys = self.get_available_keys() 
        if not available_keys:
             return None, "NO_KEYS"

        # 随机打乱 key 顺序
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
                stop_flag = False # 重置标志
                return None, "STOPPED"

            email = api_key_info.get('email', 'N/A')
            
            # 1. 构造 FOFA URL
            query_str = query_details.get('query', '')
            size = query_details.get('size', 100)
            fields = query_details.get('fields', 'host,ip,port')
            
            base_url = "https://fofa.info/api/v1/search/all"
            query_hash = base64.b64encode(query_str.encode()).decode()
            
            url = (
                f"{base_url}?qbase64={query_hash}"
                f"&email={email}&key={api_key_info.get('key')}"
                f"&size={size}&fields={fields}"
            )
            
            # 2. 通知用户尝试中
            context.bot.send_message(
                chat_id=chat_id, 
                text=f"🔑 正在使用 Key ({email[:5]}... / 尝试 {i+1}/{len(available_keys)}) 尝试查询...",
            )
            
            # 3. 发送请求 (使用 httpx 实现超时控制)
            status_code, content = self._make_request_sync(
                url=url, 
                proxy=self.default_proxy, 
                timeout=query_timeout
            )
            
            # 4. 结果判断
            if status_code == 200:
                # 成功获取数据
                return content, "SUCCESS"
            
            # 常见错误码处理
            try:
                error_data = json.loads(content)
                errmsg = error_data.get('errmsg', '未知 API 错误')
            except json.JSONDecodeError:
                errmsg = content # 如果不是 JSON，直接使用内容
            
            
            if status_code == 408:
                # 请求超时
                logger.warning(f"Key {email} 请求超时。")
                context.bot.send_message(chat_id=chat_id, text="⚠️ 请求超时，尝试下一个 Key...")
                continue # 尝试下一个 Key
            
            elif status_code == 401 or 'key invalid' in errmsg.lower():
                # API Key 无效或过期
                logger.error(f"Key {email} 无效。")
                context.bot.send_message(chat_id=chat_id, text="🔑 API Key 无效/过期，尝试下一个 Key...")
                continue # 尝试下一个 Key

            elif status_code == 402 or 'balance' in errmsg.lower():
                # 余额不足
                logger.error(f"Key {email} 余额不足。")
                context.bot.send_message(chat_id=chat_id, text="💰 API Key 余额不足，尝试下一个 Key...")
                continue # 尝试下一个 Key
            
            else:
                # 通用错误处理
                logger.error(f"Key {email} 查询失败，状态码: {status_code}, 错误信息: {errmsg}")
                context.bot.send_message(chat_id=chat_id, text=f"❌ 查询失败 (Code: {status_code})，尝试下一个 Key...")
                continue # 尝试下一个 Key
                
        # 循环结束，所有 Key 均失败
        return None, "FAILED_ALL"


# --- Telegram Bot 状态常量 ---
STATE_KKFOFA_QUERY = 1
STATE_KKFOFA_MODE = 2
STATE_SETTINGS_MAIN = 3
STATE_ADD_KEY_EMAIL = 4
STATE_ADD_KEY_KEY = 5


# --- 统一的停止指令处理函数 (改进用户反馈) ---
def unified_stop_handler(update: Update, context: CallbackContext) -> int:
    """
    处理 /stop 指令，设置全局停止标志并提供即时用户反馈。
    """
    global stop_flag
    
    # 1. 立即反馈用户 (关键改进)
    # 检查 update.message 是否存在，以应对 fallbacks 可能接收到 CallbackQuery 的情况
    if update.callback_query:
        update.callback_query.answer("停止指令已收到。")
        update.callback_query.edit_message_text(
            "🛑 **收到停止指令**。任务将在执行完当前请求后尽快停止。请稍候...",
            parse_mode=ParseMode.MARKDOWN
        )
    elif update.message:
        update.message.reply_text(
            "🛑 **收到停止指令**。当前任务（如 FOFA 查询）将在执行完当前请求后尽快停止。请稍候...", 
            parse_mode=ParseMode.MARKDOWN
        )
    
    # 2. 设置停止标志
    stop_flag = True
    
    # 3. 退出当前对话
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
    user_id = update.effective_user.id
    
    # 存储查询参数到 context.user_data
    context.user_data['fofa_query_str'] = fofa_query
    context.user_data['fofa_query_size'] = 1000 # 默认查询 1000 条
    context.user_data['fofa_query_fields'] = 'host,ip,port,title,country'
    
    # 提示用户选择模式
    keyboard = [
        [
            InlineKeyboardButton("默认模式 (1000条)", callback_data="mode_default"),
            InlineKeyboardButton("精简模式 (host,ip)", callback_data="mode_simple")
        ],
        [InlineKeyboardButton("自定义字段", callback_data="mode_custom")]
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
    
    if mode == 'default':
        # 使用默认参数，直接执行
        pass
    elif mode == 'simple':
        context.user_data['fofa_query_size'] = 500
        context.user_data['fofa_query_fields'] = 'host,ip'
    elif mode == 'custom':
        # 简略处理，实际中应进入另一个对话状态让用户输入字段
        query.edit_message_text("🚧 自定义字段功能待实现。本次使用默认模式执行。")
        # 实际代码中可以 return STATE_CUSTOM_FIELDS_INPUT

    query_str = context.user_data.get('fofa_query_str')
    query_details = {
        'query': query_str,
        'size': context.user_data.get('fofa_query_size'),
        'fields': context.user_data.get('fofa_query_fields'),
    }

    query.edit_message_text(f"🚀 开始执行 FOFA 查询...\n查询语句：`{query_str}`", parse_mode=ParseMode.MARKDOWN)

    # 实例化客户端
    config = load_config()
    client = FofaAPIClient(config)
    
    # 调用改进后的执行函数
    content, status = client.execute_query_with_fallback(query_details, context, query_timeout=DEFAULT_TIMEOUT_SEC)
    
    # 结果处理
    if status == "SUCCESS":
        try:
            result = json.loads(content)
            results_count = result.get('size', 0)
            
            # 记录历史
            history = load_history()
            history.append({
                'user_id': context.effective_user.id,
                'username': context.effective_user.username,
                'query': query_str,
                'size': results_count,
                'time': datetime.now(timezone.utc).isoformat()
            })
            save_history(history)
            
            # 格式化结果并发送
            if results_count > 0:
                # 简单结果格式化
                first_results = [f"| {r[0]:<40} | {r[1]:<15} |" for r in result.get('results', [])[:5]]
                output = (
                    f"✅ **查询成功！** 发现结果 **{results_count}** 条。\n\n"
                    f"**查询语句:** `{query_str}`\n"
                    f"**模式/字段:** {query_details['fields']}\n"
                    f"**--- 示例结果 (前 5 条) ---**\n"
                    f"```\n| Host (部分)                           | IP/Port         |\n"
                    f"|---------------------------------------|-----------------|\n"
                    + "\n".join(first_results) + 
                    "\n```"
                )
                
                # TODO: 实际应用中，如果结果超过一定量，应将结果保存为文件并通过 Telegram 发送
                
            else:
                output = f"⚠️ **查询成功，但未发现结果。**\n查询语句：`{query_str}`"
            
            context.bot.send_message(
                chat_id=context.effective_chat.id, 
                text=output,
                parse_mode=ParseMode.MARKDOWN
            )

        except json.JSONDecodeError:
            context.bot.send_message(
                chat_id=context.effective_chat.id, 
                text="❌ **查询成功，但返回数据格式错误。**"
            )
        
    elif status == "STOPPED":
        # 停止标志已由 execute_query_with_fallback 处理
        pass
    elif status == "FAILED_ALL":
        context.bot.send_message(
            chat_id=context.effective_chat.id,
            text="❌ **查询失败**：所有可用的 API Key 均查询失败或达到限制。"
        )
    elif status == "NO_KEYS":
        context.bot.send_message(
            chat_id=context.effective_chat.id,
            text="❌ **查询失败**：您的配置中没有可用的 FOFA API Key。请使用 /settings 配置。"
        )
        
    global stop_flag # 确保停止标志被重置
    stop_flag = False
        
    # 清理 user_data
    context.user_data.pop('fofa_query_str', None)
    context.user_data.pop('fofa_query_size', None)
    context.user_data.pop('fofa_query_fields', None)
        
    return ConversationHandler.END

# --- 其他命令的占位符实现 ---
def start_command(update: Update, context: CallbackContext):
    update.message.reply_text("欢迎使用 FOFA 搜索机器人！输入 /kkfofa 开始查询，/settings 配置 API Key。")

def help_command(update: Update, context: CallbackContext):
    update.message.reply_text("这是一个用于 FOFA 资产搜索的 Telegram 机器人。\n"
                              "主要命令:\n"
                              "/kkfofa - 开始 FOFA 查询。\n"
                              "/settings - 管理 API Key 和机器人设置。\n"
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


# --- 设置对话处理 (简化版) ---
def settings_command(update: Update, context: CallbackContext) -> int:
    """进入设置主菜单。"""
    config = load_config()
    key_count = len(config.get("api_keys", []))
    
    keyboard = [
        [InlineKeyboardButton(f"🔑 管理 API Key ({key_count} 个)", callback_data="set_keys")],
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
    
    if action == 'keys':
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


# --- 主函数和 Bot 启动 ---
def main():
    """主函数，负责启动 Bot。"""
    # 从环境变量或配置文件中获取 Bot Token
    BOT_TOKEN = os.environ.get('BOT_TOKEN', 'YOUR_TELEGRAM_BOT_TOKEN')
    if BOT_TOKEN == 'YOUR_TELEGRAM_BOT_TOKEN':
        logger.error("请设置 BOT_TOKEN 环境变量或替换代码中的占位符！")
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
        # 在任何状态下，遇到 /stop 都执行统一停止处理
        fallbacks=[unified_stop_handler] 
    )
    
    # 2. 设置对话 (简化版，仅用于演示)
    settings_conv = ConversationHandler(
        entry_points=[CommandHandler("settings", settings_command)],
        states={
            STATE_SETTINGS_MAIN: [CallbackQueryHandler(settings_callback, pattern=r"^set_")],
            # ... 省略 Key 添加的具体状态 ...
        },
        fallbacks=[unified_stop_handler]
    )

    # 3. 注册所有 Handler
    dispatcher.add_handler(CommandHandler("start", start_command))
    dispatcher.add_handler(CommandHandler("help", help_command))
    dispatcher.add_handler(CommandHandler("history", history_command))
    dispatcher.add_handler(unified_stop_handler) # 注册 /stop 命令处理器
    dispatcher.add_handler(settings_conv)
    dispatcher.add_handler(kkfofa_conv)
    # ... 其他 handlers ...

    try:
        updater.bot.set_my_commands([
            BotCommand("kkfofa", "🔍 资产搜索"),
            BotCommand("settings", "⚙️ 设置与管理"),
            BotCommand("stop", "🛑 停止/取消"), # 关键：显示 /stop 命令
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
    # 确保 config.json 存在，否则 main 启动会失败
    if not os.path.exists(CONFIG_FILE):
        save_config(load_config())
    
    main()
