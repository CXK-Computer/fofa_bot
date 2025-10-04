#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import logging
import base64
import time
import re
import asyncio
from datetime import datetime, timezone
from functools import wraps
from typing import Optional, Dict, Tuple

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
)

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- 全局变量和常量 ---
CONFIG_FILE = 'config.json'
HISTORY_FILE = 'history.json'
LOG_FILE = 'fofa_bot.log'
MAX_HISTORY_SIZE = 50

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
logging.getLogger("telegram.ext").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- 会话状态定义 ---
(
    STATE_KKFOFA_MODE,
    STATE_SETTINGS_MAIN,
    STATE_SETTINGS_ACTION,
    STATE_GET_KEY,
    STATE_GET_PROXY,
    STATE_REMOVE_API,
    STATE_ACCESS_CONTROL,
    STATE_ADD_ADMIN,
    STATE_REMOVE_ADMIN,
) = range(9)

# --- 配置与历史记录管理 ---
def load_json_file(filename: str, default_content: Dict) -> Dict:
    if not os.path.exists(filename):
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(default_content, f, indent=4, ensure_ascii=False)
        return default_content
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        logger.error(f"{filename} 损坏或无法读取，将使用默认配置重建。")
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(default_content, f, indent=4, ensure_ascii=False)
        return default_content

def save_json_file(filename: str, data: Dict):
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

# 默认管理员ID，请根据需要修改或在程序中添加
default_admin_id = 123456789 # 这是一个示例ID，请替换
DEFAULT_CONFIG = {
    "bot_token": "YOUR_BOT_TOKEN_HERE", # <--- 在这里或在config.json中填入你的Token
    "apis": [],
    "admins": [default_admin_id],
    "proxy": "",
    "full_mode": False,
    "public_mode": False
}
CONFIG = load_json_file(CONFIG_FILE, DEFAULT_CONFIG)

# 确保旧配置文件有新字段
if 'public_mode' not in CONFIG: CONFIG['public_mode'] = False
if 'bot_token' not in CONFIG: CONFIG['bot_token'] = "YOUR_BOT_TOKEN_HERE"

HISTORY = load_json_file(HISTORY_FILE, {"queries": []})

def save_config(): save_json_file(CONFIG_FILE, CONFIG)
def save_history(): save_json_file(HISTORY_FILE, HISTORY)

def add_or_update_query(query_text: str):
    existing_query = next((q for q in HISTORY['queries'] if q['query_text'] == query_text), None)
    if existing_query:
        HISTORY['queries'].remove(existing_query)
    new_query = {"query_text": query_text, "timestamp": datetime.now(timezone.utc).isoformat()}
    HISTORY['queries'].insert(0, new_query)
    while len(HISTORY['queries']) > MAX_HISTORY_SIZE:
        HISTORY['queries'].pop()
    save_history()

# --- 辅助函数与装饰器 ---
def escape_markdown(text: str) -> str:
    escape_chars = r'_*`[]()~>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def admin_only(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in CONFIG.get('admins', []):
            if update.message:
                await update.message.reply_text("⛔️ 抱歉，您没有权限执行此管理操作。")
            elif update.callback_query:
                await update.callback_query.answer("⛔️ 权限不足", show_alert=True)
            return ConversationHandler.END # 结束会话
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- FOFA API 核心逻辑 ---
async def _make_request_async(url: str) -> Tuple[Optional[Dict], Optional[str]]:
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
            return None, f"网络请求失败 (curl): {stderr.decode(errors='ignore').strip()}"
        response_text = stdout.decode(errors='ignore')
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

async def verify_fofa_api(key: str) -> Tuple[Optional[Dict], Optional[str]]:
    url = f"https://fofa.info/api/v1/info/my?key={key}"
    return await _make_request_async(url)

async def fetch_fofa_data(key: str, query: str) -> Tuple[Optional[Dict], Optional[str]]:
    b64_query = base64.b64encode(query.encode('utf-8')).decode('utf-8')
    full_param = "&full=true" if CONFIG.get("full_mode", False) else ""
    # 默认查询10000条数据，字段仅host
    url = f"https://fofa.info/api/v1/search/all?key={key}&qbase64={b64_query}&size=10000&fields=host{full_param}"
    return await _make_request_async(url)

