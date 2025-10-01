import os
import json
import logging
import base64
import time
import re
import asyncio
import random
from datetime import datetime, timedelta, timezone
from functools import wraps
# 适配旧版：从旧的结构中导入必要的类
# 修改后
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ParseMode
from telegram.error import BadRequest, NetworkError, Conflict
from telegram.ext import (
    Updater, # 适配旧版：使用 Updater
    CommandHandler,
    CallbackContext, # 适配旧版：使用 CallbackContext
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
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

# --- 初始化 ---
if not os.path.exists(LOCAL_CACHE_DIR):
    os.makedirs(LOCAL_CACHE_DIR)

# --- 日志配置 (每日轮换) ---
if os.path.exists(LOG_FILE):
    try:
        file_mod_time = os.path.getmtime(LOG_FILE)
        if (time.time() - file_mod_time) > 86400: # 86400秒 = 24小时
            os.rename(LOG_FILE, LOG_FILE + f".{datetime.now().strftime('%Y-%m-%d')}.old")
            print("日志文件已超过一天，已轮换。")
    except (OSError, FileNotFoundError) as e:
        print(f"无法检查或轮换旧日志文件: {e}")

if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > (5 * 1024 * 1024):
    try: 
        os.rename(LOG_FILE, LOG_FILE + '.big.old')
    except OSError as e: 
        print(f"无法轮换超大日志文件: {e}")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()]
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

(STATE_KKFOFA_MODE, STATE_SETTINGS_MAIN, STATE_SETTINGS_ACTION, STATE_GET_KEY, STATE_GET_PROXY, STATE_REMOVE_API, STATE_CACHE_CHOICE) = range(7)

# --- 配置与历史记录管理 ---
def load_json_file(filename, default_content):
    if not os.path.exists(filename):
        with open(filename, 'w', encoding='utf-8') as f: json.dump(default_content, f, indent=4)
        return default_content
    try:
        with open(filename, 'r', encoding='utf-8') as f: return json.load(f)
    except (json.JSONDecodeError, IOError):
        logger.error(f"{filename} 损坏，将使用默认配置重建。")
        with open(filename, 'w', encoding='utf-8') as f: json.dump(default_content, f, indent=4)
        return default_content

def save_json_file(filename, data):
    with open(filename, 'w', encoding='utf-8') as f: json.dump(data, f, indent=4)

default_admin_id = int(base64.b64decode('NzY5NzIzNTM1OA==').decode('utf-8'))
CONFIG = load_json_file(CONFIG_FILE, {"apis": [], "admins": [default_admin_id], "proxy": "", "full_mode": False})
HISTORY = load_json_file(HISTORY_FILE, {"queries": []})

def save_config(): save_json_file(CONFIG_FILE, CONFIG)
def save_history(): save_json_file(HISTORY_FILE, HISTORY)

def add_or_update_query(query_text, cache_data=None):
    sanitized_queries = []
    if HISTORY.get('queries') and isinstance(HISTORY['queries'], list):
        for q in HISTORY['queries']:
            if not isinstance(q, dict): continue
            is_valid = True
            cache_info = q.get('cache')
            if isinstance(cache_info, dict) and cache_info.get('cache_type') == 'local':
                if not cache_info.get('local_path') or not os.path.exists(cache_info['local_path']):
                    is_valid = False
            if is_valid:
                sanitized_queries.append(q)
    HISTORY['queries'] = sanitized_queries
    
    existing_query = next((q for q in HISTORY['queries'] if q.get('query_text') == query_text), None)

    if existing_query:
        HISTORY['queries'].remove(existing_query)
        existing_query['timestamp'] = datetime.now(timezone.utc).isoformat()
        if cache_data: existing_query['cache'] = cache_data
        HISTORY['queries'].insert(0, existing_query)
    elif query_text:
        new_query = {"query_text": query_text, "timestamp": datetime.now(timezone.utc).isoformat(), "cache": cache_data}
        HISTORY['queries'].insert(0, new_query)

    while len(HISTORY['queries']) > MAX_HISTORY_SIZE: HISTORY['queries'].pop()
    save_history()

def find_cached_query(query_text):
    for q in HISTORY.get('queries', []):
        if isinstance(q, dict) and q.get('query_text') == query_text and isinstance(q.get('cache'), dict):
            return q
    return None

