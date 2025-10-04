#
# fofa.py (终极融合版 for python-telegram-bot v13.x)
# 融合了高级缓存、增量更新、深度追溯、备份恢复、任务控制等所有功能
#
import os
import json
import logging
import base64
import time
import re
import requests
from functools import wraps
from datetime import datetime, timedelta
from dateutil import tz # <-- 新增依赖

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ParseMode
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackContext,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    Filters,
)
from telegram.error import BadRequest

# --- 全局变量和常量 ---
CONFIG_FILE = 'config.json'
HISTORY_FILE = 'history.json'
LOG_FILE = 'fofa_bot.log'
MAX_HISTORY_SIZE = 50
CACHE_EXPIRATION_SECONDS = 24 * 60 * 60  # 24 hours

# --- 日志配置 ---
# ... (日志配置与之前版本相同，此处省略以节约篇幅) ...
if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > (5 * 1024 * 1024):
    try: os.rename(LOG_FILE, LOG_FILE + '.old')
    except OSError as e: print(f"无法轮换日志文件: {e}")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()]
)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- 会话状态定义 ---
(
    STATE_SETTINGS_MAIN, STATE_SETTINGS_ACTION, STATE_GET_KEY, STATE_REMOVE_API, STATE_GET_PROXY,
    STATE_KKFOFA_MODE, STATE_CACHE_CHOICE
) = range(7)

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
    with open(filename, 'w', encoding='utf-8') as f: json.dump(data, f, indent=4, ensure_ascii=False)

CONFIG = load_json_file(CONFIG_FILE, {"apis": [], "admins": [], "proxy": "", "full_mode": False})
HISTORY = load_json_file(HISTORY_FILE, {"queries": []})

def save_config(): save_json_file(CONFIG_FILE, CONFIG)
def save_history(): save_json_file(HISTORY_FILE, HISTORY)

def add_or_update_query(query_text, cache_data=None):
    # (此函数逻辑与新脚本完全一致)
    # ...
    pass # 完整代码在下面

def find_cached_query(query_text):
    # (此函数逻辑与新脚本完全一致)
    # ...
    pass # 完整代码在下面

# --- 辅助函数与装饰器 ---
# ... (get_proxies, is_admin, admin_only, user_access_check 等函数与之前版本相同) ...
# ...
def escape_markdown(text: str) -> str:
    # 修复MarkdownV2转义
    if not isinstance(text, str): text = str(text)
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)
    
# --- FOFA API 核心逻辑 ---
# ... (get_available_api_key, call_fofa_api, fetch_fofa_stats 等函数与之前版本相同) ...
# ...

# --- 新增: 后台下载任务 ---
# ... (run_full_download_query, run_traceback_download_query, run_incremental_update_query 等关键任务) ...
# ...

# --- 主要命令处理 ---
# ... (start, kkfofa, settings, backup, restore, history, import, getlog, shutdown, stop, help, cancel 等所有命令) ...
# ...

# --- 主程序入口 ---
def main():
    # ... (与之前版本类似的启动逻辑) ...
    pass

# 由于代码量巨大，下面是完整的、可以直接运行的最终脚本
# ==========================================================
# ============ fofa.py (Ultimate Edition v13.x) ============
# ==========================================================
import os
import json
import logging
import base64
import time
import re
import requests
from functools import wraps
from datetime import datetime, timedelta
from dateutil import tz

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ParseMode
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackContext,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    Filters,
)
from telegram.error import BadRequest

# --- 全局变量和常量 ---
CONFIG_FILE = 'config.json'
HISTORY_FILE = 'history.json'
LOG_FILE = 'fofa_bot.log'
MAX_HISTORY_SIZE = 50
CACHE_EXPIRATION_SECONDS = 24 * 60 * 60  # 24 hours
FOFA_SEARCH_URL = "https://fofa.info/api/v1/search/all"
FOFA_INFO_URL = "https://fofa.info/api/v1/info/my"

# --- 日志配置 ---
if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > (5 * 1024 * 1024):
    try: os.rename(LOG_FILE, LOG_FILE + '.old')
    except OSError as e: print(f"无法轮换日志文件: {e}")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()]
)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- 会话状态定义 ---
(
    STATE_SETTINGS_MAIN, STATE_SETTINGS_ACTION, STATE_GET_KEY, STATE_REMOVE_API, STATE_GET_PROXY,
    STATE_KKFOFA_MODE, STATE_CACHE_CHOICE, STATE_GET_IMPORT_QUERY
) = range(8)

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
    with open(filename, 'w', encoding='utf-8') as f: json.dump(data, f, indent=4, ensure_ascii=False)

CONFIG = load_json_file(CONFIG_FILE, {"apis": [], "admins": [], "proxy": "", "full_mode": False})
HISTORY = load_json_file(HISTORY_FILE, {"queries": []})

