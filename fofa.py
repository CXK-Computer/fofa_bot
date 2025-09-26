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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, Conflict
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.request import Request

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

# --- 日志配置 (新增每日轮换) ---
# 新增功能：启动时检查日志文件，如果超过一天则进行轮换
if os.path.exists(LOG_FILE):
    try:
        file_mod_time = os.path.getmtime(LOG_FILE)
        # 如果文件修改时间超过24小时 (86400秒)
        if (time.time() - file_mod_time) > 86400:
            os.rename(LOG_FILE, LOG_FILE + f".{datetime.now().strftime('%Y-%m-%d')}.old")
            print("日志文件已超过一天，已轮换。")
    except (OSError, FileNotFoundError) as e:
        print(f"无法检查或轮换旧日志文件: {e}")

# 保留原有的基于大小的轮换作为备用方案
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
    # --- 核心修复：极其健壮的历史记录清理 ---
    sanitized_queries = []
    if HISTORY.get('queries') and isinstance(HISTORY['queries'], list):
        for q in HISTORY['queries']:
            # 过滤掉所有非字典类型的脏数据 (例如 null)
            if not isinstance(q, dict): continue

            is_valid = True
            cache_info = q.get('cache')
            if isinstance(cache_info, dict) and cache_info.get('cache_type') == 'local':
                # 如果是本地缓存，但路径无效或文件不存在，则标记为无效
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
def escape_markdown(text: str) -> str:
    escape_chars = '_*`[]()~>#+-=|{}.!'; return "".join(['\\' + char if char in escape_chars else char for char in text])