async def execute_query_with_fallback(query_func, *args):
    if not CONFIG['apis']:
        return None, None, "没有配置任何API Key。"
    
    tasks = [verify_fofa_api(key) for key in CONFIG['apis']]
    results = await asyncio.gather(*tasks)
    
    valid_keys = [
        {'key': CONFIG['apis'][i], 'index': i + 1, 'is_vip': data.get('is_vip', False)}
        for i, (data, error) in enumerate(results) if not error and data
    ]
    
    if not valid_keys:
        return None, None, "所有API Key均无效或验证失败。"
        
    prioritized_keys = sorted(valid_keys, key=lambda x: x['is_vip'], reverse=True)
    
    last_error = "没有可用的API Key。"
    for key_info in prioritized_keys:
        data, error = await query_func(key_info['key'], *args)
        if not error:
            return data, key_info['index'], None
        last_error = error
        if "[820031]" in str(error): # F点余额不足
            logger.warning(f"Key [#{key_info['index']}] F点余额不足，尝试下一个...")
            continue
        return None, key_info['index'], error
        
    return None, None, f"所有Key均尝试失败，最后错误: {last_error}"

# --- 命令处理 ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    start_text = (
        f"👋 你好, {user.first_name}！\n\n"
        "欢迎使用 FOFA 查询机器人。\n"
        "发送 /kkfofa `查询语句` 或直接发送查询语句即可开始。\n\n"
        "例如: `app=\"nginx\" && port=\"443\"`\n\n"
        "管理员可以使用 /settings 进入设置菜单。"
    )
    await update.message.reply_text(start_text, parse_mode=ParseMode.MARKDOWN)

@admin_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """手动检查所有API Key的状态"""
    if not CONFIG.get('apis'):
        await update.message.reply_text("ℹ️ 当前没有配置任何 API Key。")
        return

    msg = await update.message.reply_text("🔍 正在检查所有API Key状态，请稍候...")

    tasks = [verify_fofa_api(key) for key in CONFIG['apis']]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    status_lines = ["*📊 API Key 状态报告*"]
    all_ok = True
    for i, res in enumerate(results):
        key_masked = CONFIG['apis'][i][:4] + '...' + CONFIG['apis'][i][-4:]
        line = f"\n`Key #{i+1}` ({escape_markdown(key_masked)}): "
        if isinstance(res, Exception):
            line += f"NETWORK_ERROR - {escape_markdown(str(res))}"
            all_ok = False
        elif res[1] is not None:
            line += f"❌ *无效* - {escape_markdown(res[1])}"
            all_ok = False
        elif res[0] is not None:
            data = res[0]
            fpoints = data.get('fofa_point', 'N/A')
            is_vip = "✅ VIP" if data.get('is_vip') else "☑️ 普通"
            email = escape_markdown(data.get('email', 'N/A'))
            line += f"{is_vip}, F点: *{fpoints}*, 邮箱: {email}"
            if isinstance(fpoints, int) and fpoints < 100:
                 line += " (⚠️*F点较低*)"
        status_lines.append(line)

    if all_ok:
        status_lines.append("\n✅ 所有Key均可正常使用。")

    await msg.edit_text("\n".join(status_lines), parse_mode=ParseMode.MARKDOWN)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """取消并结束会话"""
    await update.message.reply_text("操作已取消。")
    return ConversationHandler.END

# --- FOFA 查询会话 ---
async def kkfofa_command_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not CONFIG.get('public_mode', False) and user_id not in CONFIG.get('admins', []):
        await update.message.reply_text("⛔️ 抱歉，此机器人当前为私有模式，您没有权限进行查询。")
        return ConversationHandler.END

    query = ' '.join(context.args) if context.args else update.message.text
    if update.message.text.startswith('/'):
        # 如果是命令，则去掉命令本身
        parts = update.message.text.split(maxsplit=1)
        query = parts[1] if len(parts) > 1 else ""

    if not query:
        await update.message.reply_text("请输入您的FOFA查询语句。例如：`/kkfofa app=\"nginx\"`")
        return ConversationHandler.END

    context.user_data['query'] = query
    await process_fofa_query(update, context)
    return ConversationHandler.END