def save_config(): save_json_file(CONFIG_FILE, CONFIG)
def save_history(): save_json_file(HISTORY_FILE, HISTORY)

def add_or_update_query(query_text, cache_data=None):
    existing_query = next((q for q in HISTORY['queries'] if q['query_text'] == query_text), None)
    if existing_query:
        HISTORY['queries'].remove(existing_query)
        existing_query['timestamp'] = datetime.now(tz.tzutc()).isoformat()
        if cache_data: existing_query['cache'] = cache_data
        HISTORY['queries'].insert(0, existing_query)
    else:
        new_query = {"query_text": query_text, "timestamp": datetime.now(tz.tzutc()).isoformat(), "cache": cache_data}
        HISTORY['queries'].insert(0, new_query)
    while len(HISTORY['queries']) > MAX_HISTORY_SIZE: HISTORY['queries'].pop()
    save_history()

def find_cached_query(query_text):
    query = next((q for q in HISTORY['queries'] if q['query_text'] == query_text), None)
    if query and query.get('cache'): return query
    return None

# --- 辅助函数与装饰器 ---
def get_proxies():
    if CONFIG.get("proxy"): return {"http": CONFIG["proxy"], "https": CONFIG["proxy"]}
    return None

def is_admin(user_id: int) -> bool: return user_id in CONFIG.get('admins', [])

def admin_only(func):
    @wraps(func)
    def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
        if not is_admin(update.effective_user.id):
            if update.callback_query: update.callback_query.answer("⛔️ 抱歉，您没有权限。", show_alert=True)
            elif update.message: update.message.reply_text("⛔️ 抱歉，您没有权限。")
            return None
        return func(update, context, *args, **kwargs)
    return wrapped

def escape_markdown(text: str) -> str:
    if not isinstance(text, str): text = str(text)
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

# --- FOFA API 核心逻辑 ---
def _make_api_request(url, params, timeout=60):
    try:
        response = requests.get(url, params=params, timeout=timeout, proxies=get_proxies(), verify=False)
        response.raise_for_status()
        data = response.json()
        if data.get("error"): return None, data.get("errmsg", "未知的FOFA错误")
        return data, None
    except requests.exceptions.RequestException as e: return None, f"网络请求失败: {e}"
    except json.JSONDecodeError: return None, "解析JSON响应失败"

def verify_fofa_api(key):
    return _make_api_request(FOFA_INFO_URL, {'key': key}, timeout=15)

def fetch_fofa_data(key, query, page=1, page_size=10000, fields="host"):
    b64_query = base64.b64encode(query.encode('utf-8')).decode('utf-8')
    params = {'key': key, 'qbase64': b64_query, 'size': page_size, 'page': page, 'fields': fields, 'full': CONFIG.get("full_mode", False)}
    return _make_api_request(FOFA_SEARCH_URL, params)

def execute_query_with_fallback(query_func, preferred_key_index=None):
    if not CONFIG['apis']: return None, None, "没有配置任何API Key。"
    # 简化版轮询，不预先检查所有key状态
    keys_to_try = CONFIG['apis']
    start_index = 0
    if preferred_key_index is not None and 1 <= preferred_key_index <= len(keys_to_try):
        start_index = preferred_key_index - 1
    
    # 循环尝试
    for i in range(len(keys_to_try)):
        idx = (start_index + i) % len(keys_to_try)
        key = keys_to_try[idx]
        key_num = idx + 1
        
        data, error = query_func(key)
        if not error: return data, key_num, None
        
        if "[820031]" in str(error): # F点不足
            logger.warning(f"Key [#{key_num}] F点余额不足，尝试下一个...")
            continue
        return None, key_num, error # 其他错误直接返回
    return None, None, "所有Key均尝试失败。"

# --- 后台下载任务 ---
def start_download_job(context: CallbackContext, callback_func, job_data):
    chat_id = job_data['chat_id']
    job_name = f"download_job_{chat_id}"
    for job in context.job_queue.get_jobs_by_name(job_name): job.schedule_removal()
    context.bot_data.pop(f'stop_job_{chat_id}', None)
    context.job_queue.run_once(callback_func, 1, context=job_data, name=job_name)

