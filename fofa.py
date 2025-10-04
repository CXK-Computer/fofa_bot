import os
import json
import logging
import base64
import time
import re
import asyncio
from datetime import datetime, timezone
from functools import wraps
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    JobQueue,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- 全局变量和常量 ---
CONFIG_FILE = 'config.json'
LOG_FILE = 'fofa_bot.log'
CACHE_FILE = 'fofa_cache.json'
CACHE_EXPIRATION_SECONDS = 24 * 60 * 60  # 24 hours
TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024 # 20 MB

# --- 日志配置 ---
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

# --- 状态定义 ---
(
    STATE_SETTINGS_MAIN, STATE_SETTINGS_ACTION, STATE_GET_KEY, STATE_GET_PROXY,
    STATE_REMOVE_API, STATE_ACCESS_CONTROL, STATE_ADD_ADMIN, STATE_REMOVE_ADMIN,
    STATE_PRESET_SUBMIT_NAME, STATE_PRESET_SUBMIT_QUERY, STATE_PRESET_MANAGE,
    STATE_PRESET_ADD_NAME, STATE_PRESET_ADD_QUERY, STATE_PRESET_REMOVE
) = range(14)


# --- 配置管理 ---
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
    "super_admin": 0, # 请务必在config.json中设置正确的ID
    "admins": [],
    "apis": [],
    "proxy": "",
    "full_mode": False,
    "public_mode": False,
    "presets": [],
    "pending_presets": []
}
CONFIG = load_json_file(CONFIG_FILE, DEFAULT_CONFIG)
# 确保新字段存在
CONFIG.setdefault('presets', [])
CONFIG.setdefault('pending_presets', [])
save_json_file(CONFIG_FILE, CONFIG) # 保存以确保新字段写入

CACHE = load_json_file(CACHE_FILE, {})

def save_config():
    save_json_file(CONFIG_FILE, CONFIG)

def save_cache():
    save_json_file(CACHE_FILE, CACHE)


# --- 辅助函数与装饰器 ---
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


# --- FOFA API 核心逻辑 ---
async def get_available_api(context: ContextTypes.DEFAULT_TYPE):
    # 此处省略了完整的API调用和检查逻辑，假设它能正常工作并返回一个可用的API key
    # In a real scenario, you'd check API limits here.
    if not CONFIG['apis']:
        return None
    # Simple round-robin for demonstration
    if 'api_index' not in context.bot_data:
        context.bot_data['api_index'] = 0
    
    idx = context.bot_data['api_index']
    api_key = CONFIG['apis'][idx]
    context.bot_data['api_index'] = (idx + 1) % len(CONFIG['apis'])
    return api_key

async def call_fofa_api(query: str, api_key: str):
    # This is a mock API call function. Replace with your actual HTTP request logic.
    logger.info(f"模拟调用FOFA API: query='{query}', key='...{api_key[-4:]}'")
    await asyncio.sleep(3) # Simulate network delay
    # Generate some fake data for demonstration
    results = [f"https://example.com/result/{i}" for i in range(1, 151)]
    return {"error": False, "results": results, "size": len(results)}

async def execute_fofa_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query_text: str):
    message = update.effective_message
    status_msg = await message.reply_text(f"🔍 正在查询: `{escape_markdown(query_text)}`", parse_mode=ParseMode.MARKDOWN)

    # 1. 检查缓存
    now = time.time()
    cache_key = base64.b64encode(query_text.encode()).decode()
    if cache_key in CACHE and (now - CACHE[cache_key]['timestamp']) < CACHE_EXPIRATION_SECONDS:
        logger.info(f"命中缓存: {query_text}")
        cached_data = CACHE[cache_key]['data']
        await status_msg.edit_text(f"✅ 查询完成 (来自缓存):\n`{escape_markdown(query_text)}`", parse_mode=ParseMode.MARKDOWN)
        if isinstance(cached_data, str) and cached_data.startswith("FILEID:"):
            await message.reply_document(cached_data.replace("FILEID:", ""), caption=f"缓存结果 for `{query_text}`")
        else:
            await message.reply_text(cached_data)
        return

    # 2. 调用API
    api_key = await get_available_api(context)
    if not api_key:
        await status_msg.edit_text("❌ 查询失败：没有可用的FOFA API密钥。请管理员添加。")
        return

    try:
        data = await call_fofa_api(query_text, api_key)
        if data.get('error'):
            await status_msg.edit_text(f"❌ API错误: {data.get('errmsg', '未知错误')}")
            return

        results = data.get('results', [])
        if not results:
            await status_msg.edit_text(f"✅ 查询完成，但未找到结果。\n`{escape_markdown(query_text)}`", parse_mode=ParseMode.MARKDOWN)
            return

        result_count = len(results)
        await status_msg.edit_text(f"✅ 查询完成，共找到 {result_count} 条结果。\n`{escape_markdown(query_text)}`", parse_mode=ParseMode.MARKDOWN)

        # 3. 格式化并发送结果
        output_text = "\n".join(map(str, results))

        if len(output_text.encode('utf-8')) <= 4000:
             # 直接发送
            await message.reply_text(output_text)
            CACHE[cache_key] = {'timestamp': now, 'data': output_text}
        else:
            # 发送文件
            filename = f"fofa_results_{int(now)}.txt"
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(output_text)
            
            with open(filename, 'rb') as f:
                sent_message = await message.reply_document(f, caption=f"查询结果: `{query_text}`")
                # 缓存文件ID以便复用
                CACHE[cache_key] = {'timestamp': now, 'data': f"FILEID:{sent_message.document.file_id}"}
            os.remove(filename)
        
        save_cache()

    except Exception as e:
        logger.error(f"查询执行失败: {e}")
        await status_msg.edit_text(f"❌ 执行查询时发生内部错误。")