def restricted(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in CONFIG.get('admins', []):
            if update.message: await update.message.reply_text("⛔️ 抱歉，您没有权限。")
            return None
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- FOFA API 核心逻辑 ---
async def _make_request_async(url: str):
    proxy_str = ""
    if CONFIG.get("proxy"): proxy_str = f'--proxy "{CONFIG["proxy"]}"'
    command = f'curl -s -L -k {proxy_str} "{url}"'
    try:
        proc = await asyncio.create_subprocess_shell(command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0: return None, f"网络请求失败 (curl): {stderr.decode().strip()}"
        response_text = stdout.decode()
        if not response_text: return None, "API 返回了空响应。"
        data = json.loads(response_text)
        if data.get("error"): return None, data.get("errmsg", "未知的FOFA错误")
        return data, None
    except json.JSONDecodeError: return None, f"解析JSON响应失败: {response_text[:200]}"
    except Exception as e: return None, f"执行curl时发生意外错误: {e}"

async def verify_fofa_api(key):
    url = f"https://fofa.info/api/v1/info/my?key={key}"; return await _make_request_async(url)

async def fetch_fofa_data(key, query, page=1, page_size=10000, fields="host"):
    b64_query = base64.b64encode(query.encode('utf-8')).decode('utf-8')
    full_param = "&full=true" if CONFIG.get("full_mode", False) else ""
    url = f"https://fofa.info/api/v1/search/all?key={key}&qbase64={b64_query}&size={page_size}&page={page}&fields={fields}{full_param}"
    return await _make_request_async(url)

async def execute_query_with_fallback(query_func, preferred_key_index=None):
    if not CONFIG['apis']: return None, None, "没有配置任何API Key。"
    tasks = [verify_fofa_api(key) for key in CONFIG['apis']]
    results = await asyncio.gather(*tasks)
    valid_keys = [{'key': CONFIG['apis'][i], 'index': i + 1, 'is_vip': data.get('is_vip', False)} for i, (data, error) in enumerate(results) if not error and data]
    if not valid_keys: return None, None, "所有API Key均无效或验证失败。"
    prioritized_keys = sorted(valid_keys, key=lambda x: x['is_vip'], reverse=True)
    keys_to_try = prioritized_keys
    if preferred_key_index is not None:
        start_index = next((i for i, k in enumerate(prioritized_keys) if k['index'] == preferred_key_index), -1)
        if start_index != -1: keys_to_try = prioritized_keys[start_index:] + prioritized_keys[:start_index]
    last_error = "没有可用的API Key。"
    for key_info in keys_to_try:
        data, error = await query_func(key_info['key'])
        if not error: return data, key_info['index'], None
        last_error = error
        if "[820031]" in str(error): logger.warning(f"Key [#{key_info['index']}] F点余额不足，尝试下一个..."); continue
        return None, key_info['index'], error
    return None, None, f"所有Key均尝试失败，最后错误: {last_error}"

# --- 管理员命令 ---
@restricted
async def get_log_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if os.path.exists(LOG_FILE): await update.message.reply_document(document=open(LOG_FILE, 'rb'), caption="这是当前的机器人运行日志。")
    else: await update.message.reply_text("❌ 未找到日志文件。")

@restricted
async def shutdown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ **收到指令！**\n机器人正在安全关闭...", parse_mode=ParseMode.MARKDOWN)
    logger.info(f"接收到来自用户 {update.effective_user.id} 的关闭指令。")
    shutdown_event = context.bot_data.get('shutdown_event')
    if shutdown_event: shutdown_event.set()
    else: logger.error("无法找到 shutdown_event, 无法正常关闭。"); await update.message.reply_text("❌ 内部错误：无法触发关闭事件。")

# --- 核心命令 ---
@restricted
async def kkfofa_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args: await update.message.reply_text("用法: `/kkfofa [key编号] <查询语句>`"); return ConversationHandler.END
    key_index, query_text = None, " ".join(args)
    try:
        key_index = int(args[0])
        if not (1 <= key_index <= len(CONFIG['apis'])): await update.message.reply_text(f"❌ Key编号无效。"); return ConversationHandler.END
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
            [InlineKeyboardButton("🔍 全新搜索 (覆盖旧缓存)", callback_data='cache_newsearch')],
            [InlineKeyboardButton("❌ 取消", callback_data='cache_cancel')]
        ]
        
        await update.message.reply_text(f"{message_text}\n\n请选择操作：", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        return STATE_CACHE_CHOICE
        
    return await start_new_search(update, context)

# --- 其他命令 ---
@restricted
async def backup_config_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if os.path.exists(CONFIG_FILE): await update.effective_chat.send_document(document=open(CONFIG_FILE, 'rb'), caption="这是您当前的配置文件备份。")
    else: await update.effective_chat.send_message("❌ 找不到配置文件。")

@restricted
async def restore_config_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📥 要恢复配置，请直接将您的 `config.json` 备份文件作为文档发送给我。")

@restricted
async def receive_config_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CONFIG
    document = update.message.document
    if document.file_name != CONFIG_FILE: await update.message.reply_text(f"❌ 文件名错误，请确保上传的文件名为 `{CONFIG_FILE}`。"); return
    try:
        file = await document.get_file(); temp_file_path = f"{CONFIG_FILE}.tmp"; await file.download_to_drive(temp_file_path)
        with open(temp_file_path, 'r', encoding='utf-8') as f: json.load(f)
        os.replace(temp_file_path, CONFIG_FILE)
        CONFIG = load_json_file(CONFIG_FILE, {})
        await update.message.reply_text("✅ 配置已成功恢复！")
    except Exception as e:
        logger.error(f"恢复配置文件时出错: {e}"); await update.message.reply_text(f"❌ 恢复配置时发生意外错误: {e}")
        if os.path.exists(temp_file_path): os.remove(temp_file_path)

@restricted
async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_or_update_query(None)
    
    # 修复: 使用 update.effective_chat.send_message 兼容命令和回调
    if not HISTORY['queries']:
        await update.effective_chat.send_message("🕰️ 暂无缓存记录。")
        return

    message_text = "🕰️ *最近10条缓存记录:*\n"
    for i, query in enumerate(HISTORY['queries'][:10]):
        dt_utc = datetime.fromisoformat(query['timestamp'])
        dt_local = dt_utc.astimezone()
        time_str = dt_local.strftime('%Y-%m-%d %H:%M')
        cache_info = query.get('cache', {})
        message_text += f"\n`{i+1}.` **查询:** `{escape_markdown(query['query_text'])}`\n   _时间: {time_str} | 结果: {cache_info.get('result_count', 'N/A')} 条_\n"
    
    await update.effective_chat.send_message(message_text, parse_mode=ParseMode.MARKDOWN)

async def start_new_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_text = context.user_data['query']; key_index = context.user_data.get('key_index')
    add_or_update_query(query_text, cache_data=None) 
    message_able = update.callback_query.message if update.callback_query else update.message
    edit_func = message_able.edit_text if update.callback_query else (lambda text, **kwargs: message_able.reply_text(text, **kwargs))
    msg = await edit_func("🔄 正在执行全新查询...")
    data, used_key_index, error = await execute_query_with_fallback(lambda key: fetch_fofa_data(key, query_text, 1, 1, "host"), key_index)
    if error: await msg.edit_text(f"❌ 查询出错: {error}"); return ConversationHandler.END
    total_size = data.get('size', 0)
    if total_size == 0: await msg.edit_text("🤷‍♀️ 未找到结果。"); return ConversationHandler.END
    context.user_data.update({'total_size': total_size, 'chat_id': update.effective_chat.id})
    success_message = f"✅ 使用 Key [#{used_key_index}] 找到 {total_size} 条结果。"
    if total_size <= 10000:
        await msg.edit_text(f"{success_message}\n开始下载..."); 
        await start_download_job(context, run_full_download_query, context.user_data)
        return ConversationHandler.END
    else:
        keyboard = [[InlineKeyboardButton("💎 全部下载", callback_data='mode_full'), InlineKeyboardButton("🌀 深度追溯下载", callback_data='mode_traceback')], [InlineKeyboardButton("❌ 取消", callback_data='mode_cancel')]]
        await msg.edit_text(f"{success_message}\n请选择下载模式:", reply_markup=InlineKeyboardMarkup(keyboard))
        return STATE_KKFOFA_MODE

async def cache_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    user_data = context.user_data
    if not user_data.get('query'): await query.edit_message_text("❌ 会话已过期，请重新发起 /kkfofa 查询。"); return ConversationHandler.END
    choice = query.data.split('_')[1]
    if choice == 'newsearch': return await start_new_search(update, context)
    elif choice == 'incremental':
        await query.edit_message_text("⏳ 准备增量更新...")
        await start_download_job(context, run_incremental_update_query, user_data)
        return ConversationHandler.END
    elif choice == 'cancel': await query.edit_message_text("操作已取消。"); return ConversationHandler.END

async def start_download_job(context: ContextTypes.DEFAULT_TYPE, callback_func, job_data):
    chat_id = job_data.get('chat_id')
    if not chat_id: logger.error("start_download_job 失败: job_data 中缺少 'chat_id'。"); return
    job_name = f"download_job_{chat_id}"; [job.schedule_removal() for job in context.job_queue.get_jobs_by_name(job_name)]
    context.bot_data.pop(f'stop_job_{chat_id}', None)
    context.job_queue.run_once(callback_func, 1, data=job_data, name=job_name, chat_id=chat_id)
    
async def stop_all_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data[f'stop_job_{update.effective_chat.id}'] = True
    await update.message.reply_text("✅ 已发送停止信号。")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('👋 欢迎使用 Fofa 查询机器人！请使用 /help 查看命令手册。')
    if update.effective_user.id not in CONFIG.get('admins', []):
        CONFIG.setdefault('admins', []).append(update.effective_user.id); save_config()
        await update.message.reply_text("ℹ️ 已自动将您添加为管理员。")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = ( "📖 *Fofa 机器人指令手册*\n\n" 
                  "*🔍 资产查询*\n`/kkfofa [key编号] <查询语句>`\n\n" 
                  "*💾 数据管理*\n"
                  "`/history` - 查看缓存历史\n"
                  "`/backup` - 备份配置文件\n"
                  "`/restore` - 恢复配置文件\n\n"
                  "*⚙️ 管理与设置*\n`/settings` - 进入交互式设置菜单\n\n" 
                  "*💻 系统管理 (仅管理员)*\n"
                  "`/getlog` - 获取机器人运行日志\n"
                  "`/shutdown` - 安全关闭机器人\n\n"
                  "*🛑 任务控制*\n`/stop` - 紧急停止当前下载任务\n`/cancel` - 取消当前操作" )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

async def query_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    user_data = context.user_data
    if not user_data.get('query'): await query.edit_message_text("❌ 会话已过期，请重新发起 /kkfofa 查询。"); return ConversationHandler.END
    mode = query.data.split('_')[1]
    if mode == 'full': await query.edit_message_text(f"⏳ 开始全量下载任务..."); await start_download_job(context, run_full_download_query, user_data)
    elif mode == 'traceback': await query.edit_message_text(f"⏳ 开始深度追溯下载任务..."); await start_download_job(context, run_traceback_download_query, user_data)
    elif mode == 'cancel': await query.edit_message_text("操作已取消。")
    return ConversationHandler.END

# --- 设置菜单 ---
@restricted
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("🔑 API 管理", callback_data='settings_api')], [InlineKeyboardButton("🌐 代理设置", callback_data='settings_proxy')], [InlineKeyboardButton("💾 备份与恢复", callback_data='settings_backup')], [InlineKeyboardButton("🕰️ 查询历史", callback_data='settings_history')]]
    message_text = "⚙️ *设置菜单*"
    if update.callback_query: await update.callback_query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    else: await update.message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return STATE_SETTINGS_MAIN