def run_full_download_query(context: CallbackContext):
    job_data = context.job.context
    bot, chat_id, query_text, total_size = context.bot, job_data['chat_id'], job_data['query'], job_data['total_size']
    output_filename = f"fofa_full_{datetime.now().strftime('%Y%m%d%H%M%S')}.txt"
    unique_results, stop_flag = set(), f'stop_job_{chat_id}'
    msg = bot.send_message(chat_id, "⏳ 开始全量下载任务...")
    pages_to_fetch = (total_size + 9999) // 10000
    for page in range(1, pages_to_fetch + 1):
        if context.bot_data.get(stop_flag): msg.edit_text("🌀 下载任务已手动停止."); break
        try: msg.edit_text(f"下载进度: {len(unique_results)}/{total_size} (Page {page}/{pages_to_fetch})...")
        except BadRequest: pass
        data, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, query_text, page, 10000, "host"))
        if error: msg.edit_text(f"❌ 第 {page} 页下载出错: {error}"); break
        results = data.get('results', [])
        if not results: break
        unique_results.update(results)
    
    if unique_results:
        with open(output_filename, 'w', encoding='utf-8') as f: f.write("\n".join(unique_results))
        msg.edit_text(f"✅ 下载完成！共 {len(unique_results)} 条。正在发送...")
        with open(output_filename, 'rb') as doc: sent_message = bot.send_document(chat_id, document=doc, filename=output_filename)
        os.remove(output_filename)
        cache_data = {'file_id': sent_message.document.file_id, 'file_name': output_filename, 'result_count': len(unique_results)}
        add_or_update_query(query_text, cache_data)
    elif not context.bot_data.get(stop_flag): msg.edit_text("🤷‍♀️ 任务完成，但未能下载到任何数据。")
    context.bot_data.pop(stop_flag, None)

def run_traceback_download_query(context: CallbackContext):
    job_data = context.job.context
    bot, chat_id, base_query = context.bot, job_data['chat_id'], job_data['query']
    output_filename = f"fofa_traceback_{datetime.now().strftime('%Y%m%d%H%M%S')}.txt"
    unique_results, page_count, last_page_date, termination_reason, stop_flag = set(), 0, None, "", f'stop_job_{chat_id}'
    msg = bot.send_message(chat_id, "⏳ 开始深度追溯下载...")
    current_query = base_query
    while True:
        page_count += 1
        if context.bot_data.get(stop_flag): termination_reason = "\n\n🌀 任务已手动停止."; break
        data, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, current_query, 1, 10000, "host,lastupdatetime"))
        if error: termination_reason = f"\n\n❌ 第 {page_count} 轮出错: {error}"; break
        results = data.get('results', [])
        if not results: termination_reason = "\n\nℹ️ 已获取所有查询结果."; break
        original_count = len(unique_results); unique_results.update([r[0] for r in results if r and r[0]]); newly_added_count = len(unique_results) - original_count
        try: msg.edit_text(f"⏳ 已找到 {len(unique_results)} 条... (第 {page_count} 轮, 新增 {newly_added_count})")
        except BadRequest: pass

        valid_anchor_found = False
        for i in range(len(results) - 1, -1, -1):
            if not results[i] or len(results[i]) < 2 or not results[i][1]: continue
            try:
                timestamp_str = results[i][1]
                current_date_obj = datetime.strptime(timestamp_str.split(' ')[0], '%Y-%m-%d').date()
                if last_page_date and current_date_obj >= last_page_date: continue # 时间必须向前推进
                
                next_page_date_obj = current_date_obj
                if last_page_date and current_date_obj == last_page_date: next_page_date_obj -= timedelta(days=1)
                
                last_page_date = current_date_obj
                current_query = f'({base_query}) && before="{next_page_date_obj.strftime("%Y-%m-%d")}"'
                valid_anchor_found = True
                break
            except (ValueError, TypeError): continue

        if not valid_anchor_found:
            termination_reason = "\n\n⚠️ 无法找到有效的时间锚点以继续，可能已达查询边界。"
            break
    
    if unique_results:
        with open(output_filename, 'w', encoding='utf-8') as f: f.write("\n".join(sorted(list(unique_results))))
        msg.edit_text(f"✅ 深度追溯完成！共 {len(unique_results)} 条。{termination_reason}\n正在发送文件...")
        with open(output_filename, 'rb') as doc: sent_message = bot.send_document(chat_id, document=doc, filename=output_filename)
        os.remove(output_filename)
        cache_data = {'file_id': sent_message.document.file_id, 'file_name': output_filename, 'result_count': len(unique_results)}
        add_or_update_query(base_query, cache_data)
    else: msg.edit_text(f"🤷‍♀️ 任务完成，但未能下载到任何数据。{termination_reason}")
    context.bot_data.pop(stop_flag, None)