async def process_fofa_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = context.user_data['query']
    msg = await update.message.reply_text(f"正在查询: `{query}`\n请稍候...", parse_mode=ParseMode.MARKDOWN)
    
    add_or_update_query(query)
    
    data, used_key_index, error = await execute_query_with_fallback(fetch_fofa_data, query)

    if error:
        await msg.edit_text(f"查询失败 😞 (使用 Key #{used_key_index})\n错误: `{error}`", parse_mode=ParseMode.MARKDOWN)
        return

    results = data.get('results', [])
    if not results:
        await msg.edit_text(f"查询 `{query}` 没有找到任何结果。", parse_mode=ParseMode.MARKDOWN)
        return
        
    size = data.get('size', len(results))
    filename = f"fofa_results_{int(time.time())}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        for item in results:
            f.write(f"{item}\n")
    
    caption = (f"✅ 查询成功 (使用 Key #{used_key_index})\n"
               f"语句: `{escape_markdown(query)}`\n"
               f"共找到 *{size}* 条结果。")

    await update.message.reply_document(
        document=open(filename, 'rb'),
        caption=caption,
        parse_mode=ParseMode.MARKDOWN
    )
    await msg.delete()
    os.remove(filename)

# --- 设置会话 ---
@admin_only
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [
        [InlineKeyboardButton("🔑 API 管理", callback_data='settings_api')],
        [InlineKeyboardButton("🌐 代理设置", callback_data='settings_proxy')],
        [InlineKeyboardButton("👑 访问控制", callback_data='settings_access')],
        [InlineKeyboardButton("模式切换 (Full)", callback_data='settings_toggle_full')],
        [InlineKeyboardButton("📜 查询历史", callback_data='settings_history')],
    ]
    message_text = "⚙️ *设置菜单*"
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(message_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    return STATE_SETTINGS_MAIN

async def settings_main_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    menu = query.data.split('_', 1)[1]

    if menu == 'api':
        await show_api_menu(update, context)
        return STATE_SETTINGS_ACTION
    elif menu == 'proxy':
        await show_proxy_menu(update, context)
        return STATE_SETTINGS_ACTION
    elif menu == 'access':
        await show_access_control_menu(update, context)
        return STATE_ACCESS_CONTROL
    elif menu == 'toggle_full':
        CONFIG['full_mode'] = not CONFIG.get('full_mode', False)
        save_config()
        status = "✅ 开启" if CONFIG['full_mode'] else "❌ 关闭"
        await query.answer(f"Full模式已{status}", show_alert=True)
        return STATE_SETTINGS_MAIN
    elif menu == 'history':
        if not HISTORY['queries']:
            await query.message.reply_text("查询历史为空。")
        else:
            history_text = "*最近查询历史:*\n" + "\n".join(
                f"`{idx+1}`: `{escape_markdown(q['query_text'])}`"
                for idx, q in enumerate(HISTORY['queries'][:10])
            )
            await query.message.reply_text(history_text, parse_mode=ParseMode.MARKDOWN)
        return STATE_SETTINGS_MAIN

# API & Proxy Menus
async def show_api_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_list = "\n".join([f"`{i+1}`: `{key[:4]}...{key[-4:]}`" for i, key in enumerate(CONFIG['apis'])]) or "_无_"
    text = f"🔑 *API Key 管理*\n\n当前 Keys:\n{api_list}"
    keyboard = [
        [InlineKeyboardButton("➕ 添加", callback_data='action_add_api'), InlineKeyboardButton("➖ 删除", callback_data='action_remove_api')],
        [InlineKeyboardButton("🔙 返回", callback_data='action_back_main')]
    ]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def show_proxy_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    proxy = f"`{CONFIG.get('proxy')}`" if CONFIG.get('proxy') else "_未设置_"
    text = f"🌐 *代理设置*\n\n当前代理: {proxy}"
    keyboard = [
        [InlineKeyboardButton("✏️ 设置/修改", callback_data='action_set_proxy'), InlineKeyboardButton("🗑️ 清除", callback_data='action_clear_proxy')],
        [InlineKeyboardButton("🔙 返回", callback_data='action_back_main')]
    ]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def settings_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split('_', 1)[1]

    if action == 'back_main':
        await settings_command(update, context)
        return STATE_SETTINGS_MAIN
    elif action == 'add_api':
        await query.edit_message_text("请输入要添加的FOFA API Key:")
        return STATE_GET_KEY
    elif action == 'remove_api':
        await query.edit_message_text("请输入要删除的API Key的序号:")
        return STATE_REMOVE_API
    elif action == 'set_proxy':
        await query.edit_message_text("请输入新的代理地址 (格式: http://user:pass@host:port):")
        return STATE_GET_PROXY
    elif action == 'clear_proxy':
        CONFIG['proxy'] = ""
        save_config()
        await query.message.reply_text("✅ 代理已清除。")
        await asyncio.sleep(1)
        await show_proxy_menu(update, context)
        return STATE_SETTINGS_ACTION

async def get_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = update.message.text.strip()
    data, error = await verify_fofa_api(key)
    if error:
        await update.message.reply_text(f"❌ 添加失败，Key无效: {error}")
    else:
        CONFIG['apis'].append(key)
        save_config()
        await update.message.reply_text("✅ API Key 添加成功！")
    await asyncio.sleep(1)
    # Re-show menu in a new message
    await settings_command(update, context)
    return STATE_SETTINGS_MAIN

async def remove_api(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        index = int(update.message.text.strip()) - 1
        if 0 <= index < len(CONFIG['apis']):
            removed_key = CONFIG['apis'].pop(index)
            save_config()
            await update.message.reply_text(f"✅ Key #{index+1} (`{removed_key[:4]}...`) 已被移除。")
        else:
            await update.message.reply_text("❌ 无效的序号。")
    except ValueError:
        await update.message.reply_text("❌ 请输入数字序号。")
    await asyncio.sleep(1)
    await settings_command(update, context)
    return STATE_SETTINGS_MAIN

async def get_proxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    CONFIG['proxy'] = update.message.text.strip()
    save_config()
    await update.message.reply_text(f"✅ 代理已更新为: `{CONFIG['proxy']}`")
    await asyncio.sleep(1)
    await settings_command(update, context)
    return STATE_SETTINGS_MAIN

# Access Control Menus
async def show_access_control_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    public_mode_status = "✅ 公共模式 (任何人可查询)" if CONFIG.get('public_mode', False) else "❌ 私有模式 (仅管理员可查询)"
    admin_list = "\n".join([f"`{admin_id}`" for admin_id in CONFIG.get('admins', [])]) or "_无_"
    message_text = f"👑 *访问控制*\n\n**当前模式**: {public_mode_status}\n\n**管理员列表**:\n{admin_list}"
    keyboard = [
        [InlineKeyboardButton("🔄 切换模式", callback_data='access_toggle_public')],
        [InlineKeyboardButton("➕ 添加管理员", callback_data='access_add_admin'), InlineKeyboardButton("➖ 删除管理员", callback_data='access_remove_admin')],
        [InlineKeyboardButton("🔙 返回主菜单", callback_data='access_back_main')]
    ]
    await update.callback_query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def access_control_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split('_', 1)[1]

    if action == 'back_main':
        await settings_command(update, context)
        return STATE_SETTINGS_MAIN
    elif action == 'toggle_public':
        CONFIG['public_mode'] = not CONFIG.get('public_mode', False)
        save_config()
        await show_access_control_menu(update, context)
        return STATE_ACCESS_CONTROL
    elif action == 'add_admin':
        await query.edit_message_text("请输入要添加的管理员Telegram用户ID:")
        return STATE_ADD_ADMIN
    elif action == 'remove_admin':
        if len(CONFIG.get('admins', [])) <= 1:
            await query.answer("❌ 不能删除最后一个管理员。", show_alert=True)
            return STATE_ACCESS_CONTROL
        await query.edit_message_text("请输入要删除的管理员Telegram用户ID:")
        return STATE_REMOVE_ADMIN

async def add_admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_admin_id = int(update.message.text.strip())
        if new_admin_id not in CONFIG['admins']:
            CONFIG['admins'].append(new_admin_id)
            save_config()
            await update.message.reply_text(f"✅ 管理员 `{new_admin_id}` 添加成功！")
        else:
            await update.message.reply_text(f"ℹ️ 用户 `{new_admin_id}` 已经是管理员了。")
    except ValueError:
        await update.message.reply_text("❌ 无效的ID，请输入纯数字的用户ID。")
    await asyncio.sleep(1)
    await settings_command(update, context)
    return STATE_SETTINGS_MAIN

async def remove_admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        admin_id_to_remove = int(update.message.text.strip())
        if len(CONFIG['admins']) <= 1 and admin_id_to_remove in CONFIG['admins']:
             await update.message.reply_text("❌ 不能删除最后一个管理员。")
        elif admin_id_to_remove in CONFIG['admins']:
            CONFIG['admins'].remove(admin_id_to_remove)
            save_config()
            await update.message.reply_text(f"✅ 管理员 `{admin_id_to_remove}` 已被移除。")
        else:
            await update.message.reply_text(f"❌ 用户 `{admin_id_to_remove}` 不是管理员。")
    except ValueError:
        await update.message.reply_text("❌ 无效的ID，请输入纯数字的用户ID。")
    await asyncio.sleep(1)
    await settings_command(update, context)
    return STATE_SETTINGS_MAIN

# --- 主程序入口 ---
async def main() -> None:
    bot_token = CONFIG.get("bot_token")
    if not bot_token or bot_token == "YOUR_BOT_TOKEN_HERE":
        logger.critical("严重错误：config.json 中的 'bot_token' 未设置！请修改配置文件后重启。")
        return

    application = Application.builder().token(bot_token).build()

    # 设置机器人命令菜单
    commands = [
        BotCommand("start", "🚀 启动机器人"),
        BotCommand("kkfofa", "🔎 执行FOFA查询"),
        BotCommand("status", "📊 检查API Key状态"),
        BotCommand("settings", "⚙️ 打开设置菜单"),
        BotCommand("cancel", "❌ 取消当前操作"),
    ]
    await application.bot.set_my_commands(commands)

    # 设置会话处理器
    settings_conv = ConversationHandler(
        entry_points=[CommandHandler("settings", settings_command)],
        states={
            STATE_SETTINGS_MAIN: [CallbackQueryHandler(settings_main_handler, pattern=r"^settings_")],
            STATE_SETTINGS_ACTION: [CallbackQueryHandler(settings_action_handler, pattern=r"^action_")],
            STATE_ACCESS_CONTROL: [CallbackQueryHandler(access_control_callback_handler, pattern=r"^access_")],
            STATE_ADD_ADMIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_admin_handler)],
            STATE_REMOVE_ADMIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, remove_admin_handler)],
            STATE_GET_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_key)],
            STATE_GET_PROXY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_proxy)],
            STATE_REMOVE_API: [MessageHandler(filters.TEXT & ~filters.COMMAND, remove_api)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )
    
    # 直接查询处理器
    # 匹配 /kkfofa 命令或非命令的普通文本消息
    kkfofa_handler = CommandHandler("kkfofa", kkfofa_command_entry)
    direct_query_handler = MessageHandler(filters.TEXT & ~filters.COMMAND, kkfofa_command_entry)

    # 注册处理器
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(settings_conv)
    application.add_handler(kkfofa_handler)
    application.add_handler(direct_query_handler) # 最后添加，作为默认行为

    logger.info("机器人已启动，正在等待消息...")
    await application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("机器人已关闭。")

