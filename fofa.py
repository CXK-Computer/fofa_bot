#
# fofa.py (最终融合版 for python-telegram-bot v13.x)
# 新增功能: /stats 全球统计, /kkfofa 预设菜单, /settings 预设管理
#
import os
import json
import logging
import base64
import time
import re
import requests # <-- 新增依赖

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

# --- 全局变量和常量 ---
CONFIG_FILE = 'config.json'
LOG_FILE = 'fofa_bot.log'
FOFA_SEARCH_URL = "https://fofa.info/api/v1/search/all"
FOFA_STATS_URL = "https://fofa.info/api/v1/stats/statistical"

# --- 日志配置 ---
if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > (5 * 1024 * 1024): # 5MB
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

# --- 会话状态定义 (已扩展) ---
(
    STATE_SETTINGS_MAIN,
    STATE_API_MENU, STATE_GET_KEY, STATE_REMOVE_API,
    STATE_ACCESS_CONTROL, STATE_ADD_ADMIN, STATE_REMOVE_ADMIN,
    STATE_PRESET_MENU, STATE_GET_PRESET_NAME, STATE_GET_PRESET_QUERY, STATE_REMOVE_PRESET,
    STATE_GET_STATS_QUERY
) = range(12)

# --- 配置管理 (已增强) ---
def load_config():
    default_config = {
        "bot_token": "YOUR_BOT_TOKEN_HERE",
        "apis": [],
        "admins": [],
        "proxy": "",
        "full_mode": False,
        "public_mode": False,
        "presets": [] # 新增: 预设列表
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(default_config, f, indent=4)
        logger.info(f"配置文件 {CONFIG_FILE} 不存在，已创建默认配置。")
        return default_config
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
            # 确保所有默认键都存在
            for key, value in default_config.items():
                config.setdefault(key, value)
            return config
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"读取 {CONFIG_FILE} 失败: {e}。将使用默认配置。")
        return default_config

def save_config():
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(CONFIG, f, indent=4, ensure_ascii=False)
    except IOError as e:
        logger.error(f"保存配置文件失败: {e}")

CONFIG = load_config()

# --- 辅助函数与装饰器 ---
def get_proxies():
    if CONFIG.get("proxy"):
        return {"http": CONFIG["proxy"], "https": CONFIG["proxy"]}
    return None

def is_admin(user_id: int) -> bool:
    return user_id in CONFIG.get('admins', [])

def admin_only(func):
    @wraps(func)
    def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
        user_id = update.effective_user.id
        if not is_admin(user_id):
            message_text = "⛔️ 抱歉，您没有权限执行此管理操作。"
            if update.callback_query:
                update.callback_query.answer(message_text, show_alert=True)
            elif update.message:
                update.message.reply_text(message_text)
            return None
        return func(update, context, *args, **kwargs)
    return wrapped

def user_access_check(func):
    @wraps(func)
    def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
        user_id = update.effective_user.id
        if not CONFIG.get('public_mode', False) and not is_admin(user_id):
            update.message.reply_text("⛔️ 抱歉，此机器人当前为私有模式，您没有权限进行查询。")
            return None
        return func(update, context, *args, **kwargs)
    return wrapped

# --- FOFA API 核心逻辑 (使用 requests) ---
def get_available_api_key(context: CallbackContext) -> str:
    """轮询获取一个可用的API Key"""
    if not CONFIG['apis']: return None
    api_index = context.bot_data.get('api_index', 0)
    key = CONFIG['apis'][api_index]
    context.bot_data['api_index'] = (api_index + 1) % len(CONFIG['apis'])
    return key

def call_fofa_api(query: str, api_key: str) -> dict:
    """用于 /kkfofa 的核心查询函数"""
    logger.info(f"正在使用Key '...{api_key[-4:]}' 查询: {query}")
    try:
        qbase64 = base64.b64encode(query.encode('utf-8')).decode('utf-8')
        params = {
            'key': api_key, 'qbase64': qbase64, 'size': 10000,
            'fields': 'host,title,ip,domain,port,protocol,server',
            'full': CONFIG.get('full_mode', False)
        }
        response = requests.get(FOFA_SEARCH_URL, params=params, timeout=60, proxies=get_proxies(), verify=False)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"FOFA API请求失败: {e}")
        return {"error": True, "errmsg": f"网络错误: {e}"}