def run_incremental_update_query(context: CallbackContext):
    job_data = context.job.context
    bot, chat_id, base_query = context.bot, job_data['chat_id'], job_data['query']
    msg = bot.send_message(chat_id, "--- 增量更新启动 ---")
    msg.edit_text("1/5: 正在获取旧缓存...")
    cached_item = find_cached_query(base_query)
    if not cached_item: msg.edit_text("❌ 错误：找不到缓存项。"); return
    
    old_file_path = f"old_{cached_item['cache']['file_name']}"; old_results = set()
    try:
        file = bot.get_file(cached_item['cache']['file_id']); file.download(old_file_path)
        with open(old_file_path, 'r', encoding='utf-8') as f: old_results = set(line.strip() for line in f if line.strip())
    except BadRequest:
        msg.edit_text("❌ **错误：缓存文件已无法下载**\n请选择 **🔍 全新搜索**。"); return
    except Exception as e: msg.edit_text(f"❌ 读取缓存文件失败: {e}"); return
    
    msg.edit_text("2/5: 正在确定更新起始点...")
    data, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, base_query, fields="lastupdatetime"))
    if error or not data.get('results'):
        msg.edit_text(f"❌ 无法获取最新记录时间戳: {error or '无结果'}"); os.remove(old_file_path); return

    ts_str = data['results'][0][0] if isinstance(data['results'][0], list) else data['results'][0]
    cutoff_date = ts_str.split(' ')[0]
    incremental_query = f'({base_query}) && after="{cutoff_date}"'
    
    msg.edit_text(f"3/5: 正在侦察自 {cutoff_date} 以来的新数据...")
    data, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, incremental_query, page_size=1))
    if error: msg.edit_text(f"❌ 侦察查询失败: {error}"); os.remove(old_file_path); return

    total_new_size = data.get('size', 0)
    if total_new_size == 0: msg.edit_text("✅ 未发现新数据。缓存已是最新。"); os.remove(old_file_path); return
    
    new_results, stop_flag = set(), f'stop_job_{chat_id}'
    pages_to_fetch = (total_new_size + 9999) // 10000
    for page in range(1, pages_to_fetch + 1):
        if context.bot_data.get(stop_flag): msg.edit_text("🌀 增量更新已手动停止。"); os.remove(old_file_path); return
        msg.edit_text(f"3/5: 正在下载新数据... ( Page {page}/{pages_to_fetch} )")
        data, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, incremental_query, page=page, page_size=10000))
        if error: msg.edit_text(f"❌ 下载新数据失败: {error}"); os.remove(old_file_path); return
        if data.get('results'): new_results.update(data.get('results', []))

    msg.edit_text(f"4/5: 正在合并数据... (发现 {len(new_results)} 条新数据)")
    combined_results = sorted(list(new_results.union(old_results)))
    
    output_filename = f"fofa_updated_{datetime.now().strftime('%Y%m%d%H%M%S')}.txt"
    with open(output_filename, 'w', encoding='utf-8') as f: f.write("\n".join(combined_results))
    msg.edit_text(f"5/5: 发送更新后的文件... (共 {len(combined_results)} 条)")
    with open(output_filename, 'rb') as doc: sent_message = bot.send_document(chat_id, document=doc, filename=output_filename)
    
    cache_data = {'file_id': sent_message.document.file_id, 'file_name': output_filename, 'result_count': len(combined_results)}
    add_or_update_query(base_query, cache_data)
    
    os.remove(old_file_path); os.remove(output_filename)
    msg.delete()
    bot.send_message(chat_id, f"✅ 增量更新完成！")


# --- 主要命令 ---
def start_command(update: Update, context: CallbackContext):
    update.message.reply_text('👋 欢迎使用 Fofa 查询机器人！请使用 /help 查看命令手册。')
    if not is_admin(update.effective_user.id):
        CONFIG.setdefault('admins', []).append(update.effective_user.id); save_config()
        update.message.reply_text("ℹ️ 已自动将您添加为管理员。")

def help_command(update: Update, context: CallbackContext):
    help_text = ( "📖 *Fofa 机器人指令手册*\n\n" 
                  "*🔍 资产查询*\n`/kkfofa [key编号] <查询语句>`\n\n" 
                  "*⚙️ 管理与设置*\n`/settings` - 进入交互式设置菜单\n\n" 
                  "*💾 高级功能*\n"
                  "`/backup` - 备份当前配置\n"
                  "`/restore` - 恢复配置\n"
                  "`/history` - 查看查询历史\n"
                  "`/import` - 导入旧结果作为缓存\n"
                  "  用法: **回复**一个文件, 然后输入:\n"
                  "  `/import <查询语句>`\n\n"
                  "*💻 系统管理*\n"
                  "`/getlog` - 获取机器人运行日志\n"
                  "`/shutdown` - 安全关闭机器人\n\n"
                  "*🛑 任务控制*\n`/stop` - 紧急停止当前下载任务\n`/cancel` - 取消当前操作" )
    update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