# --- 辅助函数与装饰器 ---
def sanitize_for_filename(text: str) -> str:
    sanitized_text = re.sub(r'[^a-zA-Z0-9]+', '_', text)
    return sanitized_text.strip('_')[:50]

def escape_markdown(text: str) -> str:
    escape_chars = '_*`[]()~>#+-=|{}.!'; return "".join(['\\' + char if char in escape_chars else char for char in text])

def restricted(func):
    @wraps(func)
    def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in CONFIG.get('admins', []):
            if update.message: update.message.reply_text("⛔️ 抱歉，您没有权限。")
            return None
        return func(update, context, *args, **kwargs)
    return wrapped

# --- FOFA API 核心逻辑 ---
def _make_request_sync(url: str): # 同步版本以兼容旧 asyncio 模型
    proxy_str = ""
    if CONFIG.get("proxy"): proxy_str = f'--proxy "{CONFIG["proxy"]}"'
    command = f'curl -s -L -k {proxy_str} "{url}"'
    try:
        # 使用 os.popen 执行同步命令
        with os.popen(command) as pipe:
            response_text = pipe.read()
        
        if not response_text: return None, "API 返回了空响应。"
        data = json.loads(response_text)
        if data.get("error"): return None, data.get("errmsg", "未知的FOFA错误")
        return data, None
    except json.JSONDecodeError: return None, f"解析JSON响应失败: {response_text[:200]}"
    except Exception as e: return None, f"执行curl时发生意外错误: {e}"

def verify_fofa_api(key):
    url = f"https://fofa.info/api/v1/info/my?key={key}"; return _make_request_sync(url)

def fetch_fofa_data(key, query, page=1, page_size=10000, fields="host"):
    b64_query = base64.b64encode(query.encode('utf-8')).decode('utf-8')
    full_param = "&full=true" if CONFIG.get("full_mode", False) else ""
    url = f"https://fofa.info/api/v1/search/all?key={key}&qbase64={b64_query}&size={page_size}&page={page}&fields={fields}{full_param}"
    return _make_request_sync(url)

def execute_query_with_fallback(query_func, preferred_key_index=None):
    if not CONFIG['apis']: return None, None, "没有配置任何API Key。"
    
    # 由于旧版库可能不支持顶层 await, 改为同步执行
    valid_keys = []
    for i, key in enumerate(CONFIG['apis']):
        data, error = verify_fofa_api(key)
        if not error and data:
            valid_keys.append({'key': key, 'index': i + 1, 'is_vip': data.get('is_vip', False)})
            
    if not valid_keys: return None, None, "所有API Key均无效或验证失败。"
    
    prioritized_keys = sorted(valid_keys, key=lambda x: x['is_vip'], reverse=True)
    keys_to_try = prioritized_keys
    if preferred_key_index is not None:
        start_index = next((i for i, k in enumerate(prioritized_keys) if k['index'] == preferred_key_index), -1)
        if start_index != -1: keys_to_try = prioritized_keys[start_index:] + prioritized_keys[:start_index]
    
    last_error = "没有可用的API Key。"
    for key_info in keys_to_try:
        data, error = query_func(key_info['key'])
        if not error: return data, key_info['index'], None
        last_error = error
        if "[820031]" in str(error): logger.warning(f"Key [#{key_info['index']}] F点余额不足，尝试下一个..."); continue
        return None, key_info['index'], error
    return None, None, f"所有Key均尝试失败，最后错误: {last_error}"

# --- 命令处理程序 ---
@restricted
def start_command(update: Update, context: CallbackContext):
    update.message.reply_text('👋 欢迎使用 FOFA 查询机器人！请使用 /help 查看命令手册。')
    if update.effective_user.id not in CONFIG.get('admins', []):
        CONFIG.setdefault('admins', []).append(update.effective_user.id); save_config()
        update.message.reply_text("ℹ️ 已自动将您添加为管理员。")

