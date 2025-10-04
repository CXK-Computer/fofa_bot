import os
import json
import logging
import base64
import time
import re
import asyncio
import requests
from datetime import datetime, timezone
from functools import wraps
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    JobQueue,
)
import pytz # 保留 pytz 用于创建时区对象

# --- 全局变量和常量 (无变化) ---
CONFIG_FILE = 'config.json'
LOG_FILE = 'fofa_bot.log'
CACHE_FILE = 'fofa_cache.json'
CACHE_EXPIRATION_SECONDS = 24 * 60 * 60  # 24 hours
FOFA_SEARCH_URL = "https://fofa.info/api/v1/search/all"
FOFA_STATS_URL = "https://fofa.info/api/v1/stats/statistical"

# --- 日志配置 (无变化) ---
if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > (5 * 1024 * 1024):  # 5MB
    try:
        os.rename(LOG_FILE, LOG_FILE + '.old')
    except OSError as e:
        print(f"无法轮换日志文件: {e}")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()]
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- 状态定义 (无变化) ---
(
    STATE_SETTINGS_MAIN, STATE_API_MENU, STATE_GET_KEY, STATE_REMOVE_API,
    STATE_ACCESS_CONTROL, STATE_ADD_ADMIN, STATE_REMOVE_ADMIN,
    STATE_PRESET_SUBMIT_NAME, STATE_PRESET_SUBMIT_QUERY, STATE_PRESET_MANAGE,
    STATE_PRESET_REMOVE, STATE_GET_STATS_QUERY, STATE_MONITOR_MENU,
    STATE_SET_MONITOR_URL, STATE_SET_MONITOR_INTERVAL
) = range(15)

# --- 配置管理 (无变化) ---
def load_json_file(filename, default_content):
    if not os.path.exists(filename):
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(default_content, f, indent=4)
        return default_content
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        logger.error(f"{filename} 损坏或为空, 将使用默认内容重建。")
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(default_content, f, indent=4)
        return default_content

def save_json_file(filename, data):
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)

DEFAULT_CONFIG = {
    "super_admin": 0,
    "admins": [],
    "apis": [],
    "proxy": "",
    "full_mode": False,
    "public_mode": False,
    "presets": [],
    "pending_presets": [],
    "monitor_url": "",
    "monitor_interval_seconds": 300
}
CONFIG = load_json_file(CONFIG_FILE, DEFAULT_CONFIG)
CONFIG.setdefault('presets', [])
CONFIG.setdefault('pending_presets', [])
CONFIG.setdefault('monitor_url', "")
CONFIG.setdefault('monitor_interval_seconds', 300)
save_json_file(CONFIG_FILE, CONFIG)

CACHE = load_json_file(CACHE_FILE, {})

def save_config():
    save_json_file(CONFIG_FILE, CONFIG)

def save_cache():
    save_json_file(CACHE_FILE, CACHE)