# --- 命令处理 ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "欢迎使用 FOFA 查询机器人！\n\n"
        "➡️ 使用 `/kkfofa` 开始查询。\n"
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
        
        await query.edit_message_text(f"🚀 正在执行预设查询: *{escape_markdown(preset['name'])}*", parse_mode=ParseMode.MARKDOWN)
        await execute_fofa_search(update, context, query_text)
    except (ValueError, IndexError):
        await query.edit_message_text("❌ 预设查询失败，可能该预设已被移除。")
    except Exception as e:
        logger.error(f"执行预设时出错: {e}")
        await query.edit_message_text("❌ 执行预设时发生内部错误。")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_text = '操作已取消。'
    if update.message:
        await update.message.reply_text(message_text)
    elif update.callback_query:
        await update.callback_query.edit_message_text(message_text)
        await update.callback_query.answer()
    context.user_data.clear()
    return ConversationHandler.END


# --- 预设提交与审批 ---
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
                                  parse_mode=ParseMode.MARKDOWN)
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
                reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN
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
        # Re-load config to get the latest state
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
            await query.edit_message_text(f"✅ 您已批准预设 *{escape_markdown(preset_name)}*。", parse_mode=ParseMode.MARKDOWN)
            if proposer_id:
                await context.bot.send_message(chat_id=proposer_id, text=f"🎉 恭喜！您提交的预设查询 “{preset_name}” 已被批准。")
        elif action == 'reject':
            await query.edit_message_text(f"❌ 您已拒绝预设 *{escape_markdown(preset_name)}*。", parse_mode=ParseMode.MARKDOWN)
            if proposer_id:
                await context.bot.send_message(chat_id=proposer_id, text=f"很遗憾，您提交的预设查询 “{preset_name}” 已被拒绝。")
        
        save_config()

    except IndexError:
        await query.edit_message_text("🤔 操作失败，该提交可能已被处理或撤销。")
    except Exception as e:
        logger.error(f"处理预设审批时出错: {e}")
        await query.edit_message_text("❌ 内部错误，操作失败。")
    finally:
        context.bot_data.pop(lock_key, None)