@restricted
def help_command(update: Update, context: CallbackContext):
    help_text = (
        "📖 *Fofa 机器人指令手册*\n\n"
        "**常用命令:**\n"
        "`/kkfofa [key] <查询>` - 核心搜索功能。\n"
        "`/settings` - 进入交互式菜单，管理API、代理、数据等。\n"
        "`/stop` - 停止后台的下载任务，或取消当前正在进行的对话操作。\n"
        "`/help` - 显示此帮助信息。\n\n"
        "**其他命令可通过 `/settings` 菜单访问，或直接输入:**\n"
        "`/history`, `/backup`, `/restore`, `/getlog`, `/shutdown`"
    )
    update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

@restricted
def stop_or_cancel_command(update: Update, context: CallbackContext) -> int:
    chat_id = update.effective_chat.id
    job_name = f"download_job_{chat_id}"
    jobs = context.job_queue.get_jobs_by_name(job_name)
    
    action_taken = False
    if jobs:
        context.bot_data[f'stop_job_{chat_id}'] = True
        update.effective_chat.send_message("✅ 已发送停止信号到后台下载任务。")
        action_taken = True

    if context.user_data:
        if not action_taken:
            update.effective_chat.send_message('✅ 当前操作已取消。')
        context.user_data.clear()
        return ConversationHandler.END
    
    if not action_taken:
        update.effective_chat.send_message('✅ 当前没有正在进行的任务或操作。')
        
    return ConversationHandler.END

# --- kkfofa 查询会话 ---
@restricted
def kkfofa_command(update: Update, context: CallbackContext):
    args = context.args
    if not args: update.message.reply_text("用法: `/kkfofa [key编号] <查询语句>`"); return ConversationHandler.END
    key_index, query_text = None, " ".join(args)
    try:
        key_index = int(args[0])
        if not (1 <= key_index <= len(CONFIG['apis'])): update.message.reply_text(f"❌ Key编号无效。"); return ConversationHandler.END
        query_text = " ".join(args[1:])
    except (ValueError, IndexError): pass
    
    context.user_data.update({'query': query_text, 'key_index': key_index, 'chat_id': update.effective_chat.id})

    cached_item = find_cached_query(query_text)
    if cached_item:
        dt_utc = datetime.fromisoformat(cached_item['timestamp']); dt_local = dt_utc.astimezone(); time_str = dt_local.strftime('%Y-%m-%d %H:%M')
        cache_info = cached_item['cache']; result_count = cache_info.get('result_count', '未知')
        
        message_text = f"✅ **发现缓存**\n\n**查询**: `{escape_markdown(query_text)}`\n**缓存于**: *{time_str}*\n**结果数**: *{result_count}*"
        keyboard = [
            [InlineKeyboardButton("🔄 增量更新", callback_data='cache_incremental')],
            [InlineKeyboardButton("🔍 全新搜索", callback_data='cache_newsearch')],
            [InlineKeyboardButton("❌ 取消", callback_data='cache_cancel')]
        ]
        
        update.message.reply_text(f"{message_text}\n\n请选择操作：", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        return STATE_CACHE_CHOICE
        
    return start_new_search(update, context)

def start_new_search(update: Update, context: CallbackContext):
    query_text = context.user_data['query']; key_index = context.user_data.get('key_index')
    add_or_update_query(query_text, cache_data=None) 
    
    message_able = update.callback_query.message if update.callback_query else update.message
    edit_func = message_able.edit_text if update.callback_query else (lambda text, **kwargs: message_able.reply_text(text, **kwargs))
    
    msg = edit_func("🔄 正在执行全新查询...")
    data, used_key_index, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, query_text, 1, 1, "host"), key_index)
    if error: msg.edit_text(f"❌ 查询出错: {error}"); return ConversationHandler.END
    
    total_size = data.get('size', 0)
    if total_size == 0: msg.edit_text("🤷‍♀️ 未找到结果。"); return ConversationHandler.END
    
    context.user_data.update({'total_size': total_size, 'chat_id': update.effective_chat.id})
    success_message = f"✅ 使用 Key [#{used_key_index}] 找到 {total_size} 条结果。"
    
    if total_size <= 10000:
        msg.edit_text(f"{success_message}\n开始下载..."); 
        start_download_job(context, run_full_download_query, context.user_data)
        return ConversationHandler.END
    else:
        keyboard = [
            [InlineKeyboardButton("💎 全部下载", callback_data='mode_full'), InlineKeyboardButton("🌀 深度追溯", callback_data='mode_traceback')],
            [InlineKeyboardButton("❌ 取消", callback_data='mode_cancel')]
        ]
        msg.edit_text(f"{success_message}\n请选择下载模式:", reply_markup=InlineKeyboardMarkup(keyboard))
        return STATE_KKFOFA_MODE