# --- 辅助函数与装饰器 (无变化) ---
def escape_markdown(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    escape_chars = r'_*`[]()~>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def is_super_admin(user_id: int) -> bool:
    return user_id == CONFIG.get('super_admin')

def is_admin(user_id: int) -> bool:
    return is_super_admin(user_id) or user_id in CONFIG.get('admins', [])

def super_admin_only(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if not is_super_admin(user_id):
            message_text = "⛔️ 抱歉，此操作仅限**超级管理员**执行。"
            if update.callback_query:
                await update.callback_query.answer(message_text.replace('**', ''), show_alert=True)
            elif update.message:
                await update.message.reply_text(message_text, parse_mode=ParseMode.MARKDOWN)
            return None
        return await func(update, context, *args, **kwargs)
    return wrapped

def admin_only(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if not is_admin(user_id):
            message_text = "⛔️ 抱歉，您没有权限执行此管理操作。"
            if update.callback_query:
                await update.callback_query.answer(message_text, show_alert=True)
            elif update.message:
                await update.message.reply_text(message_text)
            return None
        return await func(update, context, *args, **kwargs)
    return wrapped

def user_access_check(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if not CONFIG.get('public_mode', False) and not is_admin(user_id):
            await update.message.reply_text("⛔️ 抱歉，此机器人当前为私有模式，您没有权限进行查询。")
            return None
        return await func(update, context, *args, **kwargs)
    return wrapped


# --- 所有其他函数 (从 FOFA API 核心逻辑 到 设置菜单) 保持完全不变 ---
# (为了简洁，这里省略了 600 多行未修改的代码)
# ...
# 粘贴到这里的所有函数都和上一个版本完全一样
# ...
async def get_available_api_key(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """轮询获取一个可用的API Key"""
    if not CONFIG['apis']:
        return None
    
    # 简单的轮询逻辑
    if 'api_index' not in context.bot_data or context.bot_data['api_index'] >= len(CONFIG['apis']):
        context.bot_data['api_index'] = 0
    
    idx = context.bot_data['api_index']
    api_key = CONFIG['apis'][idx]
    context.bot_data['api_index'] = (idx + 1) % len(CONFIG['apis'])
    return api_key

async def call_fofa_api(query: str, api_key: str) -> dict:
    """真实的FOFA API调用函数，用于 /kkfofa 查询"""
    logger.info(f"正在使用Key '...{api_key[-4:]}' 调用FOFA API查询: {query}")
    try:
        qbase64 = base64.b64encode(query.encode('utf-8')).decode('utf-8')
        params = {
            'key': api_key,
            'qbase64': qbase64,
            'size': 10000,  # 查询最大数量
            'fields': 'host,title,ip,domain,port,protocol,server', # 可自定义字段
            'full': CONFIG.get('full_mode', False)
        }
        response = requests.get(FOFA_SEARCH_URL, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"FOFA API请求失败: {e}")
        return {"error": True, "errmsg": f"网络错误: {e}"}

async def fetch_fofa_stats(query: str, api_key: str) -> dict:
    """真实的FOFA API调用函数，用于 /stats 全球统计"""
    logger.info(f"正在使用Key '...{api_key[-4:]}' 调用FOFA API统计: {query}")
    try:
        qbase64 = base64.b64encode(query.encode('utf-8')).decode('utf-8')
        params = {'key': api_key, 'qbase64': qbase64}
        response = requests.get(FOFA_STATS_URL, params=params, timeout=20)
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            return {"success": False, "message": data.get("errmsg", "未知API错误")}
        return {"success": True, "data": data}
    except requests.exceptions.RequestException as e:
        logger.error(f"FOFA统计API请求失败: {e}")
        return {"success": False, "message": f"网络错误: {e}"}

# (函数 execute_fofa_search, start_command, kkfofa_command, run_preset_callback, cancel 保持不变)
async def execute_fofa_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query_text: str):
    message = update.effective_message
    status_msg = await message.reply_text(f"🔍 正在查询: `{escape_markdown(query_text)}`", parse_mode=ParseMode.MARKDOWN_V2)

    now = time.time()
    cache_key = base64.b64encode(query_text.encode()).decode()
    if cache_key in CACHE and (now - CACHE[cache_key]['timestamp']) < CACHE_EXPIRATION_SECONDS:
        logger.info(f"命中缓存: {query_text}")
        cached_data = CACHE[cache_key]['data']
        await status_msg.edit_text(f"✅ 查询完成 (来自缓存):\n`{escape_markdown(query_text)}`", parse_mode=ParseMode.MARKDOWN_V2)
        if isinstance(cached_data, str) and cached_data.startswith("FILEID:"):
            await message.reply_document(cached_data.replace("FILEID:", ""), caption=f"缓存结果 for `{query_text}`")
        else:
            await message.reply_text(cached_data)
        return

    api_key = await get_available_api_key(context)
    if not api_key:
        await status_msg.edit_text("❌ 查询失败：没有可用的FOFA API密钥。请管理员添加。")
        return

    try:
        data = await call_fofa_api(query_text, api_key)
        if data.get('error'):
            await status_msg.edit_text(f"❌ API错误: {escape_markdown(data.get('errmsg', '未知错误'))}", parse_mode=ParseMode.MARKDOWN_V2)
            return

        results = data.get('results', [])
        if not results:
            await status_msg.edit_text(f"✅ 查询完成，但未找到结果。\n`{escape_markdown(query_text)}`", parse_mode=ParseMode.MARKDOWN_V2)
            return

        result_count = len(results)
        await status_msg.edit_text(f"✅ 查询完成，共找到 {result_count} 条结果。\n`{escape_markdown(query_text)}`", parse_mode=ParseMode.MARKDOWN_V2)
        
        # 将列表转换为字符串
        output_text = "\n".join(map(str, results))

        if len(output_text.encode('utf-8')) <= 4000:
            await message.reply_text(output_text)
            CACHE[cache_key] = {'timestamp': now, 'data': output_text}
        else:
            filename = f"fofa_results_{int(now)}.txt"
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(output_text)
            
            with open(filename, 'rb') as f:
                sent_message = await message.reply_document(f, caption=f"查询结果: `{query_text}`")
                CACHE[cache_key] = {'timestamp': now, 'data': f"FILEID:{sent_message.document.file_id}"}
            os.remove(filename)
        
        save_cache()

    except Exception as e:
        logger.error(f"查询执行失败: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ 执行查询时发生内部错误。")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "欢迎使用 FOFA 查询机器人！\n\n"
        "➡️ 使用 `/kkfofa` 开始查询。\n"
        "📊 使用 `/stats` 获取全球统计。\n"
        "🚀 使用 `/run` 启动后台任务。\n"
        "➡️ 管理员可使用 `/settings` 进行配置。\n"
        "➡️ 管理员可使用 `/submit_preset` 提交常用查询。"
    )

@user_access_check
async def kkfofa_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        presets = CONFIG.get("presets", [])
        if not presets:
            await update.message.reply_text("欢迎使用FOFA查询机器人。\n\n"
                                          "➡️ 直接输入查询语法: `/kkfofa domain=\"example.com\"`\n"
                                          "ℹ️ 当前没有可用的预设查询。管理员可通过 `/submit_preset` 提交。")
            return
        
        keyboard = [[InlineKeyboardButton(p['name'], callback_data=f"run_preset_{i}")] for i, p in enumerate(presets)]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("👇 请选择一个预设查询，或直接输入查询语法:", reply_markup=reply_markup)
        return

    query_text = " ".join(args)
    await execute_fofa_search(update, context, query_text)

async def run_preset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    try:
        preset_index = int(query.data.replace("run_preset_", ""))
        preset = CONFIG["presets"][preset_index]
        query_text = preset['query']
        
        await query.edit_message_text(f"🚀 正在执行预设查询: *{escape_markdown(preset['name'])}*", parse_mode=ParseMode.MARKDOWN_V2)
        await execute_fofa_search(update, context, query_text)
    except (ValueError, IndexError):
        await query.edit_message_text("❌ 预设查询失败，可能该预设已被移除。")
    except Exception as e:
        logger.error(f"执行预设时出错: {e}")
        await query.edit_message_text("❌ 执行预设时发生内部错误。")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_text = '操作已取消。'
    if update.message:
        await update.message.reply_text(message_text, reply_markup=ReplyKeyboardRemove())
    elif update.callback_query:
        await update.callback_query.edit_message_text(message_text)
        await update.callback_query.answer()
    context.user_data.clear()
    return ConversationHandler.END


# --- 新增: FOFA 全球统计 /stats ---
@user_access_check
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/stats 命令的入口, 启动对话"""
    if not CONFIG['apis']:
        await update.message.reply_text("错误：FOFA API Key 未设置！\n请管理员使用 /settings -> API管理 进行配置。")
        return ConversationHandler.END

    await update.message.reply_text(
        "请输入你想要统计的 FOFA 语法。\n例如: `app=\"nginx\"`\n\n随时可以发送 /cancel 来取消。",
        parse_mode=ParseMode.MARKDOWN_V2
    )
    return STATE_GET_STATS_QUERY

async def get_fofa_stats_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """接收用户发送的 FOFA 统计语法并处理"""
    query_text = update.message.text
    api_key = await get_available_api_key(context) # 复用轮询逻辑
    
    processing_message = await update.message.reply_text("正在查询 FOFA, 请稍候...")

    result = await fetch_fofa_stats(query_text, api_key)
    
    if not result["success"]:
        error_message = escape_markdown(result["message"])
        await processing_message.edit_text(f"查询失败 😞\n*原因:* `{error_message}`", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    stats_data = result["data"]
    safe_query = escape_markdown(query_text)
    message_lines = [
        f"*📊 FOFA 全球统计信息*",
        f"*查询:* `{safe_query}`",
        f"*最后更新:* `{escape_markdown(stats_data.get('last_update_time', 'N/A'))}`", "",
        "*🌍 Top 5 国家/地区:*",
    ]
    for item in stats_data.get("countries", [])[:5]: message_lines.append(f"  \\- `{escape_markdown(item['name'])}`: *{item['count']}*")
    message_lines.append("\n*💻 Top 5 服务/组件:*")
    for item in stats_data.get("as_servers", [])[:5]: message_lines.append(f"  \\- `{escape_markdown(item['name'])}`: *{item['count']}*")
    message_lines.append("\n*🔌 Top 5 协议:*")
    for item in stats_data.get("protocols", [])[:5]: message_lines.append(f"  \\- `{escape_markdown(item['name'])}`: *{item['count']}*")

    await processing_message.edit_text("\n".join(message_lines), parse_mode=ParseMode.MARKDOWN_V2)
    return ConversationHandler.END


# --- 新增: 后台监控任务与命令 ---
def get_job_name(chat_id: int) -> str:
    """为后台任务生成唯一的名称"""
    return f"monitor_task_{chat_id}"

async def monitor_task(context: ContextTypes.DEFAULT_TYPE) -> None:
    """后台循环监控任务"""
    job = context.job
    # 重新加载配置以获取最新URL和管理员ID
    global CONFIG; CONFIG = load_json_file(CONFIG_FILE, DEFAULT_CONFIG)
    
    monitor_url = CONFIG.get('monitor_url')
    super_admin_id = CONFIG.get('super_admin')

    if not monitor_url:
        logger.warning("[监控任务] URL未设置，任务跳过。")
        # 仅在第一次或配置被清除时通知
        if not context.bot_data.get('monitor_url_warning_sent', False):
             if super_admin_id: await context.bot.send_message(chat_id=super_admin_id, text="[后台任务] 警告：监控 URL 未设置，任务无法执行。请使用 /settings 进行设置。")
             context.bot_data['monitor_url_warning_sent'] = True
        return
    context.bot_data['monitor_url_warning_sent'] = False # Reset warning flag

    logger.info(f"[监控任务] 正在执行... 目标URL: {monitor_url}")
    # --- 在下方添加你的核心监控逻辑 ---
    # 示例:
    try:
        # response = requests.get(monitor_url, timeout=10)
        # response.raise_for_status()
        # if "error" in response.text:
        #     await context.bot.send_message(chat_id=super_admin_id, text=f"🚨 监控警报！URL {monitor_url} 返回内容异常！")
        pass # Placeholder for your logic
    except Exception as e:
        logger.error(f"[监控任务] 访问URL时出错: {e}")
        if super_admin_id: await context.bot.send_message(chat_id=super_admin_id, text=f"🚨 监控警报！访问 {monitor_url} 失败: {e}")
    # --- 监控逻辑结束 ---

@admin_only
async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    job_name = get_job_name(chat_id)
    
    if not CONFIG.get('monitor_url'):
        await update.message.reply_text("配置不完整！请先使用 /settings -> 监控设置 来设置监控URL。")
        return

    if context.job_queue.get_jobs_by_name(job_name):
        await update.message.reply_text("后台任务已经在运行中。")
        return

    interval = CONFIG.get('monitor_interval_seconds', 300)
    context.job_queue.run_repeating(monitor_task, interval=interval, chat_id=chat_id, name=job_name)
    await update.message.reply_text(f"✅ 后台监控任务已启动！将每 {interval} 秒执行一次。")

@admin_only
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    job_name = get_job_name(chat_id)
    
    current_jobs = context.job_queue.get_jobs_by_name(job_name)
    if not current_jobs:
        await update.message.reply_text("当前没有正在运行的后台任务。")
        return
    for job in current_jobs: job.schedule_removal()
    await update.message.reply_text("⏹️ 后台监控任务已停止。")

@admin_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    job_name = get_job_name(chat_id)
    
    if context.job_queue.get_jobs_by_name(job_name):
        interval = CONFIG.get('monitor_interval_seconds', 300)
        url = CONFIG.get('monitor_url')
        await update.message.reply_text(f"🟢 后台任务正在运行中。\nURL: `{escape_markdown(url)}`\n间隔: *{interval}* 秒", parse_mode=ParseMode.MARKDOWN_V2)
    else:
        await update.message.reply_text("🔴 后台任务已停止。")


# --- 预设提交与审批 (保持不变) ---
# (函数 submit_preset_command, preset_submit_get_name, preset_submit_get_query, preset_approval_callback 保持不变)
@admin_only
async def submit_preset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("好的，我们来提交一个新的预设查询。\n\n"
                                  "📝 **第一步**：请输入这个预设的名称（例如：海康威视摄像头）。\n\n"
                                  "随时可以输入 /cancel 来取消操作。", parse_mode=ParseMode.MARKDOWN)
    return STATE_PRESET_SUBMIT_NAME

async def preset_submit_get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['preset_name'] = update.message.text.strip()
    await update.message.reply_text(f"名称设为: *{escape_markdown(context.user_data['preset_name'])}*\n\n"
                                  "📝 **第二步**：现在请输入完整的FOFA查询语法（例如：`app=\"HIKVISION-NVR\"`）。",
                                  parse_mode=ParseMode.MARKDOWN_V2)
    return STATE_PRESET_SUBMIT_QUERY

async def preset_submit_get_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['preset_query'] = update.message.text.strip()
    name = context.user_data['preset_name']
    query_text = context.user_data['preset_query']
    user = update.effective_user

    pending_preset = {
        "name": name, "query": query_text, "proposer_id": user.id,
        "proposer_name": user.full_name, "timestamp": datetime.now(timezone.utc).isoformat()
    }
    CONFIG['pending_presets'].append(pending_preset)
    save_config()
    
    await update.message.reply_text("✅ 您的预设提交成功！已发送给超级管理员进行审批。")
    
    super_admin_id = CONFIG.get('super_admin')
    if super_admin_id and super_admin_id != user.id:
        pending_index = len(CONFIG['pending_presets']) - 1
        keyboard = [[
            InlineKeyboardButton("✅ 同意", callback_data=f"preset_approve_{pending_index}"),
            InlineKeyboardButton("❌ 拒绝", callback_data=f"preset_reject_{pending_index}")
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        notification_text = (
            f"🔔 *新的预设提交请求*\n\n"
            f"**提交人**: {escape_markdown(user.full_name)} (`{user.id}`)\n"
            f"**预设名称**: {escape_markdown(name)}\n"
            f"**查询语法**: `{escape_markdown(query_text)}`"
        )
        try:
            await context.bot.send_message(
                chat_id=super_admin_id, text=notification_text,
                reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception as e:
            logger.error(f"无法向超级管理员 {super_admin_id} 发送通知: {e}")

    context.user_data.clear()
    return ConversationHandler.END

@super_admin_only
async def preset_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    action, index_str = query.data.split('_')[1:]
    index = int(index_str)

    lock_key = f"lock_preset_{index}"
    if context.bot_data.get(lock_key):
        await query.answer("正在处理中，请勿重复点击。", show_alert=True)
        return
    context.bot_data[lock_key] = True

    try:
        global CONFIG
        CONFIG = load_json_file(CONFIG_FILE, DEFAULT_CONFIG)
        pending_presets = CONFIG.get('pending_presets', [])
        
        if index >= len(pending_presets):
            raise IndexError("Preset not found, it may have been processed.")

        pending_preset = pending_presets.pop(index)
        proposer_id = pending_preset['proposer_id']
        preset_name = pending_preset['name']

        if action == 'approve':
            CONFIG['presets'].append({"name": preset_name, "query": pending_preset['query']})
            await query.edit_message_text(f"✅ 您已批准预设 *{escape_markdown(preset_name)}*。", parse_mode=ParseMode.MARKDOWN_V2)
            if proposer_id: await context.bot.send_message(chat_id=proposer_id, text=f"🎉 恭喜！您提交的预设查询 “{preset_name}” 已被批准。")
        elif action == 'reject':
            await query.edit_message_text(f"❌ 您已拒绝预设 *{escape_markdown(preset_name)}*。", parse_mode=ParseMode.MARKDOWN_V2)
            if proposer_id: await context.bot.send_message(chat_id=proposer_id, text=f"很遗憾，您提交的预设查询 “{preset_name}” 已被拒绝。")
        
        save_config()

    except IndexError:
        await query.edit_message_text("🤔 操作失败，该提交可能已被处理或撤销。")
    except Exception as e:
        logger.error(f"处理预设审批时出错: {e}")
        await query.edit_message_text("❌ 内部错误，操作失败。")
    finally:
        context.bot_data.pop(lock_key, None)


# --- 设置菜单 (已扩展) ---
@admin_only
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🔑 API 管理", callback_data='settings_api')],
        [InlineKeyboardButton("🚀 监控设置", callback_data='settings_monitor')],
    ]
    if is_super_admin(update.effective_user.id):
        pending_count = len(CONFIG.get('pending_presets', []))
        preset_btn_text = f"✨ 预设管理" + (f" ({pending_count}🔔)" if pending_count > 0 else "")
        keyboard.extend([
            [InlineKeyboardButton("👑 访问控制", callback_data='settings_access')],
            [InlineKeyboardButton(preset_btn_text, callback_data='settings_presets')]
        ])
    keyboard.extend([
        [InlineKeyboardButton("⚙️ 模式切换", callback_data='settings_mode')],
        [InlineKeyboardButton("💾 备份配置", callback_data='settings_backup')]
    ])
    
    message_text = "⚙️ *设置菜单*"
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    else:
        await update.message.reply_text(message_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    return STATE_SETTINGS_MAIN

# --- 新增: API 管理菜单 ---
@super_admin_only
async def show_api_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_keys = CONFIG.get("apis", [])
    text = "🔑 *API 密钥管理*\n\n当前已配置的密钥:\n"
    if not api_keys:
        text += "_无_"
    else:
        text += "\n".join([f"`{i+1}`: `...{key[-4:]}`" for i, key in enumerate(api_keys)])

    keyboard = [
        [InlineKeyboardButton("➕ 添加密钥", callback_data='api_add'), InlineKeyboardButton("➖ 移除密钥", callback_data='api_remove')],
        [InlineKeyboardButton("🔙 返回设置", callback_data='api_back_settings')]
    ]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
    return STATE_API_MENU

async def get_api_key_to_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_key = update.message.text.strip()
    if api_key in CONFIG['apis']:
        await update.message.reply_text("该密钥已存在。")
    else:
        CONFIG['apis'].append(api_key)
        save_config()
        await update.message.reply_text("✅ 密钥添加成功！")
    
    await update.message.reply_text("请选择下一步操作...", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回API管理", callback_data='api_back_menu')]]))
    return STATE_API_MENU

async def get_api_index_to_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        index = int(update.message.text.strip()) - 1
        if 0 <= index < len(CONFIG['apis']):
            removed_key = CONFIG['apis'].pop(index)
            save_config()
            await update.message.reply_text(f"✅ 成功移除密钥 `...{removed_key[-4:]}`。")
        else:
            await update.message.reply_text("❌ 无效的序号。")
    except ValueError:
        await update.message.reply_text("❌ 请输入数字序号。")
    
    await update.message.reply_text("请选择下一步操作...", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回API管理", callback_data='api_back_menu')]]))
    return STATE_API_MENU

# --- 新增: 监控设置菜单 ---
@admin_only
async def show_monitor_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = CONFIG.get('monitor_url', '_未设置_')
    interval = CONFIG.get('monitor_interval_seconds', 300)
    text = (
        f"🚀 *后台监控设置*\n\n"
        f"当前URL: `{escape_markdown(url)}`\n"
        f"当前间隔: *{interval}* 秒\n\n"
        "你可以设置一个URL让我在后台定时访问，用于服务心跳或简单监控。"
    )
    keyboard = [
        [InlineKeyboardButton("✏️ 设置URL", callback_data='monitor_set_url'), InlineKeyboardButton("⏰ 设置间隔", callback_data='monitor_set_interval')],
        [InlineKeyboardButton("🔙 返回设置", callback_data='monitor_back_settings')]
    ]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
    return STATE_MONITOR_MENU
    
async def set_monitor_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    CONFIG['monitor_url'] = url
    save_config()
    await update.message.reply_text(f"✅ 监控URL已更新为: `{escape_markdown(url)}`", parse_mode=ParseMode.MARKDOWN_V2,
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回监控设置", callback_data='monitor_back_menu')]]))
    return STATE_MONITOR_MENU

async def set_monitor_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        interval = int(update.message.text.strip())
        if interval < 60:
            await update.message.reply_text("❌ 间隔时间不能少于60秒。")
        else:
            CONFIG['monitor_interval_seconds'] = interval
            save_config()
            await update.message.reply_text(f"✅ 监控间隔已更新为 *{interval}* 秒。", parse_mode=ParseMode.MARKDOWN_V2)
    except ValueError:
        await update.message.reply_text("❌ 请输入一个纯数字。")

    await update.message.reply_text("请选择下一步操作...", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回监控设置", callback_data='monitor_back_menu')]]))
    return STATE_MONITOR_MENU

# (预设、访问控制等菜单函数保持不变或作为占位符)
@super_admin_only
async def show_preset_management_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CONFIG; CONFIG = load_json_file(CONFIG_FILE, DEFAULT_CONFIG)
    presets, pending = CONFIG.get("presets", []), CONFIG.get("pending_presets", [])
    text = "✨ *预设管理*\n\n*已批准:*\n" + ("\n".join([f"🔹 `{p['name']}`" for p in presets]) if presets else "_无_")
    if pending: text += "\n\n🔔 *待审批:*\n" + "\n".join([f"🔸 `{p['name']}` (from {p.get('proposer_name', 'N/A')})" for p in pending])
    keyboard = [[InlineKeyboardButton("🔙 返回设置", callback_data='preset_back_settings')]]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
    return STATE_PRESET_MANAGE

async def show_access_control_menu(update, context): await update.callback_query.edit_message_text("访问控制功能占位符\n\n🔙 返回设置", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="access_back_settings")]]))
async def show_mode_menu(update, context): await update.callback_query.edit_message_text("模式切换功能占位符\n\n🔙 返回设置", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回", callback_data="mode_back_settings")]]))

async def backup_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("正在发送备份...")
    try:
        with open(CONFIG_FILE, 'rb') as f:
            await update.effective_message.reply_document(f, caption="这是当前的配置文件备份。")
    except Exception as e:
        logger.error(f"发送备份文件失败: {e}")
        await update.callback_query.message.reply_text(f"发送备份失败: {e}")

# --- 主设置回调分发器 ---
async def settings_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    menu = query.data.split('_', 1)[1]

    user_id = update.effective_user.id
    if menu in ['access', 'presets', 'api'] and not is_super_admin(user_id):
        await query.answer("⛔️ 权限不足", show_alert=True)
        return STATE_SETTINGS_MAIN

    if menu == 'api': return await show_api_menu(update, context)
    elif menu == 'monitor': return await show_monitor_menu(update, context)
    elif menu == 'access': return await show_access_control_menu(update, context)
    elif menu == 'presets': return await show_preset_management_menu(update, context)
    elif menu == 'mode': return await show_mode_menu(update, context)
    elif menu == 'backup': await backup_config(update, context); return STATE_SETTINGS_MAIN

# --- 其他菜单回调 ---
async def api_menu_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split('_', 1)[1]

    if action == 'back_settings': return await settings_command(update, context)
    if action == 'back_menu': return await show_api_menu(update, context)
    if action == 'add':
        await query.message.reply_text("请输入要添加的 FOFA API Key:")
        return STATE_GET_KEY
    if action == 'remove':
        if not CONFIG['apis']:
            await query.answer("当前没有可移除的密钥。", show_alert=True)
            return STATE_API_MENU
        await query.message.reply_text("请输入要移除的密钥的序号:")
        return STATE_REMOVE_API

async def monitor_menu_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split('_', 1)[1]

    if action == 'back_settings': return await settings_command(update, context)
    if action == 'back_menu': return await show_monitor_menu(update, context)
    if action == 'set_url':
        await query.message.reply_text("请输入新的监控 URL:")
        return STATE_SET_MONITOR_URL
    if action == 'set_interval':
        await query.message.reply_text("请输入新的监控间隔（秒，建议不低于60）:")
        return STATE_SET_MONITOR_INTERVAL
    
async def placeholder_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # For access_control, mode, preset menus to return to main settings
    await update.callback_query.answer()
    return await settings_command(update, context)

# --- 主程序 (关键修正部分) ---
# --- 主程序 (这是正确的版本) ---
async def main() -> None:
    if not CONFIG.get('super_admin'):
        logger.critical("严重错误：config.json 中的 'super_admin' 未设置！机器人无法确定权限，即将退出。")
        return

    # 1. 首先，只构建 Application 对象。它会自己创建一个默认的 JobQueue。
    application = (
        Application.builder()
        .token("8325002891:AAHzYRlWn2Tq_lMyzbfBbkhPC-vX8LqS6kw") # 请替换为你的Token
        .build()
    )

    # 2. 然后，在 application 已经创建好的 job_queue 的 scheduler 上设置时区。
    #    这是唯一正确且不会引起冲突的方式。
    if application.job_queue:
        application.job_queue.scheduler.timezone = pytz.timezone('Asia/Shanghai')
    # --- 会话处理器 (无变化) ---
    # PTBUserWarning 说明:
    # per_message=False 是正确的选择。这个警告只是提醒你，如果设置为False，
    # 整个对话（比如进入设置菜单）不会因为用户发了另一条无关消息而中断。
    # 这对于多级菜单是必要的行为，所以可以忽略这个警告。
    settings_conv = ConversationHandler(
        entry_points=[CommandHandler("settings", settings_command)],
        states={
            STATE_SETTINGS_MAIN: [CallbackQueryHandler(settings_callback_handler, pattern=r"^settings_")],
            STATE_API_MENU: [CallbackQueryHandler(api_menu_callback_handler, pattern=r"^api_")],
            STATE_GET_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_api_key_to_add)],
            STATE_REMOVE_API: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_api_index_to_remove)],
            STATE_MONITOR_MENU: [CallbackQueryHandler(monitor_menu_callback_handler, pattern=r"^monitor_")],
            STATE_SET_MONITOR_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_monitor_url)],
            STATE_SET_MONITOR_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_monitor_interval)],
            STATE_PRESET_MANAGE: [CallbackQueryHandler(placeholder_menu_callback, pattern=r"^preset_")],
            STATE_ACCESS_CONTROL: [CallbackQueryHandler(placeholder_menu_callback, pattern=r"^access_")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False # 这个设置是正确的，因此警告可以忽略
    )

    submit_preset_conv = ConversationHandler(
        entry_points=[CommandHandler("submit_preset", submit_preset_command)],
        states={
            STATE_PRESET_SUBMIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, preset_submit_get_name)],
            STATE_PRESET_SUBMIT_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, preset_submit_get_query)],
        }, fallbacks=[CommandHandler("cancel", cancel)],
    )

    stats_conv = ConversationHandler(
        entry_points=[CommandHandler("stats", stats_command)],
        states={
            STATE_GET_STATS_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_fofa_stats_query)],
        }, fallbacks=[CommandHandler("cancel", cancel)],
    )
    
    # --- 命令与回调处理器 (无变化) ---
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("kkfofa", kkfofa_command))
    application.add_handler(CommandHandler("run", run_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(submit_preset_conv)
    application.add_handler(stats_conv)
    application.add_handler(settings_conv)

    application.add_handler(CallbackQueryHandler(run_preset_callback, pattern=r"^run_preset_"))
    application.add_handler(CallbackQueryHandler(preset_approval_callback, pattern=r"^preset_(approve|reject)_"))

    commands = [
        BotCommand("start", "欢迎与帮助"),
        BotCommand("kkfofa", "执行FOFA查询或查看预设"),
        BotCommand("stats", "获取FOFA全球资产统计"),
        BotCommand("run", "(管理员) 启动后台监控"),
        BotCommand("stop", "(管理员) 停止后台监控"),
        BotCommand("status", "(管理员) 查看监控状态"),
        BotCommand("submit_preset", "(管理员) 提交预设查询"),
        BotCommand("settings", "(管理员) 打开设置菜单"),
        BotCommand("cancel", "取消当前操作"),
    ]
    await application.bot.set_my_commands(commands)

    logger.info("机器人启动成功...")
    await application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    asyncio.run(main())