# --- 设置菜单及相关功能 (部分简化，请根据需要填充) ---
@admin_only
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🔑 API 管理", callback_data='settings_api')],
        [InlineKeyboardButton("🌐 代理设置", callback_data='settings_proxy')],
    ]
    if is_super_admin(update.effective_user.id):
        pending_count = len(CONFIG.get('pending_presets', []))
        preset_btn_text = f"✨ 预设管理" + (f" ({pending_count}🔔)" if pending_count > 0 else "")
        keyboard.append([InlineKeyboardButton("👑 访问控制", callback_data='settings_access')])
        keyboard.append([InlineKeyboardButton(preset_btn_text, callback_data='settings_presets')])
    keyboard.append([InlineKeyboardButton("⚙️ 模式切换", callback_data='settings_mode')])
    keyboard.append([InlineKeyboardButton("💾 备份配置", callback_data='settings_backup')])
    
    message_text = "⚙️ *设置菜单*"
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(message_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    return STATE_SETTINGS_MAIN

# --- 预设管理(超级管理员) ---
@super_admin_only
async def show_preset_management_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Re-load config to show latest info
    global CONFIG; CONFIG = load_json_file(CONFIG_FILE, DEFAULT_CONFIG)
    
    presets = CONFIG.get("presets", [])
    text = "✨ *预设管理*\n\n*已批准的预设:*\n"
    text += "\n".join([f"🔹 `{p['name']}`" for p in presets]) if presets else "_无_"
    
    pending = CONFIG.get("pending_presets", [])
    if pending:
        text += "\n\n🔔 *待审批的预设:*\n"
        text += "\n".join([f"🔸 `{p['name']}` (from {p.get('proposer_name', 'N/A')})" for p in pending])

    keyboard = [
        [InlineKeyboardButton("➕ 添加预设", callback_data='preset_add'), InlineKeyboardButton("➖ 删除预设", callback_data='preset_remove')],
        [InlineKeyboardButton("🔙 返回设置", callback_data='preset_back_settings')]
    ]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return STATE_PRESET_MANAGE

# --- 模拟其他设置功能 ---
# 为了保持脚本完整性，这里提供了其他设置菜单项的框架函数。
async def show_api_menu(update, context): await update.callback_query.edit_message_text("API管理功能占位符")
async def show_proxy_menu(update, context): await update.callback_query.edit_message_text("代理设置功能占位符")
async def show_access_control_menu(update, context): await update.callback_query.edit_message_text("访问控制功能占位符")
async def show_mode_menu(update, context): await update.callback_query.edit_message_text("模式切换功能占位符")
async def backup_config(update, context): 
    await update.callback_query。answer("正在发送备份...")
    await update.effective_message。reply_document(open(CONFIG_FILE, 'rb'), caption="这是当前的配置文件备份。")

async def settings_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    menu = query.data.split('_', 1)[1]

    user_id = update.effective_user.id
    if menu in ['access', 'presets'] and not is_super_admin(user_id):
        await query.answer("⛔️ 权限不足", show_alert=True)
        return STATE_SETTINGS_MAIN

    if menu == 'api': await show_api_menu(update, context); return STATE_SETTINGS_ACTION
    elif menu == 'proxy': await show_proxy_menu(update, context); return STATE_SETTINGS_ACTION
    elif menu == 'access': await show_access_control_menu(update, context); return STATE_ACCESS_CONTROL
    elif menu == 'presets': await show_preset_management_menu(update, context); return STATE_PRESET_MANAGE
    elif menu == 'mode': await show_mode_menu(update, context); return STATE_SETTINGS_ACTION
    elif menu == 'backup': await backup_config(update, context); return STATE_SETTINGS_MAIN # Remain in main menu
    else: return STATE_SETTINGS_MAIN

async def preset_management_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data。split('_'， 1)[1]

    if action == 'back_settings':
        await settings_command(update, context)
        return STATE_SETTINGS_MAIN
    # Add logic for preset_add and preset_remove here
    await query.message.reply_text(f"功能 '{action}' 待实现。")
    return STATE_PRESET_MANAGE


# --- 主程序 ---
async def main() -> None:
    # 1. 创建一个 pytz 时区对象
    timezone = pytz.timezone('Asia/Shanghai')
    
    # 2. 创建一个 JobQueue，并为它配置一个带有正确时区的调度器
    job_queue = JobQueue()
    job_queue.scheduler = AsyncIOScheduler(timezone=timezone)

    # 3. 在构建 Application 时，使用我们手动创建的 job_queue
    application = (
        Application.builder()
        .token("YOUR_TELEGRAM_BOT_TOKEN")
        .job_queue(job_queue)
        .build()
    )

    # --- 会话处理器 ---
    submit_preset_conv = ConversationHandler(
        entry_points=[CommandHandler("submit_preset", submit_preset_command)],
        states={
            STATE_PRESET_SUBMIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, preset_submit_get_name)],
            STATE_PRESET_SUBMIT_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, preset_submit_get_query)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    settings_conv = ConversationHandler(
        entry_points=[CommandHandler("settings", settings_command)],
        states={
            STATE_SETTINGS_MAIN: [CallbackQueryHandler(settings_callback_handler, pattern=r"^settings_")],
            STATE_PRESET_MANAGE: [CallbackQueryHandler(preset_management_callback_handler, pattern=r"^preset_")],
            # Add other states like STATE_SETTINGS_ACTION, STATE_ACCESS_CONTROL here
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )
    
    # --- 命令与回调处理器 ---
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("kkfofa", kkfofa_command))
    application.add_handler(submit_preset_conv)
    application.add_handler(settings_conv)

    application.add_handler(CallbackQueryHandler(run_preset_callback, pattern=r"^run_preset_"))
    application.add_handler(CallbackQueryHandler(preset_approval_callback, pattern=r"^preset_(approve|reject)_"))

    commands = [
        BotCommand("start", "开始使用机器人"),
        BotCommand("kkfofa", "执行FOFA查询或查看预设"),
        BotCommand("submit_preset", "（管理员）提交预设查询"),
        BotCommand("settings", "（管理员）打开设置菜单"),
        BotCommand("cancel", "取消当前操作"),
    ]
    await application.bot.set_my_commands(commands)

    logger.info("机器人启动成功...")
    await application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    asyncio.run(main())