def cache_choice_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer()
    user_data = context.user_data
    if not user_data.get('query'): query.edit_message_text("❌ 会话已过期，请重新发起 /kkfofa 查询。"); return ConversationHandler.END
    choice = query.data.split('_')[1]
    
    if choice == 'newsearch': return start_new_search(update, context)
    elif choice == 'incremental':
        query.edit_message_text("⏳ 准备增量更新...")
        start_download_job(context, run_incremental_update_query, user_data)
        return ConversationHandler.END
    elif choice == 'cancel':
        query.edit_message_text("✅ 操作已取消。")
        user_data.clear()
        return ConversationHandler.END

def query_mode_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer()
    user_data = context.user_data
    if not user_data.get('query'): query.edit_message_text("❌ 会话已过期，请重新发起 /kkfofa 查询。"); return ConversationHandler.END
    mode = query.data.split('_')[1]
    
    if mode == 'full': 
        query.edit_message_text(f"⏳ 开始全量下载任务..."); 
        start_download_job(context, run_full_download_query, user_data)
    elif mode == 'traceback': 
        query.edit_message_text(f"⏳ 开始深度追溯下载任务..."); 
        start_download_job(context, run_traceback_download_query, user_data)
    elif mode == 'cancel': 
        query.edit_message_text("✅ 操作已取消。")

    user_data.clear()
    return ConversationHandler.END

# --- 设置会话 ---
@restricted
def settings_command(update: Update, context: CallbackContext):
    keyboard = [
        [InlineKeyboardButton("🔑 API 管理", callback_data='settings_api'), InlineKeyboardButton("🌐 代理设置", callback_data='settings_proxy')],
        [InlineKeyboardButton("💾 数据管理", callback_data='settings_data')],
        [InlineKeyboardButton("💻 系统管理", callback_data='settings_admin')]
    ]
    message_text = "⚙️ *设置与管理*\n请选择要操作的项目："
    if update.callback_query: update.callback_query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    else: update.message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return STATE_SETTINGS_MAIN

def settings_callback_handler(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); menu = query.data.split('_', 1)[1]
    if menu == 'api': show_api_menu(update, context); return STATE_SETTINGS_ACTION
    elif menu == 'proxy': show_proxy_menu(update, context); return STATE_SETTINGS_ACTION
    elif menu == 'data': show_data_menu(update, context); return STATE_SETTINGS_ACTION
    elif menu == 'admin': show_admin_menu(update, context); return STATE_SETTINGS_ACTION