def fetch_fofa_stats(query: str, api_key: str) -> dict:
    """用于 /stats 的全球统计函数"""
    logger.info(f"正在使用Key '...{api_key[-4:]}' 进行统计: {query}")
    try:
        qbase64 = base64.b64encode(query.encode('utf-8')).decode('utf-8')
        params = {'key': api_key, 'qbase64': qbase64}
        response = requests.get(FOFA_STATS_URL, params=params, timeout=30, proxies=get_proxies(), verify=False)
        response.raise_for_status()
        data = response.json()
        return {"success": not data.get("error"), "data": data}
    except requests.exceptions.RequestException as e:
        logger.error(f"FOFA统计API请求失败: {e}")
        return {"success": False, "data": {"errmsg": f"网络错误: {e}"}}

# --- 主要命令处理 ---
def start_command(update: Update, context: CallbackContext) -> None:
    update.message.reply_text(
        "欢迎使用 FOFA 查询机器人！\n\n"
        "➡️ 使用 /kkfofa 执行查询或查看预设\n"
        "📊 使用 /stats 获取全球统计\n"
        "⚙️ 管理员可使用 /settings 进行配置"
    )

def execute_fofa_search(update: Update, context: CallbackContext, query_text: str):
    """执行FOFA查询并发送结果的中心函数"""
    message = update.effective_message
    status_msg = message.reply_text(f"🔍 正在查询: `{query_text}`", parse_mode=ParseMode.MARKDOWN)
    
    api_key = get_available_api_key(context)
    if not api_key:
        status_msg.edit_text("❌ 查询失败：没有可用的FOFA API密钥。请管理员添加。")
        return

    data = call_fofa_api(query_text, api_key)
    if data.get('error'):
        status_msg.edit_text(f"❌ API错误: {data.get('errmsg', '未知错误')}")
        return

    results = data.get('results', [])
    if not results:
        status_msg.edit_text(f"✅ 查询完成，但未找到结果。\n`{query_text}`", parse_mode=ParseMode.MARKDOWN)
        return

    result_count = len(results)
    caption = f"✅ 查询完成!\n语法: `{query_text}`\n共找到 *{result_count}* 条结果。"
    filename = f"fofa_results_{int(time.time())}.txt"
    with open(filename, 'w', encoding='utf-8') as f:
        for item in results:
            f.write(str(item) + "\n")
    
    status_msg.delete()
    with open(filename, 'rb') as f:
        message.reply_document(f, caption=caption, parse_mode=ParseMode.MARKDOWN)
    os.remove(filename)