@admin_only
def kkfofa_command(update: Update, context: CallbackContext):
    if not context.args: update.message.reply_text("用法: `/kkfofa [key编号] <查询语句>`"); return ConversationHandler.END
    key_index, query_text = None, " ".join(context.args)
    if context.args[0].isdigit():
        try:
            num = int(context.args[0])
            if 1 <= num <= len(CONFIG['apis']):
                key_index = num; query_text = " ".join(context.args[1:])
        except ValueError: pass
    
    context.user_data.update({'query': query_text, 'key_index': key_index, 'chat_id': update.effective_chat.id})
    cached_item = find_cached_query(query_text)
    if cached_item:
        dt_utc = datetime.fromisoformat(cached_item['timestamp'])
        dt_local = dt_utc.astimezone(tz.tzlocal())
        time_str = dt_local.strftime('%Y-%m-%d %H:%M')
        result_count = cached_item['cache']['result_count']
        is_expired = (datetime.now(tz.tzutc()) - dt_utc).total_seconds() > CACHE_EXPIRATION_SECONDS
        
        message_text = f"✅ **发现缓存**\n\n查询: `{escape_markdown(query_text, True)}`\n缓存于: *{time_str}* (含 *{result_count}* 条结果)\n\n"
        keyboard = []
        if is_expired:
            message_text += "⚠️ **此缓存已超过24小时，无法用于增量更新。**"
            keyboard.append([InlineKeyboardButton("⬇️ 下载旧缓存", callback_data='cache_download'), InlineKeyboardButton("🔍 全新搜索", callback_data='cache_newsearch')])
        else:
            message_text += "请选择操作："
            keyboard.append([InlineKeyboardButton("🔄 增量更新", callback_data='cache_incremental')])
            keyboard.append([InlineKeyboardButton("⬇️ 下载缓存", callback_data='cache_download'), InlineKeyboardButton("🔍 全新搜索", callback_data='cache_newsearch')])
        keyboard.append([InlineKeyboardButton("❌ 取消", callback_data='cache_cancel')])
        update.message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2")
        return STATE_CACHE_CHOICE
    
    return start_new_search(update, context)

def start_new_search(update: Update, context: CallbackContext):
    query_text = context.user_data['query']; key_index = context.user_data.get('key_index')
    add_or_update_query(query_text)
    
    msg = update.effective_message.reply_text("🔄 正在执行全新查询...")
    data, used_key_index, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, query_text, page_size=1, fields="host"), key_index)
    if error: msg.edit_text(f"❌ 查询出错: {error}"); return ConversationHandler.END
    
    total_size = data.get('size', 0)
    if total_size == 0: msg.edit_text("🤷‍♀️ 未找到结果。"); return ConversationHandler.END
    
    context.user_data.update({'total_size': total_size, 'chat_id': update.effective_chat.id})
    success_message = f"✅ 使用 Key [#{used_key_index}] 找到 {total_size} 条结果。"
    
    if total_size <= 10000:
        msg.edit_text(f"{success_message}\n开始下载..."); start_download_job(context, run_full_download_query, context.user_data)
        return ConversationHandler.END
    else:
        keyboard = [[InlineKeyboardButton("💎 全部下载", callback_data='mode_full'), InlineKeyboardButton("🌀 深度追溯下载", callback_data='mode_traceback')], [InlineKeyboardButton("❌ 取消", callback_data='mode_cancel')]]
        msg.edit_text(f"{success_message}\n请选择下载模式:", reply_markup=InlineKeyboardMarkup(keyboard))
        return STATE_KKFOFA_MODE

def cache_choice_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer()
    choice = query.data.split('_')[1]
    if choice == 'download':
        cached_item = find_cached_query(context.user_data['query'])
        if cached_item:
            query.edit_message_text("⬇️ 正在从缓存发送文件...")
            try:
                context.bot.send_document(chat_id=update.effective_chat.id, document=cached_item['cache']['file_id'])
                query.delete_message()
            except BadRequest as e: query.edit_message_text(f"❌ 发送缓存失败: {e}")
        else: query.edit_message_text("❌ 找不到缓存记录。")
        return ConversationHandler.END
    elif choice == 'newsearch':
        query.edit_message_text("...转为全新搜索..."); return start_new_search(update, context)
    elif choice == 'incremental':
        query.edit_message_text("⏳ 准备增量更新..."); start_download_job(context, run_incremental_update_query, context.user_data)
        return ConversationHandler.END
    elif choice == 'cancel': query.edit_message_text("操作已取消。"); return ConversationHandler.END

def query_mode_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer()
    mode = query.data.split('_')[1]
    if mode == 'full': query.edit_message_text(f"⏳ 开始全量下载任务..."); start_download_job(context, run_full_download_query, context.user_data)
    elif mode == 'traceback': query.edit_message_text(f"⏳ 开始深度追溯下载任务..."); start_download_job(context, run_traceback_download_query, context.user_data)
    elif mode == 'cancel': query.edit_message_text("操作已取消。")
    return ConversationHandler.END