async def settings_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); menu = query.data.split('_', 1)[1]
    if menu == 'api': await show_api_menu(update, context); return STATE_SETTINGS_ACTION
    elif menu == 'proxy': await show_proxy_menu(update, context); return STATE_SETTINGS_ACTION
    elif menu == 'backup': await show_backup_restore_menu(update, context); return STATE_SETTINGS_ACTION
    elif menu == 'history': 
        await history_command(update, context)
        await query.message.reply_text("返回设置主菜单:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data='settings_back_main')]]))
        return STATE_SETTINGS_MAIN

async def show_api_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await (update.callback_query.edit_message_text if update.callback_query else update.message.reply_text)("🔄 正在查询API Key状态...")
    tasks = [verify_fofa_api(key) for key in CONFIG['apis']]; results = await asyncio.gather(*tasks); api_details = []
    for i, (data, error) in enumerate(results):
        key_masked = f"`{CONFIG['apis'][i][:4]}...{CONFIG['apis'][i][-4:]}`"; status = f"❌ 无效或出错: {error}"
        if not error and data: status = f"({escape_markdown(data.get('username', 'N/A'))}, {'✅ VIP' if data.get('is_vip') else '👤 普通'}, F币: {data.get('fcoin', 0)})"
        api_details.append(f"{i+1}. {key_masked} {status}")
    api_message = "\n".join(api_details) if api_details else "目前没有存储任何API密钥。"
    keyboard = [[InlineKeyboardButton(f"时间范围: {'✅ 查询所有历史' if CONFIG.get('full_mode') else '⏳ 仅查近一年'}", callback_data='action_toggle_full')], [InlineKeyboardButton("➕ 添加Key", callback_data='action_add_api'), InlineKeyboardButton("➖ 删除Key", callback_data='action_remove_api')], [InlineKeyboardButton("🔙 返回主菜单", callback_data='action_back_main')]]
    await msg.edit_text(f"🔑 *API 管理*\n\n{api_message}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def show_proxy_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("✏️ 设置/更新", callback_data='action_set_proxy')], [InlineKeyboardButton("🗑️ 清除", callback_data='action_delete_proxy')], [InlineKeyboardButton("🔙 返回主菜单", callback_data='action_back_main')]]
    await update.callback_query.edit_message_text(f"🌐 *代理设置*\n当前: `{CONFIG.get('proxy') or '未设置'}`", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def show_backup_restore_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_text = ("💾 *备份与恢复*\n\n📤 *备份*\n点击下方按钮，或使用 /backup 命令。\n\n📥 *恢复*\n直接向机器人**发送** `config.json` 文件即可。")
    keyboard = [[InlineKeyboardButton("📤 立即备份", callback_data='action_backup_now')], [InlineKeyboardButton("🔙 返回主菜单", callback_data='action_back_main')]]
    await update.callback_query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def settings_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); action = query.data.split('_', 1)[1]
    if action == 'back_main': return await settings_command(update, context)
    elif action == 'toggle_full': CONFIG["full_mode"] = not CONFIG.get("full_mode", False); save_config(); await show_api_menu(update, context); return STATE_SETTINGS_ACTION
    elif action == 'add_api': await query.edit_message_text("请发送您的 Fofa API Key。"); return STATE_GET_KEY
    elif action == 'remove_api':
        if not CONFIG['apis']: await query.message.reply_text("没有可删除的API Key。"); await show_api_menu(update, context); return STATE_SETTINGS_ACTION
        await query.edit_message_text("请回复要删除的API Key编号。"); return STATE_REMOVE_API
    elif action == 'set_proxy': await query.edit_message_text("请输入代理地址。"); return STATE_GET_PROXY
    elif action == 'delete_proxy': CONFIG['proxy'] = ""; save_config(); await query.edit_message_text("✅ 代理已清除。"); await asyncio.sleep(1); return await settings_command(update, context)
    elif action == 'backup_now': await backup_config_command(update, context); return STATE_SETTINGS_ACTION

async def get_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = update.message.text.strip(); msg = await update.message.reply_text("正在验证...")
    data, error = await verify_fofa_api(key)
    if not error and data:
        if key not in CONFIG['apis']: CONFIG['apis'].append(key); save_config(); await msg.edit_text(f"✅ 添加成功！你好, {escape_markdown(data.get('username', 'user'))}!", parse_mode=ParseMode.MARKDOWN)
        else: await msg.edit_message_text(f"ℹ️ 该Key已存在。")
    else: await msg.edit_message_text(f"❌ 验证失败: {error}")
    await asyncio.sleep(2); await msg.delete(); await show_api_menu(update, context); return STATE_SETTINGS_ACTION

async def get_proxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    CONFIG['proxy'] = update.message.text.strip(); save_config()
    await update.message.reply_text(f"✅ 代理已更新。"); await asyncio.sleep(1); await settings_command(update, context); return STATE_SETTINGS_MAIN

async def remove_api(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        index = int(update.message.text) - 1
        if 0 <= index < len(CONFIG['apis']): CONFIG['apis'].pop(index); save_config(); await update.message.reply_text(f"✅ 已删除。")
        else: await update.message.reply_text("❌ 无效编号。")
    except (ValueError, IndexError): await update.message.reply_text("❌ 请输入数字。")
    await asyncio.sleep(1); await show_api_menu(update, context); return STATE_SETTINGS_ACTION

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('操作已取消。'); context.user_data.clear(); return ConversationHandler.END

# --- 核心文件处理与发送逻辑 ---
async def _save_and_send_results(bot, chat_id, query_text, results, msg):
    local_filename = f"fofa_cache_{hash(query_text) & 0xffffff}_{int(time.time())}.txt"
    local_file_path = os.path.join(LOCAL_CACHE_DIR, local_filename)
    with open(local_file_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(results))
    
    cache_data = {'cache_type': 'local', 'local_path': local_file_path, 'file_name': local_filename, 'result_count': len(results)}
    add_or_update_query(query_text, cache_data)
    
    file_size = os.path.getsize(local_file_path)
    if file_size <= TELEGRAM_BOT_UPLOAD_LIMIT:
        try:
            await msg.edit_text(f"✅ 下载完成！共 {len(results)} 条。\n💾 本地保存成功，正在发送至 Telegram...")
            # 修复: 增加发送文件的超时时间，防止因网络波动导致发送失败
            await bot.send_document(chat_id, document=open(local_file_path, 'rb'), read_timeout=60, write_timeout=60)
            await msg.edit_text(f"✅ 下载完成！共 {len(results)} 条。\n\n💾 结果已保存至服务器本地:\n`{escape_markdown(local_file_path)}`\n\n⬆️ 文件已成功发送！", parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"发送文件时发生未知错误: {e}")
            await msg.edit_text(f"✅ 下载完成！共 {len(results)} 条。\n\n💾 结果已保存至服务器本地:\n`{escape_markdown(local_file_path)}`\n\n❌ 文件发送失败: {e}", parse_mode=ParseMode.MARKDOWN)
    else:
        num_parts = (file_size + TELEGRAM_BOT_UPLOAD_LIMIT - 1) // TELEGRAM_BOT_UPLOAD_LIMIT
        await msg.edit_text(f"📦 文件大小为 {file_size/1024/1024:.2f} MB，超过45MB。\n正在分割成 {num_parts} 个文件并发送...")
        try:
            with open(local_file_path, 'r', encoding='utf-8') as f: lines = f.readlines()
            lines_per_part = (len(lines) + num_parts - 1) // num_parts
            for i in range(num_parts):
                await msg.edit_text(f"📦 正在发送第 {i+1}/{num_parts} 部分...")
                part_lines = lines[i*lines_per_part:(i+1)*lines_per_part]
                part_filename = f"part_{i+1}_of_{num_parts}_{os.path.basename(local_file_path)}"
                part_filepath = os.path.join(LOCAL_CACHE_DIR, part_filename)
                with open(part_filepath, 'w', encoding='utf-8') as pf: pf.writelines(part_lines)
                 # 修复: 增加发送文件的超时时间
                await bot.send_document(chat_id, document=open(part_filepath, 'rb'), read_timeout=60, write_timeout=60)
                os.remove(part_filepath)
            await msg.edit_text(f"✅ 所有 {num_parts} 个文件分卷已发送完毕！\n\n💾 完整文件保存在本地:\n`{escape_markdown(local_file_path)}`", parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"分割文件时出错: {e}")
            await msg.edit_text(f"❌ 处理文件分卷时发生错误: {e}")

async def run_full_download_query(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data; bot = context.bot; chat_id, query_text, total_size = job_data['chat_id'], job_data['query'], job_data['total_size']
    msg = await bot.send_message(chat_id, "⏳ 开始全量下载任务...")
    unique_results = set(); pages_to_fetch = (total_size + 9999) // 10000; stop_flag = f'stop_job_{chat_id}'
    for page in range(1, pages_to_fetch + 1):
        if context.bot_data.get(stop_flag): await msg.edit_text("🌀 下载任务已手动停止."); break
        try: await msg.edit_text(f"下载进度: {len(unique_results)}/{total_size} (Page {page}/{pages_to_fetch})...")
        except BadRequest: pass
        data, _, error = await execute_query_with_fallback(lambda key: fetch_fofa_data(key, query_text, page, 10000, "host"))
        if error: await msg.edit_text(f"❌ 第 {page} 页下载出错: {error}"); break
        if not data.get('results'): break
        unique_results.update(data.get('results', []))
    if unique_results: await _save_and_send_results(bot, chat_id, query_text, list(unique_results), msg)
    elif not context.bot_data.get(stop_flag): await msg.edit_text("🤷‍♀️ 任务完成，但未能下载到任何数据。")
    context.bot_data.pop(stop_flag, None)

async def run_traceback_download_query(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data; bot = context.bot; chat_id, base_query = job_data['chat_id'], job_data['query']
    msg = await bot.send_message(chat_id, "⏳ 开始深度追溯下载...")
    unique_results, page_count, last_page_date, termination_reason = set(), 0, None, ""
    current_query = base_query; stop_flag = f'stop_job_{chat_id}'
    while True:
        page_count += 1
        if context.bot_data.get(stop_flag): termination_reason = "\n\n🌀 任务已手动停止."; break
        data, _, error = await execute_query_with_fallback(lambda key: fetch_fofa_data(key, current_query, 1, 10000, "host,lastupdatetime"))
        if error: termination_reason = f"\n\n❌ 第 {page_count} 轮出错: {error}"; break
        results = data.get('results', [])
        if not results: termination_reason = f"\n\nℹ️ 已获取所有查询结果."; break
        original_count = len(unique_results); unique_results.update([r[0] for r in results if r and r[0]]); newly_added_count = len(unique_results) - original_count
        try: await msg.edit_text(f"⏳ 已找到 {len(unique_results)} 条... (第 {page_count} 轮, 新增 {newly_added_count})")
        except BadRequest: pass
        valid_anchor_found = False; outer_loop_break = False
        for i in range(len(results) - 1, -1, -1):
            if not results[i] or not results[i][0]: continue
            potential_anchor_host = results[i][0]
            anchor_host_data, _, _ = await execute_query_with_fallback(lambda key: fetch_fofa_data(key, f'host="{potential_anchor_host}"', 1, 1, "lastupdatetime"))
            try:
                ts_str = anchor_host_data.get('results', [])[0];
                if isinstance(ts_str, list): ts_str = ts_str[0]
                current_date_obj = datetime.strptime(ts_str.split(' ')[0], '%Y-%m-%d')
                if last_page_date and current_date_obj.date() >= last_page_date: continue
                next_page_date_obj = current_date_obj
                if last_page_date and current_date_obj.date() == last_page_date: next_page_date_obj -= timedelta(days=1)
                next_page_date_str = next_page_date_obj.strftime('%Y-%m-%d')
                if last_page_date and next_page_date_str == last_page_date.strftime('%Y-%m-%d') and newly_added_count == 0:
                    termination_reason = "\n\n⚠️ 日期未推进且无新数据，已达查询边界."; outer_loop_break = True; break
                last_page_date = current_date_obj.date(); current_query = f'({base_query}) && before="{next_page_date_str}"'; valid_anchor_found = True; break
            except (IndexError, TypeError, ValueError, AttributeError) as e: logger.warning(f"主机 {potential_anchor_host} 作为锚点无效: {e}..."); continue
        if outer_loop_break: break
        if not valid_anchor_found: termination_reason = "\n\n❌ 错误：无法找到有效的时间锚点以继续。"; break
    if unique_results:
        await msg.edit_text(f"✅ 深度追溯完成！共 {len(unique_results)} 条。{termination_reason}")
        await _save_and_send_results(bot, chat_id, base_query, list(unique_results), msg)
    else: await msg.edit_text(f"🤷‍♀️ 任务完成，但未能下载到任何数据。{termination_reason}")
    context.bot_data.pop(stop_flag, None)

async def run_incremental_update_query(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data; bot = context.bot; chat_id, base_query = job_data['chat_id'], job_data['query']
    msg = await bot.send_message(chat_id, "--- 增量更新启动 ---")
    cached_item = find_cached_query(base_query)
    if not cached_item: await msg.edit_text("❌ 错误：找不到有效的缓存项。"); return
    
    cache_info = cached_item['cache']; old_results = set(); local_path = cache_info.get('local_path')
    if not local_path or not os.path.exists(local_path): await msg.edit_text(f"❌ 错误: 本地缓存文件 `{local_path}` 已不存在。"); return

    await msg.edit_text("1/4: 正在读取本地缓存文件...")
    with open(local_path, 'r', encoding='utf-8') as f: old_results = set(line.strip() for line in f if line.strip())
    if not old_results: await msg.edit_text("❌ 错误: 缓存文件为空，无法更新。"); return
    
    await msg.edit_text("2/4: 正在确定更新起始点...")
    sample_size = min(20, len(old_results)); random_sample = random.sample(list(old_results), sample_size); latest_date = None
    for i, host in enumerate(random_sample):
        try:
            await msg.edit_text(f"2/4: 检查样本 {i+1}/{sample_size}...")
            data, _, _ = await execute_query_with_fallback(lambda key: fetch_fofa_data(key, f'host="{host}"', fields="lastupdatetime"))
            if data and data.get('results'):
                ts_str = data['results'][0];
                if isinstance(ts_str, list): ts_str = ts_str[0]
                current_date = datetime.strptime(ts_str.split(' ')[0], '%Y-%m-%d')
                if latest_date is None or current_date > latest_date: latest_date = current_date
        except Exception as e: logger.warning(f"无法获取主机 {host} 的时间戳: {e}"); continue
    
    if latest_date is None: await msg.edit_text("❌ 无法从缓存样本中获取有效的时间戳。"); return
    
    cutoff_date = latest_date.strftime('%Y-%m-%d'); incremental_query = f'({base_query}) && after="{cutoff_date}"'
    await msg.edit_text(f"3/4: 正在侦察自 {cutoff_date} 以来的新数据...")
    data, _, error = await execute_query_with_fallback(lambda key: fetch_fofa_data(key, incremental_query, page_size=1))
    if error: await msg.edit_text(f"❌ 侦察查询失败: {error}"); return
    
    total_new_size = data.get('size', 0)
    if total_new_size == 0: await msg.edit_text("✅ 未发现新数据。缓存已是最新。"); return
    
    new_results = set(); stop_flag = f'stop_job_{chat_id}'; pages_to_fetch = (total_new_size + 9999) // 10000
    for page in range(1, pages_to_fetch + 1):
        if context.bot_data.get(stop_flag): await msg.edit_text("🌀 增量更新已手动停止。"); return
        await msg.edit_text(f"3/4: 正在下载新数据... ( Page {page}/{pages_to_fetch} )")
        data, _, error = await execute_query_with_fallback(lambda key: fetch_fofa_data(key, incremental_query, page=page, page_size=10000))
        if error: await msg.edit_text(f"❌ 下载新数据失败: {error}"); return
        if data.get('results'): new_results.update(data.get('results', []))
        
    await msg.edit_text(f"4/4: 正在合并数据...")
    
    newly_added_results = new_results - old_results
    if newly_added_results:
        with open(local_path, 'a', encoding='utf-8') as f:
            for item in newly_added_results: f.write(f"\n{item}")
    
    final_results = list(old_results) + list(newly_added_results)
    
    await _save_and_send_results(bot, chat_id, base_query, final_results, msg)


async def main() -> None:
    # 警告：请务必将此处的 Token 替换为您自己的真实 Token
    # 建议使用环境变量或配置文件来管理 Token，避免硬编码在代码中
    TELEGRAM_BOT_TOKEN = "YOUR_REAL_TELEGRAM_BOT_TOKEN_HERE"
    
    # 修复: 增加网络超时时间以防止 "Timed out" 错误
    request = Request(connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0)
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).request(request).build()
    
    shutdown_event = asyncio.Event()
    application.bot_data['shutdown_event'] = shutdown_event

    settings_conv = ConversationHandler(entry_points=[CommandHandler("settings", settings_command)], states={STATE_SETTINGS_MAIN: [CallbackQueryHandler(settings_callback_handler, pattern=r"^settings_")], STATE_SETTINGS_ACTION: [CallbackQueryHandler(settings_action_handler, pattern=r"^action_")], STATE_GET_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_key)], STATE_GET_PROXY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_proxy)], STATE_REMOVE_API: [MessageHandler(filters.TEXT & ~filters.COMMAND, remove_api)],}, fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(settings_command, pattern=r"^settings_back_main$")])
    kkfofa_conv = ConversationHandler(entry_points=[CommandHandler("kkfofa", kkfofa_command)], states={STATE_CACHE_CHOICE: [CallbackQueryHandler(cache_choice_callback, pattern=r"^cache_")], STATE_KKFOFA_MODE: [CallbackQueryHandler(query_mode_callback, pattern=r"^mode_")],}, fallbacks=[CommandHandler("cancel", cancel)])
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stop", stop_all_tasks))
    application.add_handler(CommandHandler("backup", backup_config_command))
    application.add_handler(CommandHandler("restore", restore_config_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("getlog", get_log_command))
    application.add_handler(CommandHandler("shutdown", shutdown_command))
    application.add_handler(settings_conv)
    application.add_handler(kkfofa_conv)
    application.add_handler(MessageHandler(filters.Document.FileExtension("json"), receive_config_file))
    
    async with application:
        await application.bot.set_my_commands([ 
            BotCommand("start", "🚀 启动机器人"), 
            BotCommand("kkfofa", "🔍 资产搜索"), 
            BotCommand("settings", "⚙️ 设置"), 
            BotCommand("history", "🕰️ 查询历史"), 
            BotCommand("backup", "📤 备份配置"), 
            BotCommand("restore", "📥 恢复配置"), 
            BotCommand("getlog", "📄 获取日志"),
            BotCommand("shutdown", "🔌 关闭机器人"), 
            BotCommand("stop", "🛑 停止任务"), 
            BotCommand("help", "❓ 帮助"), 
            BotCommand("cancel", "❌ 取消操作")
        ])
        logger.info("🚀 机器人已启动...")
        await application.start()
        await application.updater.start_polling()
        await shutdown_event.wait()
        logger.info("正在停止 Updater..."); await application.updater.stop(); await asyncio.sleep(1) 
        logger.info("正在停止 Application..."); await application.stop()
    logger.info("机器人已安全关闭。")

if __name__ == '__main__':
    try: 
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): 
        logger.info("程序被强制退出。")
    except Conflict:
        logger.error("机器人实例冲突！请确保只有一个使用此Token的实例在运行。")
