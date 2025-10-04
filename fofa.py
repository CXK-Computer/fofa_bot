#
# fofa.py (兼容 python-telegram-bot v13.x 版本)
#
import os
import json
import logging
import base64
import time
import re
import asyncio
from datetime import datetime, timedelta, timezone
from functools import wraps
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ParseMode
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackContext,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- 全局变量和常量 ---
CONFIG_FILE = 'config.json'
LOG_FILE = 'fofa_bot.log'

# --- 日志配置 ---
if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > (5 * 1024 * 1024): # 5MB
    try:
        os.rename(LOG_FILE, LOG_FILE + '.old')
    except OSError as e:
        print(f"无法轮换日志文件: {e}")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)
logger = logging.getLogger(__name__)

# --- 会话状态定义 ---
(
    STATE_SETTINGS_MAIN,
    STATE_SETTINGS_ACTION,
    STATE_GET_KEY,
    STATE_GET_PROXY,
    STATE_REMOVE_API,
    STATE_ACCESS_CONTROL,
    STATE_ADD_ADMIN,
    STATE_REMOVE_ADMIN,
) = range(8)

# --- 配置管理 ---
def load_config():
    default_config = {
        "bot_token": "YOUR_BOT_TOKEN_HERE",
        "apis": [],
        "admins": [],
        "proxy": "",
        "full_mode": False,
        "public_mode": False
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(default_config, f, indent=4)
        logger.info(f"配置文件 {CONFIG_FILE} 不存在，已创建默认配置。")
        return default_config
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
            # 兼容性检查：确保所有新字段都存在
            for key, value in default_config.items():
                if key not in config:
                    config[key] = value
            return config
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"读取 {CONFIG_FILE} 失败: {e}。将使用默认配置。")
        return default_config

def save_config():
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(CONFIG, f, indent=4)
    except IOError as e:
        logger.error(f"保存配置文件失败: {e}")

CONFIG = load_config()

# --- 装饰器：管理员权限检查 ---
def admin_only(func):
    @wraps(func)
    def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in CONFIG.get('admins', []):
            if update.message:
                update.message.reply_text("⛔️ 抱歉，您没有权限执行此管理操作。")
            elif update.callback_query:
                update.callback_query.answer("⛔️ 权限不足", show_alert=True)
            return None
        return func(update, context, *args, **kwargs)
    return wrapped

# --- FOFA API 核心逻辑 ---
async def _make_request_async(url: str):
    proxy_str = ""
    if CONFIG.get("proxy"):
        proxy_str = f'--proxy "{CONFIG["proxy"]}"'
    command = f'curl -s -L -k {proxy_str} "{url}"'
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return None, f"网络请求失败 (curl): {stderr.decode().strip()}"
        response_text = stdout.decode()
        if not response_text:
            return None, "API 返回了空响应。"
        data = json.loads(response_text)
        if data.get("error"):
            return None, data.get("errmsg", "未知的FOFA错误")
        return data, None
    except json.JSONDecodeError:
        return None, f"解析JSON响应失败: {response_text[:200]}"
    except Exception as e:
        return None, f"执行curl时发生意外错误: {e}"

async def verify_fofa_api(key):
    url = f"https://fofa.info/api/v1/info/my?key={key}"
    return await _make_request_async(url)

async def fetch_fofa_data(key, query, page=1, page_size=10000, fields="host"):
    b64_query = base64.b64encode(query.encode('utf-8')).decode('utf-8')
    full_param = "&full=true" if CONFIG.get("full_mode", False) else ""
    url = f"https://fofa.info/api/v1/search/all?key={key}&qbase64={b64_query}&size={page_size}&page={page}&fields={fields}{full_param}"
    return await _make_request_async(url)

# --- 命令处理函数 ---
def start_command(update: Update, context: CallbackContext) -> None:
    user = update.effective_user
    update.message.reply_html(
        rf"你好, {user.mention_html()}!",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 开始查询", callback_data="start_query")]
        ])
    )