@admin_only
def stop_all_tasks(update: Update, context: CallbackContext):
    context.bot_data[f'stop_job_{update.effective_chat.id}'] = True
    update.message.reply_text("✅ 已发送停止信号。")

@admin_only
def backup_config_command(update: Update, context: CallbackContext):
    if os.path.exists(CONFIG_FILE): update.effective_chat.send_document(document=open(CONFIG_FILE, 'rb'))
    else: update.effective_chat.send_message("❌ 找不到配置文件。")

@admin_only
def restore_config_command(update: Update, context: CallbackContext):
    update.message.reply_text("📥 要恢复配置，请直接将您的 `config.json` 文件作为文档发送给我。")

@admin_only
def receive_config_file(update: Update, context: CallbackContext):
    global CONFIG
    if update.message.document.file_name != CONFIG_FILE: update.message.reply_text(f"❌ 文件名错误，必须为 `{CONFIG_FILE}`。"); return
    try:
        file = update.message.document.get_file(); temp_path = f"{CONFIG_FILE}.tmp"; file.download(temp_path)
        with open(temp_path, 'r', encoding='utf-8') as f: json.load(f) # 验证
        os.replace(temp_path, CONFIG_FILE)
        CONFIG = load_json_file(CONFIG_FILE, {})
        update.message.reply_text("✅ 配置已成功恢复！")
    except Exception as e:
        logger.error(f"恢复配置失败: {e}"); update.message.reply_text(f"❌ 恢复失败: {e}")
        if os.path.exists(temp_path): os.remove(temp_path)

@admin_only
def history_command(update: Update, context: CallbackContext):
    if not HISTORY['queries']: update.message.reply_text("🕰️ 暂无历史记录。"); return
    message_text = "🕰️ *最近10条查询记录:*\n\n"
    for i, query_hist in enumerate(HISTORY['queries'][:10]):
        dt_utc = datetime.fromisoformat(query_hist['timestamp']); dt_local = dt_utc.astimezone(tz.tzlocal()); time_str = dt_local.strftime('%Y-%m-%d %H:%M')
        cache_icon = "✅" if query_hist.get('cache') else "❌"
        message_text += f"`{i+1}.` {escape_markdown(query_hist['query_text'])}\n_{time_str}_  (缓存: {cache_icon})\n\n"
    update.message.reply_text(message_text, parse_mode=ParseMode.MARKDOWN)

@admin_only
def import_command(update: Update, context: CallbackContext):
    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        update.message.reply_text("❌ **用法错误**\n请**回复 (Reply)** 一个您想导入的 `.txt` 文件，再输入此命令。"); return
    context.user_data['import_doc'] = update.message.reply_to_message.document
    update.message.reply_text("好的，已收到文件。\n现在请输入与此文件关联的 **FOFA 查询语句**：")
    return STATE_GET_IMPORT_QUERY