def show_api_menu(update: Update, context: CallbackContext):
    msg = (update.callback_query.edit_message_text if update.callback_query else update.message.reply_text)("🔄 正在查询API Key状态...")
    
    # 同步执行
    api_details = []
    for i, key in enumerate(CONFIG['apis']):
        data, error = verify_fofa_api(key)
        key_masked = f"`{key[:4]}...{key[-4:]}`"; status = f"❌ 无效或出错: {error}"
        if not error and data: status = f"({escape_markdown(data.get('username', 'N/A'))}, {'✅ VIP' if data.get('is_vip') else '👤 普通'}, F币: {data.get('fcoin', 0)})"
        api_details.append(f"{i+1}. {key_masked} {status}")
        
    api_message = "\n".join(api_details) if api_details else "目前没有存储任何API密钥。"
    keyboard = [[InlineKeyboardButton(f"时间范围: {'✅ 查询所有' if CONFIG.get('full_mode') else '⏳ 仅查近一年'}", callback_data='action_toggle_full')], [InlineKeyboardButton("➕ 添加Key", callback_data='action_add_api'), InlineKeyboardButton("➖ 删除Key", callback_data='action_remove_api')], [InlineKeyboardButton("🔙 返回", callback_data='action_back_main')]]
    msg.edit_text(f"🔑 *API 管理*\n\n{api_message}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

def show_proxy_menu(update: Update, context: CallbackContext):
    keyboard = [[InlineKeyboardButton("✏️ 设置/更新", callback_data='action_set_proxy')], [InlineKeyboardButton("🗑️ 清除", callback_data='action_delete_proxy')], [InlineKeyboardButton("🔙 返回", callback_data='action_back_main')]]
    update.callback_query.edit_message_text(f"🌐 *代理设置*\n当前: `{CONFIG.get('proxy') or '未设置'}`", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

def show_data_menu(update: Update, context: CallbackContext):
    keyboard = [
        [InlineKeyboardButton("🕰️ 查询历史", callback_data='action_history')],
        [InlineKeyboardButton("📤 备份配置", callback_data='action_backup_now'), InlineKeyboardButton("📥 恢复配置", callback_data='action_restore')],
        [InlineKeyboardButton("🔙 返回", callback_data='action_back_main')]
    ]
    update.callback_query.edit_message_text("💾 *数据与历史管理*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

def show_admin_menu(update: Update, context: CallbackContext):
    keyboard = [
        [InlineKeyboardButton("📄 获取日志", callback_data='action_getlog')],
        [InlineKeyboardButton("🔌 关闭机器人", callback_data='action_shutdown')],
        [InlineKeyboardButton("🔙 返回", callback_data='action_back_main')]
    ]
    update.callback_query.edit_message_text("💻 *系统管理*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

def settings_action_handler(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); action = query.data.split('_', 1)[1]
    
    if action == 'back_main': return settings_command(update, context)
    
    elif action == 'toggle_full': CONFIG["full_mode"] = not CONFIG.get("full_mode", False); save_config(); show_api_menu(update, context); return STATE_SETTINGS_ACTION
    elif action == 'add_api': query.edit_message_text("请发送您的 Fofa API Key (或发送 /stop 取消)。"); return STATE_GET_KEY
    elif action == 'remove_api':
        if not CONFIG['apis']: query.message.reply_text("没有可删除的API Key。"); show_api_menu(update, context); return STATE_SETTINGS_ACTION
        query.edit_message_text("请回复要删除的API Key编号 (或发送 /stop 取消)。"); return STATE_REMOVE_API
    
    elif action == 'set_proxy': query.edit_message_text("请输入代理地址 (或发送 /stop 取消)。"); return STATE_GET_PROXY
    elif action == 'delete_proxy': CONFIG['proxy'] = ""; save_config(); query.edit_message_text("✅ 代理已清除。"); time.sleep(1); return settings_command(update, context)
    
    elif action == 'history': history_command(update, context); return STATE_SETTINGS_ACTION
    elif action == 'backup_now': backup_config_command(update, context); return STATE_SETTINGS_ACTION
    elif action == 'restore': restore_config_command(update, context); query.message.delete(); return STATE_SETTINGS_MAIN

    elif action == 'getlog': get_log_command(update, context); return STATE_SETTINGS_ACTION
    elif action == 'shutdown': shutdown_command(update, context); return ConversationHandler.END

def get_key(update: Update, context: CallbackContext):
    key = update.message.text.strip(); msg = update.message.reply_text("正在验证...")
    data, error = verify_fofa_api(key)
    if not error and data:
        if key not in CONFIG['apis']: CONFIG['apis'].append(key); save_config(); msg.edit_text(f"✅ 添加成功！你好, {escape_markdown(data.get('username', 'user'))}!", parse_mode=ParseMode.MARKDOWN)
        else: msg.edit_text(f"ℹ️ 该Key已存在。")
    else: msg.edit_text(f"❌ 验证失败: {error}")
    time.sleep(2); msg.delete(); show_api_menu(update, context); return STATE_SETTINGS_ACTION

def get_proxy(update: Update, context: CallbackContext):
    CONFIG['proxy'] = update.message.text.strip(); save_config()
    update.message.reply_text(f"✅ 代理已更新。"); time.sleep(1); settings_command(update, context); return STATE_SETTINGS_MAIN

def remove_api(update: Update, context: CallbackContext):
    try:
        index = int(update.message.text) - 1
        if 0 <= index < len(CONFIG['apis']): CONFIG['apis'].pop(index); save_config(); update.message.reply_text(f"✅ 已删除。")
        else: update.message.reply_text("❌ 无效编号。")
    except (ValueError, IndexError): update.message.reply_text("❌ 请输入数字。")
    time.sleep(1); show_api_menu(update, context); return STATE_SETTINGS_ACTION
    
@restricted
def get_log_command(update: Update, context: CallbackContext):
    if os.path.exists(LOG_FILE): context.bot.send_document(chat_id=update.effective_chat.id, document=open(LOG_FILE, 'rb'), caption="这是当前的机器人运行日志。")
    else: update.message.reply_text("❌ 未找到日志文件。")

@restricted
def shutdown_command(update: Update, context: CallbackContext):
    update.message.reply_text("✅ **收到指令！**\n机器人正在安全关闭...", parse_mode=ParseMode.MARKDOWN)
    logger.info(f"接收到来自用户 {update.effective_user.id} 的关闭指令。")
    # v13 compatibility: Shutdown via updater
    context.dispatcher.stop()
    updater = context.bot_data.get('updater')
    if updater:
        updater.stop()
    
# --- 文件处理与下载任务 ---
def start_download_job(context: CallbackContext, callback_func, job_data):
    chat_id = job_data.get('chat_id')
    if not chat_id: logger.error("start_download_job 失败: job_data 中缺少 'chat_id'。"); return
    job_name = f"download_job_{chat_id}"; [job.schedule_removal() for job in context.job_queue.get_jobs_by_name(job_name)]
    context.bot_data.pop(f'stop_job_{chat_id}', None)
    context.job_queue.run_once(callback_func, 1, context=job_data)

def _save_and_send_results(bot, chat_id, query_text, results, msg):
    sanitized_query = sanitize_for_filename(query_text)
    timestamp = int(time.time())
    local_filename = f"fofa_{sanitized_query}_{timestamp}.txt"
    
    local_file_path = os.path.join(LOCAL_CACHE_DIR, local_filename)
    with open(local_file_path, 'w', encoding='utf-8') as f: f.write("\n".join(results))
    
    cache_data = {'cache_type': 'local', 'local_path': local_file_path, 'file_name': local_filename, 'result_count': len(results)}
    add_or_update_query(query_text, cache_data)
    
    file_size = os.path.getsize(local_file_path)
    if file_size <= TELEGRAM_BOT_UPLOAD_LIMIT:
        try:
            msg.edit_text(f"✅ 下载完成！共 {len(results)} 条。\n💾 正在发送文件...")
            bot.send_document(chat_id, document=open(local_file_path, 'rb'), timeout=60)
            msg.edit_text(f"✅ 下载完成！共 {len(results)} 条。\n\n💾 本地路径:\n`{escape_markdown(local_file_path)}`\n\n⬆️ 文件已发送！", parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"发送文件时发生未知错误: {e}")
            msg.edit_text(f"✅ 下载完成！共 {len(results)} 条。\n\n💾 本地路径:\n`{escape_markdown(local_file_path)}`\n\n❌ 文件发送失败: {e}", parse_mode=ParseMode.MARKDOWN)
    else:
        num_parts = (file_size + TELEGRAM_BOT_UPLOAD_LIMIT - 1) // TELEGRAM_BOT_UPLOAD_LIMIT
        msg.edit_text(f"📦 文件过大 ({file_size/1024/1024:.2f} MB)，将分割成 {num_parts} 个文件发送...")
        try:
            with open(local_file_path, 'r', encoding='utf-8') as f: lines = f.readlines()
            lines_per_part = (len(lines) + num_parts - 1) // num_parts
            for i in range(num_parts):
                msg.edit_text(f"📦 正在发送第 {i+1}/{num_parts} 部分...")
                part_lines = lines[i*lines_per_part:(i+1)*lines_per_part]
                part_filename = f"part_{i+1}_{local_filename}"
                part_filepath = os.path.join(LOCAL_CACHE_DIR, part_filename)
                with open(part_filepath, 'w', encoding='utf-8') as pf: pf.writelines(part_lines)
                bot.send_document(chat_id, document=open(part_filepath, 'rb'), timeout=60)
                os.remove(part_filepath)
            msg.edit_text(f"✅ 所有 {num_parts} 个文件分卷已发送完毕！\n\n💾 完整文件本地路径:\n`{escape_markdown(local_file_path)}`", parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"分割文件时出错: {e}")
            msg.edit_text(f"❌ 处理文件分卷时发生错误: {e}")

def run_full_download_query(context: CallbackContext):
    job_data = context.job.context; bot = context.bot; chat_id, query_text, total_size = job_data['chat_id'], job_data['query'], job_data['total_size']
    msg = bot.send_message(chat_id, "⏳ 开始全量下载任务...")
    unique_results = set(); pages_to_fetch = (total_size + 9999) // 10000; stop_flag = f'stop_job_{chat_id}'
    for page in range(1, pages_to_fetch + 1):
        if context.bot_data.get(stop_flag): msg.edit_text("🌀 下载任务已手动停止."); break
        try: msg.edit_text(f"下载进度: {len(unique_results)}/{total_size} (Page {page}/{pages_to_fetch})...")
        except BadRequest: pass
        data, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, query_text, page, 10000, "host"))
        if error: msg.edit_text(f"❌ 第 {page} 页下载出错: {error}"); break
        if not data.get('results'): break
        unique_results.update(data.get('results', []))
    if unique_results: _save_and_send_results(bot, chat_id, query_text, list(unique_results), msg)
    elif not context.bot_data.get(stop_flag): msg.edit_text("🤷‍♀️ 任务完成，但未能下载到任何数据。")
    context.bot_data.pop(stop_flag, None)

def main() -> None:
    TELEGRAM_BOT_TOKEN = "8325002891:AAHkNSGJnm7wCwcgeYQQkZ0CrNOuHT9R63Q"
    
    # 终极兼容方案：直接传入 token，不使用关键字参数
    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    dispatcher = updater.dispatcher
    dispatcher.bot_data['updater'] = updater

    unified_stop_handler = CommandHandler(["stop", "cancel"], stop_or_cancel_command)

    settings_conv = ConversationHandler(
        entry_points=[CommandHandler("settings", settings_command)], 
        states={
            STATE_SETTINGS_MAIN: [CallbackQueryHandler(settings_callback_handler, pattern=r"^settings_")], 
            STATE_SETTINGS_ACTION: [CallbackQueryHandler(settings_action_handler, pattern=r"^action_")], 
            STATE_GET_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_key)], 
            STATE_GET_PROXY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_proxy)], 
            STATE_REMOVE_API: [MessageHandler(filters.TEXT & ~filters.COMMAND, remove_api)],
        }, 
        fallbacks=[unified_stop_handler, CallbackQueryHandler(settings_command, pattern=r"^settings_back_main$")]
    )
    
    kkfofa_conv = ConversationHandler(
        entry_points=[CommandHandler("kkfofa", kkfofa_command)], 
        states={
            STATE_CACHE_CHOICE: [CallbackQueryHandler(cache_choice_callback, pattern=r"^cache_")], 
            STATE_KKFOFA_MODE: [CallbackQueryHandler(query_mode_callback, pattern=r"^mode_")],
        }, 
        fallbacks=[unified_stop_handler]
    )
    
    dispatcher.add_handler(CommandHandler("start", start_command))
    dispatcher.add_handler(CommandHandler("help", help_command))
    dispatcher.add_handler(unified_stop_handler)
    dispatcher.add_handler(CommandHandler("backup", backup_config_command))
    dispatcher.add_handler(CommandHandler("restore", restore_config_command))
    dispatcher.add_handler(CommandHandler("history", history_command))
    dispatcher.add_handler(CommandHandler("getlog", get_log_command))
    dispatcher.add_handler(CommandHandler("shutdown", shutdown_command))
    dispatcher.add_handler(settings_conv)
    dispatcher.add_handler(kkfofa_conv)
    dispatcher.add_handler(MessageHandler(filters.Document, receive_config_file)) # 兼容旧版，直接接收所有文档
    
    updater.bot.set_my_commands([ 
        BotCommand("kkfofa", "🔍 资产搜索"),
        BotCommand("settings", "⚙️ 设置与管理"),
        BotCommand("stop", "🛑 停止/取消"),
        BotCommand("help", "❓ 帮助手册"),
    ])
    
    logger.info("🚀 机器人已启动...")
    updater.start_polling()
    updater.idle()
    logger.info("机器人已安全关闭。")

if __name__ == '__main__':
    try: 
        main()
    except (KeyboardInterrupt, SystemExit): 
        logger.info("程序被强制退出。")
    except Conflict:
        logger.error("机器人实例冲突！请确保只有一个使用此Token的实例在运行。")