def kkfofa_command_entry(update: Update, context: CallbackContext) -> None:
    user_id = update.effective_user.id
    if not CONFIG.get('public_mode', False) and user_id not in CONFIG.get('admins', []):
        update.message.reply_text("⛔️ 抱歉，此机器人当前为私有模式，您没有权限进行查询。")
        return

    query_text = " ".join(context.args) if context.args else update.message.text
    if not query_text or query_text.startswith('/'):
        update.message.reply_text("请输入您的FOFA查询语句。例如：\n`/kkfofa domain=example.com`\n或者直接发送查询语句。")
        return

    # 使用 context.bot_data 存储查询信息
    context.bot_data[user_id] = {'query': query_text}
    asyncio.run(execute_fofa_query(update, context))

async def execute_fofa_query(update: Update, context: CallbackContext) -> None:
    user_id = update.effective_user.id
    query_text = context.bot_data.get(user_id, {}).get('query')
    if not query_text: return
    
    msg = await update.message.reply_text("🔍 正在查询中，请稍候...")

    async def query_func(key):
        return await fetch_fofa_data(key, query_text)
    
    data, used_key_index, error = await execute_query_with_fallback(query_func)

    if error:
        await msg.edit_text(f"❌ 查询失败！\n错误信息: `{error}`", parse_mode=ParseMode.MARKDOWN)
        return

    results = data.get('results', [])
    filename = f"fofa_results_{int(time.time())}.txt"
    with open(filename, 'w', encoding='utf-8') as f:
        for item in results:
            f.write(f"{item}\n")

    caption = (f"✅ 查询完成！\n"
               f"语法: `{query_text}`\n"
               f"共找到 `{len(results)}` 条结果\n"
               f"使用 Key: `#{used_key_index}`")

    await msg.delete()
    with open(filename, 'rb') as f:
        await context.bot.send_document(chat_id=update.effective_chat.id, document=f, caption=caption, parse_mode=ParseMode.MARKDOWN)
    os.remove(filename)

async def execute_query_with_fallback(query_func):
    if not CONFIG['apis']:
        return None, None, "没有配置任何API Key。"
    
    tasks = [verify_fofa_api(key) for key in CONFIG['apis']]
    results = await asyncio.gather(*tasks)
    
    valid_keys = [{'key': CONFIG['apis'][i], 'index': i + 1, 'is_vip': data.get('is_vip', False)} 
                  for i, (data, error) in enumerate(results) if not error and data]

    if not valid_keys:
        return None, None, "所有API Key均无效或验证失败。"
    
    prioritized_keys = sorted(valid_keys, key=lambda x: x['is_vip'], reverse=True)
    
    last_error = "没有可用的API Key。"
    for key_info in prioritized_keys:
        data, error = await query_func(key_info['key'])
        if not error:
            return data, key_info['index'], None
        last_error = error
        if "[820031]" in str(error):
            logger.warning(f"Key [#{key_info['index']}] F点余额不足，尝试下一个...")
            continue
        return None, key_info['index'], error
        
    return None, None, f"所有Key均尝试失败，最后错误: {last_error}"

def status_command(update: Update, context: CallbackContext) -> None:
    asyncio.run(check_api_status(update, context))

async def check_api_status(update: Update, context: CallbackContext) -> None:
    if not CONFIG.get('apis'):
        await update.message.reply_text("ℹ️ 当前没有配置任何API Key。")
        return
    
    msg = await update.message.reply_text("📊 正在检查所有API Key状态，请稍候...")
    
    tasks = [verify_fofa_api(key) for key in CONFIG['apis']]
    results = await asyncio.gather(*tasks)
    
    status_lines = []
    for i, (data, error) in enumerate(results):
        key_masked = CONFIG['apis'][i][:4] + '...' + CONFIG['apis'][i][-4:]
        if error:
            status_lines.append(f"🔴 Key #{i+1} (`{key_masked}`): **验证失败**\n   错误: `{error}`")
        else:
            email = data.get('email', 'N/A')
            is_vip = "是" if data.get('is_vip') else "否"
            fcoin = data.get('fcoin', 'N/A')
            status_lines.append(f"🟢 Key #{i+1} (`{key_masked}`): **有效**\n   邮箱: `{email}`, VIP: `{is_vip}`, F点: `{fcoin}`")
            
    response_text = "📊 **API Key 状态报告**\n\n" + "\n\n".join(status_lines)
    await msg.edit_text(response_text, parse_mode=ParseMode.MARKDOWN)