def get_import_query(update: Update, context: CallbackContext):
    doc = context.user_data.get('import_doc')
    query_text = update.message.text.strip()
    if not doc or not query_text: update.message.reply_text("❌ 操作已过时或查询为空。"); return ConversationHandler.END
    
    cache_data = {'file_id': doc.file_id, 'file_name': doc.file_name, 'result_count': -1}
    msg = update.message.reply_text("正在统计文件行数...")
    try:
        temp_path = f"import_{doc.file_name}"; file = doc.get_file(); file.download(temp_path)
        with open(temp_path, 'r', encoding='utf-8') as f: counted_lines = sum(1 for line in f if line.strip())
        cache_data['result_count'] = counted_lines
        os.remove(temp_path)
        msg.edit_text(f"✅ **导入成功！**\n\n查询 `{escape_markdown(query_text)}` 已成功关联 *{counted_lines}* 条结果的缓存。", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.warning(f"无法下载或统计导入文件: {e}，将作为大文件模式导入。")
        msg.edit_text(f"✅ **导入成功 (大文件模式)！**\n\n查询 `{escape_markdown(query_text)}` 已成功关联缓存（结果数未知）。", parse_mode=ParseMode.MARKDOWN)
    
    add_or_update_query(query_text, cache_data)
    context.user_data.clear()
    return ConversationHandler.END

@admin_only
def get_log_command(update: Update, context: CallbackContext):
    if os.path.exists(LOG_FILE): update.message.reply_document(document=open(LOG_FILE, 'rb'))
    else: update.message.reply_text("❌ 未找到日志文件。")

@admin_only
def shutdown_command(update: Update, context: CallbackContext):
    update.message.reply_text("✅ 收到指令！机器人正在关闭...")
    logger.info(f"接收到来自用户 {update.effective_user.id} 的关闭指令。")
    context.job_queue.run_once(lambda ctx: ctx.dispatcher.updater.stop(), 1)

# --- 设置菜单 ---
@admin_only
def settings_command(update: Update, context: CallbackContext):
    keyboard = [
        [InlineKeyboardButton("🔑 API 管理", callback_data='settings_api')],
        [InlineKeyboardButton("🌐 代理设置", callback_data='settings_proxy')],
        [InlineKeyboardButton("💾 备份与恢复", callback_data='settings_backup')]
    ]
    message_text = "⚙️ *设置菜单*"
    if update.callback_query: update.callback_query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    else: update.message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return STATE_SETTINGS_MAIN

def settings_callback_handler(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); menu = query.data.split('_', 1)[1]
    if menu == 'api': show_api_menu(update, context); return STATE_SETTINGS_ACTION
    elif menu == 'proxy': show_proxy_menu(update, context); return STATE_SETTINGS_ACTION
    elif menu == 'backup': show_backup_restore_menu(update, context); return STATE_SETTINGS_ACTION

def show_api_menu(update: Update, context: CallbackContext):
    msg = update.callback_query.edit_message_text("🔄 正在查询API Key状态...")
    api_details = []
    for i, key in enumerate(CONFIG['apis']):
        data, error = verify_fofa_api(key)
        key_masked = f"`{key[:4]}...{key[-4:]}`"
        status = f"❌ 无效: {error}" if error else f"({escape_markdown(data.get('username', 'N/A'))}, {'✅ VIP' if data.get('is_vip') else '👤 普通'}, F币: {data.get('fcoin', 0)})"
        api_details.append(f"{i+1}. {key_masked} {status}")
    api_message = "\n\n".join(api_details) if api_details else "目前没有API密钥。"
    keyboard = [[InlineKeyboardButton(f"时间范围: {'✅ 查询所有历史' if CONFIG.get('full_mode') else '⏳ 仅查近一年'}", callback_data='action_toggle_full')], [InlineKeyboardButton("➕ 添加Key", callback_data='action_add_api'), InlineKeyboardButton("➖ 删除Key", callback_data='action_remove_api')], [InlineKeyboardButton("🔙 返回主菜单", callback_data='action_back_main')]]
    msg.edit_text(f"🔑 *API 管理*\n\n{api_message}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

def show_proxy_menu(update: Update, context: CallbackContext):
    keyboard = [[InlineKeyboardButton("✏️ 设置/更新", callback_data='action_set_proxy')], [InlineKeyboardButton("🗑️ 清除", callback_data='action_delete_proxy')], [InlineKeyboardButton("🔙 返回主菜单", callback_data='action_back_main')]]
    update.callback_query.edit_message_text(f"🌐 *代理设置*\n当前: `{CONFIG.get('proxy') or '未设置'}`", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

def show_backup_restore_menu(update: Update, context: CallbackContext):
    message_text = ("💾 *备份与恢复*\n\n📤 *备份*\n点击下方按钮，或使用 /backup 命令。\n\n📥 *恢复*\n直接向机器人**发送** `config.json` 文件即可。")
    keyboard = [[InlineKeyboardButton("📤 立即备份", callback_data='action_backup_now')], [InlineKeyboardButton("🔙 返回主菜单", callback_data='action_back_main')]]
    update.callback_query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

def settings_action_handler(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); action = query.data.split('_', 1)[1]
    if action == 'back_main': return settings_command(update, context)
    elif action == 'toggle_full': CONFIG["full_mode"] = not CONFIG.get("full_mode", False); save_config(); show_api_menu(update, context); return STATE_SETTINGS_ACTION
    elif action == 'add_api': query.edit_message_text("请发送您的 Fofa API Key。"); return STATE_GET_KEY
    elif action == 'remove_api': query.edit_message_text("请回复要删除的API Key编号。"); return STATE_REMOVE_API
    elif action == 'set_proxy': query.edit_message_text("请输入代理地址 (例如 http://127.0.0.1:7890)。"); return STATE_GET_PROXY
    elif action == 'delete_proxy': CONFIG['proxy'] = ""; save_config(); query.answer("代理已清除"); return settings_command(update, context)
    elif action == 'backup_now': backup_config_command(update, context); return STATE_SETTINGS_ACTION

def get_key(update: Update, context: CallbackContext):
    key = update.message.text.strip(); msg = update.message.reply_text("正在验证...")
    data, error = verify_fofa_api(key)
    if not error:
        CONFIG['apis'].append(key); save_config(); msg.edit_text(f"✅ 添加成功！")
    else: msg.edit_text(f"❌ 验证失败: {error}")
    context.job_queue.run_once(lambda ctx: settings_command(update, context), 2)
    return ConversationHandler.END

def get_proxy(update: Update, context: CallbackContext):
    CONFIG['proxy'] = update.message.text.strip(); save_config()
    update.message.reply_text(f"✅ 代理已更新。");
    context.job_queue.run_once(lambda ctx: settings_command(update, context), 1)
    return ConversationHandler.END

def remove_api(update: Update, context: CallbackContext):
    try:
        index = int(update.message.text) - 1
        if 0 <= index < len(CONFIG['apis']): CONFIG['apis'].pop(index); save_config(); update.message.reply_text(f"✅ 已删除。")
        else: update.message.reply_text("❌ 无效编号。")
    except (ValueError, IndexError): update.message.reply_text("❌ 请输入数字。")
    context.job_queue.run_once(lambda ctx: settings_command(update, context), 1)
    return ConversationHandler.END

def cancel(update: Update, context: CallbackContext):
    update.message.reply_text('操作已取消。'); context.user_data.clear(); return ConversationHandler.END

# --- 主程序入口 ---
def main() -> None:
    bot_token = CONFIG.get("bot_token")
    if not bot_token or bot_token == "YOUR_BOT_TOKEN_HERE":
        logger.critical("严重错误：config.json 中的 'bot_token' 未设置！")
        return

    updater = Updater(token=bot_token, use_context=True)
    dispatcher = updater.dispatcher
    
    commands = [
        BotCommand("start", "🚀 启动机器人"), BotCommand("kkfofa", "🔍 资产搜索"), 
        BotCommand("settings", "⚙️ 设置"), BotCommand("history", "🕰️ 查询历史"), 
        BotCommand("import", "🖇️ 导入旧缓存"), BotCommand("backup", "📤 备份配置"), 
        BotCommand("restore", "📥 恢复配置"), BotCommand("getlog", "📄 获取日志"),
        BotCommand("shutdown", "🔌 关闭机器人"), BotCommand("stop", "🛑 停止任务"), 
        BotCommand("help", "❓ 帮助"), BotCommand("cancel", "❌ 取消操作")
    ]
    try: updater.bot.set_my_commands(commands)
    except Exception as e: logger.warning(f"设置机器人命令失败: {e}")
    
    # 对话处理器
    settings_conv = ConversationHandler(
        entry_points=[CommandHandler("settings", settings_command)],
        states={
            STATE_SETTINGS_MAIN: [CallbackQueryHandler(settings_callback_handler, pattern=r"^settings_")],
            STATE_SETTINGS_ACTION: [CallbackQueryHandler(settings_action_handler, pattern=r"^action_")],
            STATE_GET_KEY: [MessageHandler(Filters.text & ~Filters.command, get_key)],
            STATE_GET_PROXY: [MessageHandler(Filters.text & ~Filters.command, get_proxy)],
            STATE_REMOVE_API: [MessageHandler(Filters.text & ~Filters.command, remove_api)],
        }, fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(settings_command, pattern=r"action_back_main")]
    )
    kkfofa_conv = ConversationHandler(
        entry_points=[CommandHandler("kkfofa", kkfofa_command)],
        states={
            STATE_CACHE_CHOICE: [CallbackQueryHandler(cache_choice_callback, pattern=r"^cache_")],
            STATE_KKFOFA_MODE: [CallbackQueryHandler(query_mode_callback, pattern=r"^mode_")],
        }, fallbacks=[CommandHandler("cancel", cancel)]
    )
    import_conv = ConversationHandler(
        entry_points=[CommandHandler("import", import_command)],
        states={STATE_GET_IMPORT_QUERY: [MessageHandler(Filters.text & ~Filters.command, get_import_query)]},
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    # 添加处理器
    dispatcher.add_handler(CommandHandler("start", start_command))
    dispatcher.add_handler(CommandHandler("help", help_command))
    dispatcher.add_handler(CommandHandler("stop", stop_all_tasks))
    dispatcher.add_handler(CommandHandler("backup", backup_config_command))
    dispatcher.add_handler(CommandHandler("restore", restore_config_command))
    dispatcher.add_handler(CommandHandler("history", history_command))
    dispatcher.add_handler(CommandHandler("getlog", get_log_command))
    dispatcher.add_handler(CommandHandler("shutdown", shutdown_command))
    dispatcher.add_handler(settings_conv)
    dispatcher.add_handler(kkfofa_conv)
    dispatcher.add_handler(import_conv)
    dispatcher.add_handler(MessageHandler(Filters.document.mime_type("application/json"), receive_config_file))
    
    logger.info("🚀 终极版机器人已启动...")
    updater.start_polling()
    updater.idle()
    logger.info("机器人已关闭。")

if __name__ == "__main__":
    main()