@user_access_check
def kkfofa_command(update: Update, context: CallbackContext):
    if not context.args:
        presets = CONFIG.get("presets", [])
        if not presets:
            update.message.reply_text(
                "欢迎使用FOFA查询机器人。\n\n"
                "➡️ 直接输入查询语法: `/kkfofa domain=\"example.com\"`\n"
                "ℹ️ 当前没有可用的预设查询。管理员可通过 /settings 添加。"
            )
            return
        
        keyboard = [[InlineKeyboardButton(p['name'], callback_data=f"run_preset_{i}")] for i, p in enumerate(presets)]
        update.message.reply_text("👇 请选择一个预设查询，或直接输入查询语法:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    query_text = " ".join(context.args)
    execute_fofa_search(update, context, query_text)

def run_preset_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    try:
        preset_index = int(query.data.replace("run_preset_", ""))
        preset = CONFIG["presets"][preset_index]
        query_text = preset['query']
        query.edit_message_text(f"🚀 正在执行预设查询: *{preset['name']}*", parse_mode=ParseMode.MARKDOWN)
        execute_fofa_search(update, context, query_text)
    except (ValueError, IndexError):
        query.edit_message_text("❌ 预设查询失败，可能该预设已被移除。")

# --- 新增: FOFA 全球统计 /stats ---
@user_access_check
def stats_command(update: Update, context: CallbackContext) -> int:
    if not CONFIG['apis']:
        update.message.reply_text("错误：FOFA API Key 未设置！")
        return ConversationHandler.END
    update.message.reply_text("请输入你想要统计的 FOFA 语法。\n例如: `app=\"nginx\"`\n\n随时可以发送 /cancel 来取消。", parse_mode=ParseMode.MARKDOWN)
    return STATE_GET_STATS_QUERY

def get_fofa_stats_query(update: Update, context: CallbackContext) -> int:
    query_text = update.message.text
    api_key = get_available_api_key(context)
    
    processing_message = update.message.reply_text("正在查询 FOFA, 请稍候...")
    result = fetch_fofa_stats(query_text, api_key)
    
    if not result["success"]:
        error_message = result["data"].get("errmsg", "未知错误")
        processing_message.edit_text(f"查询失败 😞\n*原因:* `{error_message}`", parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    stats_data = result["data"]
    message_lines = [
        f"*📊 FOFA 全球统计信息*",
        f"*查询:* `{query_text}`",
        f"*最后更新:* `{stats_data.get('last_update_time', 'N/A')}`", "",
        "*🌍 Top 5 国家/地区:*",
    ]
    for item in stats_data.get("countries", [])[:5]: message_lines.append(f"  - `{item['name']}`: *{item['count']}*")
    message_lines.append("\n*💻 Top 5 服务/组件:*")
    for item in stats_data.get("as_servers", [])[:5]: message_lines.append(f"  - `{item['name']}`: *{item['count']}*")
    message_lines.append("\n*🔌 Top 5 协议:*")
    for item in stats_data.get("protocols", [])[:5]: message_lines.append(f"  - `{item['name']}`: *{item['count']}*")

    processing_message.edit_text("\n".join(message_lines), parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

# --- 设置菜单 (已扩展) ---
@admin_only
def settings_command(update: Update, context: CallbackContext):
    keyboard = [
        [InlineKeyboardButton("🔑 API 管理", callback_data='menu_api')],
        [InlineKeyboardButton("✨ 预设管理", callback_data='menu_preset')],
        [InlineKeyboardButton("👑 访问控制", callback_data='menu_access')],
        [InlineKeyboardButton("⚙️ 查询模式切换", callback_data='menu_mode')],
        [InlineKeyboardButton("❌ 关闭菜单", callback_data='menu_close')]
    ]
    message_text = "⚙️ *设置菜单*"
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        query = update.callback_query; query.answer()
        query.edit_message_text(message_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    else:
        update.message.reply_text(message_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    return STATE_SETTINGS_MAIN

def settings_main_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer()
    menu = query.data.split('_', 1)[1]
    if menu == 'api': return show_api_menu(update, context)
    if menu == 'preset': return show_preset_menu(update, context)
    if menu == 'access': query.message.reply_text("访问控制功能待开发..."); return STATE_SETTINGS_MAIN # 占位
    if menu == 'mode': return toggle_full_mode(update, context)
    if menu == 'close': query.edit_message_text("菜单已关闭."); return ConversationHandler.END

# API 管理
def show_api_menu(update: Update, context: CallbackContext):
    api_list = "\n".join([f"`#{i+1}`: `...{k[-4:]}`" for i, k in enumerate(CONFIG['apis'])]) or "_无_"
    text = f"🔑 *API 管理*\n\n{api_list}"
    kbd = [[InlineKeyboardButton("➕ 添加", callback_data='api_add'), InlineKeyboardButton("➖ 移除", callback_data='api_remove')], [InlineKeyboardButton("🔙 返回", callback_data='api_back')]]
    update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kbd), parse_mode=ParseMode.MARKDOWN)
    return STATE_API_MENU

def api_menu_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); action = query.data.split('_', 1)[1]
    if action == 'back': return settings_command(update, context)
    if action == 'add': query.edit_message_text("请输入要添加的API Key:"); return STATE_GET_KEY
    if action == 'remove': query.edit_message_text("请输入要移除的Key的编号(#):"); return STATE_REMOVE_API

def get_api_key(update: Update, context: CallbackContext):
    CONFIG['apis'].append(update.message.text.strip()); save_config()
    update.message.reply_text("✅ API Key 添加成功！")
    return settings_command(update, context)

def remove_api_key(update: Update, context: CallbackContext):
    try:
        idx = int(update.message.text.strip()) - 1
        if 0 <= idx < len(CONFIG['apis']): CONFIG['apis'].pop(idx); save_config(); update.message.reply_text("✅ Key已移除。")
        else: update.message.reply_text("❌ 无效的编号。")
    except ValueError: update.message.reply_text("❌ 请输入数字编号。")
    return settings_command(update, context)

# 预设管理
def show_preset_menu(update: Update, context: CallbackContext):
    preset_list = "\n".join([f"`#{i+1}`: `{p['name']}`" for i, p in enumerate(CONFIG['presets'])]) or "_无_"
    text = f"✨ *预设管理*\n\n{preset_list}"
    kbd = [[InlineKeyboardButton("➕ 添加", callback_data='preset_add'), InlineKeyboardButton("➖ 移除", callback_data='preset_remove')], [InlineKeyboardButton("🔙 返回", callback_data='preset_back')]]
    update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kbd), parse_mode=ParseMode.MARKDOWN)
    return STATE_PRESET_MENU