def cancel(update: Update, context: CallbackContext) -> int:
    update.message.reply_text('操作已取消。')
    return ConversationHandler.END

# --- 设置菜单 (ConversationHandler) ---
@admin_only
def settings_command(update: Update, context: CallbackContext) -> int:
    keyboard = [
        [InlineKeyboardButton("🔑 API 管理", callback_data='settings_api')],
        [InlineKeyboardButton("🌐 代理设置", callback_data='settings_proxy')],
        [InlineKeyboardButton("👑 访问控制", callback_data='settings_access')],
        [InlineKeyboardButton("⚙️ 查询模式", callback_data='settings_mode')],
        [InlineKeyboardButton("❌ 关闭菜单", callback_data='settings_close')]
    ]
    message_text = "⚙️ *设置菜单*"
    if update.callback_query:
        update.callback_query.answer()
        update.callback_query.edit_message_text(
            message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN
        )
    else:
        update.message.reply_text(
            message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN
        )
    return STATE_SETTINGS_MAIN

def settings_main_handler(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    query.answer()
    menu = query.data.split('_', 1)[1]

    if menu == 'api':
        return show_api_menu(update, context)
    elif menu == 'proxy':
        return show_proxy_menu(update, context)
    elif menu == 'access':
        return show_access_control_menu(update, context)
    elif menu == 'mode':
        return toggle_full_mode(update, context)
    elif menu == 'close':
        query.edit_message_text("菜单已关闭。")
        return ConversationHandler.END

def settings_action_handler(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    query.answer()
    action = query.data.split('_', 1)[1]

    if action == 'back_main':
        return settings_command(update, context)
    elif action == 'add_key':
        query.edit_message_text("请输入要添加的FOFA API Key:")
        return STATE_GET_KEY
    elif action == 'remove_key':
        query.edit_message_text("请输入要移除的API Key的编号 (#):")
        return STATE_REMOVE_API
    elif action == 'set_proxy':
        query.edit_message_text("请输入新的代理地址 (例如: http://127.0.0.1:7890)。输入 '清除' 来移除代理。")
        return STATE_GET_PROXY

# API 管理
def show_api_menu(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    api_list_str = "\n".join([f"`#{i+1}`: `{key[:4]}...{key[-4:]}`" for i, key in enumerate(CONFIG['apis'])]) or "无"
    message_text = f"🔑 *API Key 管理*\n\n当前 Keys:\n{api_list_str}"
    keyboard = [
        [InlineKeyboardButton("➕ 添加 Key", callback_data='action_add_key'), InlineKeyboardButton("➖ 移除 Key", callback_data='action_remove_key')],
        [InlineKeyboardButton("🔙 返回", callback_data='action_back_main')]
    ]
    query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return STATE_SETTINGS_ACTION

# 代理管理
def show_proxy_menu(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    proxy_status = f"`{CONFIG.get('proxy')}`" if CONFIG.get('proxy') else "未设置"
    message_text = f"🌐 *代理设置*\n\n当前代理: {proxy_status}"
    keyboard = [
        [InlineKeyboardButton("✏️ 修改/设置代理", callback_data='action_set_proxy')],
        [InlineKeyboardButton("🔙 返回", callback_data='action_back_main')]
    ]
    query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return STATE_SETTINGS_ACTION

# 访问控制
def show_access_control_menu(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    public_mode_status = "✅ 公共模式 (任何人可查询)" if CONFIG.get('public_mode', False) else "❌ 私有模式 (仅管理员可查询)"
    admin_list = "\n".join([f"`{admin_id}`" for admin_id in CONFIG.get('admins', [])]) or "_无_"
    message_text = f"👑 *访问控制*\n\n**当前模式**: {public_mode_status}\n\n**管理员列表**:\n{admin_list}"
    keyboard = [
        [InlineKeyboardButton("🔄 切换模式", callback_data='access_toggle_public')],
        [InlineKeyboardButton("➕ 添加管理员", callback_data='access_add_admin'), InlineKeyboardButton("➖ 删除管理员", callback_data='access_remove_admin')],
        [InlineKeyboardButton("🔙 返回主菜单", callback_data='access_back_main')]
    ]
    query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return STATE_ACCESS_CONTROL

def access_control_callback_handler(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    query.answer()
    action = query.data.split('_', 1)[1]

    if action == 'back_main':
        return settings_command(update, context)
    elif action == 'toggle_public':
        CONFIG['public_mode'] = not CONFIG.get('public_mode', False)
        save_config()
        return show_access_control_menu(update, context) # Refresh menu
    elif action == 'add_admin':
        query.edit_message_text("请输入要添加的管理员Telegram用户ID。")
        return STATE_ADD_ADMIN
    elif action == 'remove_admin':
        if len(CONFIG.get('admins', [])) <= 1:
            query.message.reply_text("❌ 不能删除最后一个管理员。")
            return show_access_control_menu(update, context)
        query.edit_message_text("请输入要删除的管理员Telegram用户ID。")
        return STATE_REMOVE_ADMIN

# 查询模式
def toggle_full_mode(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    query.answer()
    CONFIG['full_mode'] = not CONFIG.get('full_mode', False)
    save_config()
    mode_text = "完整模式 (full=true)" if CONFIG['full_mode'] else "精简模式 (默认)"
    query.message.reply_text(f"✅ 查询模式已切换为: **{mode_text}**", parse_mode=ParseMode.MARKDOWN)
    return settings_command(update, context)

# 状态处理函数
def get_key(update: Update, context: CallbackContext) -> int:
    new_key = update.message.text.strip()
    if new_key not in CONFIG['apis']:
        CONFIG['apis'].append(new_key)
        save_config()
        update.message.reply_text(f"✅ API Key `{new_key[:4]}...` 添加成功！")
    else:
        update.message.reply_text("ℹ️ 这个Key已经存在了。")
    return settings_command(update, context)

def remove_api(update: Update, context: CallbackContext) -> int:
    try:
        key_index = int(update.message.text.strip()) - 1
        if 0 <= key_index < len(CONFIG['apis']):
            removed_key = CONFIG['apis'].pop(key_index)
            save_config()
            update.message.reply_text(f"✅ 已移除 Key #{key_index+1} (`{removed_key[:4]}...`)。")
        else:
            update.message.reply_text("❌ 无效的编号。")
    except ValueError:
        update.message.reply_text("❌ 请输入一个纯数字编号。")
    return settings_command(update, context)

def get_proxy(update: Update, context: CallbackContext) -> int:
    proxy_text = update.message.text.strip()
    if proxy_text.lower() == '清除':
        CONFIG['proxy'] = ""
        update.message.reply_text("✅ 代理已清除。")
    else:
        CONFIG['proxy'] = proxy_text
        update.message.reply_text(f"✅ 代理已设置为: `{proxy_text}`")
    save_config()
    return settings_command(update, context)

def add_admin_handler(update: Update, context: CallbackContext) -> int:
    try:
        new_admin_id = int(update.message.text.strip())
        if new_admin_id not in CONFIG['admins']:
            CONFIG['admins'].append(new_admin_id)
            save_config()
            update.message.reply_text(f"✅ 管理员 `{new_admin_id}` 添加成功！")
        else:
            update.message.reply_text(f"ℹ️ 用户 `{new_admin_id}` 已经是管理员了。")
    except ValueError:
        update.message.reply_text("❌ 无效的ID，请输入纯数字的用户ID。")
    return settings_command(update, context)

def remove_admin_handler(update: Update, context: CallbackContext) -> int:
    try:
        admin_id_to_remove = int(update.message.text.strip())
        if len(CONFIG['admins']) <= 1 and admin_id_to_remove in CONFIG['admins']:
             update.message.reply_text("❌ 不能删除最后一个管理员。")
        elif admin_id_to_remove in CONFIG['admins']:
            CONFIG['admins'].remove(admin_id_to_remove)
            save_config()
            update.message.reply_text(f"✅ 管理员 `{admin_id_to_remove}` 已被移除。")
        else:
            update.message.reply_text(f"❌ 用户 `{admin_id_to_remove}` 不是管理员。")
    except ValueError:
        update.message.reply_text("❌ 无效的ID，请输入纯数字的用户ID。")
    return settings_command(update, context)


# --- 主程序入口 ---
def main() -> None:
    bot_token = CONFIG.get("bot_token")
    if not bot_token or bot_token == "YOUR_BOT_TOKEN_HERE":
        logger.critical("严重错误：config.json 中的 'bot_token' 未设置！请修改配置文件后重启。")
        return

    # 使用旧版的 Updater 和 Dispatcher
    updater = Updater(token=bot_token, use_context=True)
    dispatcher = updater.dispatcher

    # 设置机器人命令菜单
    commands = [
        BotCommand("start", "🚀 启动机器人"),
        BotCommand("kkfofa", "🔎 执行FOFA查询"),
        BotCommand("status", "📊 检查API Key状态"),
        BotCommand("settings", "⚙️ 打开设置菜单"),
        BotCommand("cancel", "❌ 取消当前操作"),
    ]
    updater.bot.set_my_commands(commands)

    # 设置会话处理器
    settings_conv = ConversationHandler(
        entry_points=[CommandHandler("settings", settings_command)],
        states={
            STATE_SETTINGS_MAIN: [CallbackQueryHandler(settings_main_handler, pattern=r"^settings_")],
            STATE_SETTINGS_ACTION: [CallbackQueryHandler(settings_action_handler, pattern=r"^action_")],
            STATE_ACCESS_CONTROL: [CallbackQueryHandler(access_control_callback_handler, pattern=r"^access_")],
            STATE_ADD_ADMIN: [MessageHandler(filters.Text & ~filters.COMMAND, add_admin_handler)],
            STATE_REMOVE_ADMIN: [MessageHandler(filters.Text & ~filters.COMMAND, remove_admin_handler)],
            STATE_GET_KEY: [MessageHandler(filters.Text & ~filters.COMMAND, get_key)],
            STATE_GET_PROXY: [MessageHandler(filters.Text & ~filters.COMMAND, get_proxy)],
            STATE_REMOVE_API: [MessageHandler(filters.Text & ~filters.COMMAND, remove_api)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )
    
    # 直接查询处理器
    kkfofa_handler = CommandHandler("kkfofa", kkfofa_command_entry)
    direct_query_handler = MessageHandler(filters.Text & ~filters.COMMAND, kkfofa_command_entry)

    # 注册处理器到 dispatcher
    dispatcher.add_handler(CommandHandler("start", start_command))
    dispatcher.add_handler(CommandHandler("status", status_command))
    dispatcher.add_handler(settings_conv)
    dispatcher.add_handler(kkfofa_handler)
    dispatcher.add_handler(direct_query_handler) 

    # 同步启动机器人
    logger.info("机器人已启动，正在等待消息...")
    updater.start_polling()

    # 优雅地停止
    updater.idle()
    logger.info("机器人已关闭。")


if __name__ == "__main__":
    main()