def preset_menu_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); action = query.data.split('_', 1)[1]
    if action == 'back': return settings_command(update, context)
    if action == 'add': query.edit_message_text("请输入预设的名称 (例如: 海康威视摄像头):"); return STATE_GET_PRESET_NAME
    if action == 'remove': query.edit_message_text("请输入要移除的预设的编号(#):"); return STATE_REMOVE_PRESET

def get_preset_name(update: Update, context: CallbackContext):
    context.user_data['preset_name'] = update.message.text.strip()
    update.message.reply_text(f"名称: `{context.user_data['preset_name']}`\n\n现在请输入完整的FOFA查询语法:")
    return STATE_GET_PRESET_QUERY

def get_preset_query(update: Update, context: CallbackContext):
    new_preset = {"name": context.user_data['preset_name'], "query": update.message.text.strip()}
    CONFIG['presets'].append(new_preset); save_config()
    update.message.reply_text("✅ 预设添加成功！")
    context.user_data.clear()
    return settings_command(update, context)

def remove_preset(update: Update, context: CallbackContext):
    try:
        idx = int(update.message.text.strip()) - 1
        if 0 <= idx < len(CONFIG['presets']): CONFIG['presets'].pop(idx); save_config(); update.message.reply_text("✅ 预设已移除。")
        else: update.message.reply_text("❌ 无效的编号。")
    except ValueError: update.message.reply_text("❌ 请输入数字编号。")
    return settings_command(update, context)

# 模式切换
def toggle_full_mode(update: Update, context: CallbackContext):
    CONFIG['full_mode'] = not CONFIG.get('full_mode', False); save_config()
    mode_text = "完整模式 (full=true)" if CONFIG['full_mode'] else "精简模式 (默认)"
    update.callback_query.message.reply_text(f"✅ 查询模式已切换为: *{mode_text}*", parse_mode=ParseMode.MARKDOWN)
    return settings_command(update, context)

def cancel(update: Update, context: CallbackContext) -> int:
    update.message.reply_text('操作已取消。')
    context.user_data.clear()
    return ConversationHandler.END

# --- 主程序入口 ---
def main() -> None:
    bot_token = CONFIG.get("bot_token")
    if not bot_token or bot_token == "YOUR_BOT_TOKEN_HERE":
        logger.critical("严重错误：config.json 中的 'bot_token' 未设置！")
        return

    updater = Updater(token=bot_token, use_context=True)
    dispatcher = updater.dispatcher

    commands = [
        BotCommand("start", "欢迎与帮助"), BotCommand("kkfofa", "执行FOFA查询或查看预设"),
        BotCommand("stats", "获取FOFA全球资产统计"), BotCommand("settings", "(管理员) 打开设置菜单"),
        BotCommand("cancel", "取消当前操作"),
    ]
    try: updater.bot.set_my_commands(commands)
    except Exception as e: logger.warning(f"设置机器人命令失败: {e}")

    settings_conv = ConversationHandler(
        entry_points=[CommandHandler("settings", settings_command)],
        states={
            STATE_SETTINGS_MAIN: [CallbackQueryHandler(settings_main_callback, pattern=r"^menu_")],
            STATE_API_MENU: [CallbackQueryHandler(api_menu_callback, pattern=r"^api_")],
            STATE_GET_KEY: [MessageHandler(Filters.text & ~Filters.command, get_api_key)],
            STATE_REMOVE_API: [MessageHandler(Filters.text & ~Filters.command, remove_api_key)],
            STATE_PRESET_MENU: [CallbackQueryHandler(preset_menu_callback, pattern=r"^preset_")],
            STATE_GET_PRESET_NAME: [MessageHandler(Filters.text & ~Filters.command, get_preset_name)],
            STATE_GET_PRESET_QUERY: [MessageHandler(Filters.text & ~Filters.command, get_preset_query)],
            STATE_REMOVE_PRESET: [MessageHandler(Filters.text & ~Filters.command, remove_preset)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    
    stats_conv = ConversationHandler(
        entry_points=[CommandHandler("stats", stats_command)],
        states={STATE_GET_STATS_QUERY: [MessageHandler(Filters.text & ~Filters.command, get_fofa_stats_query)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    
    dispatcher.add_handler(CommandHandler("start", start_command))
    dispatcher.add_handler(CommandHandler("kkfofa", kkfofa_command))
    dispatcher.add_handler(settings_conv)
    dispatcher.add_handler(stats_conv)
    dispatcher.add_handler(CallbackQueryHandler(run_preset_callback, pattern=r"^run_preset_"))
    
    logger.info("功能增强版机器人已启动...")
    updater.start_polling()
    updater.idle()
    logger.info("机器人已关闭。")

if __name__ == "__main__":
    main()
