# fofa_bot_v9.8.py (修复会员等级判断逻辑 & 消息格式)
#
# v9.8 核心更新:
# 1. Bug修复: 重构了API Key等级的判断逻辑，优先检查 is_vip 字段，并修正了 vip_level 的映射，解决了 [820001] 权限错误。
# 2. Bug修复: 移除了对API错误信息不必要的转义，解决了部分消息中出现多余 "/" 的问题。
# 3. 保留了v9.7所有功能。
#
# 运行前请确保已安装依赖:
# pip install pandas openpyxl pysocks "requests[socks]" tqdm "python-telegram-bot[persistence]"
import os
import sys
import json
import logging
import base64
import time
import re
import requests
import signal
import socket
import hashlib
import shutil
import random
import csv
import asyncio
import pandas as pd
from functools import wraps
from datetime import datetime, timedelta
from dateutil import tz
from urllib.parse import urlparse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ParseMode
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackContext,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    Filters,
    PicklePersistence,
)
from telegram.error import BadRequest, RetryAfter

# --- 全局变量和常量 ---
CONFIG_FILE = 'config.json'
HISTORY_FILE = 'history.json'
LOG_FILE = 'fofa_bot.log'
FOFA_CACHE_DIR = 'fofa_file'
PERSISTENCE_FILE = 'bot_persistence'
ANONYMOUS_KEYS_FILE = 'fofa_anonymous.json'
MAX_HISTORY_SIZE = 50
CACHE_EXPIRATION_SECONDS = 24 * 60 * 60
MAX_BATCH_TARGETS = 10000
FOFA_SEARCH_URL = "https://fofa.info/api/v1/search/all"
FOFA_INFO_URL = "https://fofa.info/api/v1/info/my"
FOFA_STATS_URL = "https://fofa.info/api/v1/search/stats"

# --- FOFA 字段定义 ---
# /stats 接口可用的免费字段
FOFA_STATS_FIELDS = "protocol,domain,port,title,os,server,country,asn,org,asset_type,fid,icp"

# /search/all 接口字段按会员等级划分
FREE_FIELDS = ["ip", "port", "protocol", "country", "country_name", "region", "city", "longitude", "latitude", "asn", "org", "host", "domain", "os", "server", "icp", "title", "jarm", "header", "banner", "cert", "base_protocol", "link", "cert.issuer.org", "cert.issuer.cn", "cert.subject.org", "cert.subject.cn", "tls.ja3s", "tls.version", "cert.sn", "cert.not_before", "cert.not_after", "cert.domain"]
PERSONAL_FIELDS = FREE_FIELDS + ["header_hash", "banner_hash", "banner_fid"]
BUSINESS_FIELDS = PERSONAL_FIELDS + ["cname", "lastupdatetime", "product", "product_category", "version", "icon_hash", "cert.is_valid", "cname_domain", "body", "cert.is_match", "cert.is_equal"]
ENTERPRISE_FIELDS = BUSINESS_FIELDS + ["icon", "fid", "structinfo"]

FIELD_CATEGORIES = {
    "免费字段": FREE_FIELDS,
    "个人会员字段": list(set(PERSONAL_FIELDS) - set(FREE_FIELDS)),
    "商业版本字段": list(set(BUSINESS_FIELDS) - set(PERSONAL_FIELDS)),
    "企业版本字段": list(set(ENTERPRISE_FIELDS) - set(BUSINESS_FIELDS)),
}

KEY_LEVELS = {} # 全局变量，用于存储API Key的等级

# --- 日志配置 ---
if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > (5 * 1024 * 1024):
    try: os.rename(LOG_FILE, LOG_FILE + '.old')
    except OSError as e: print(f"无法轮换日志文件: {e}")
logging.basicConfig(
    level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()]
)
logging.getLogger("requests").setLevel(logging.WARNING); logging.getLogger("urllib3").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- 会话状态定义 ---
(
    STATE_SETTINGS_MAIN, STATE_SETTINGS_ACTION, STATE_GET_KEY, STATE_REMOVE_API,
    STATE_KKFOFA_MODE, STATE_CACHE_CHOICE, STATE_GET_IMPORT_QUERY, STATE_GET_STATS_QUERY,
    STATE_PRESET_MENU, STATE_GET_PRESET_NAME, STATE_GET_PRESET_QUERY, STATE_REMOVE_PRESET,
    STATE_GET_UPDATE_URL, STATE_ASK_CONTINENT, STATE_CONTINENT_CHOICE,
    STATE_GET_BATCH_FILE, STATE_SELECT_BATCH_FEATURES, STATE_GET_RESTORE_FILE,
    STATE_PROXYPOOL_MENU, STATE_GET_PROXY_ADD, STATE_GET_PROXY_REMOVE,
    STATE_GET_TRACEBACK_LIMIT, STATE_GET_SCAN_CONCURRENCY, STATE_GET_SCAN_TIMEOUT,
    STATE_UPLOAD_API_MENU, STATE_GET_UPLOAD_URL, STATE_GET_UPLOAD_TOKEN,
    STATE_GET_GUEST_KEY, STATE_BATCH_GET_QUERY, STATE_BATCH_SELECT_FIELDS
) = range(30)

# --- 配置管理 & 缓存 ---
def load_json_file(filename, default_content):
    if not os.path.exists(filename):
        with open(filename, 'w', encoding='utf-8') as f: json.dump(default_content, f, indent=4); return default_content
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            config = json.load(f)
            for key, value in default_content.items(): config.setdefault(key, value)
            return config
    except (json.JSONDecodeError, IOError):
        logger.error(f"{filename} 损坏，将使用默认配置重建。");
        with open(filename, 'w', encoding='utf-8') as f: json.dump(default_content, f, indent=4); return default_content

def save_json_file(filename, data):
    with open(filename, 'w', encoding='utf-8') as f: json.dump(data, f, indent=4, ensure_ascii=False)

DEFAULT_CONFIG = { "bot_token": "YOUR_BOT_TOKEN_HERE", "apis": [], "admins": [], "proxy": "", "proxies": [], "full_mode": False, "public_mode": False, "presets": [], "update_url": "", "upload_api_url": "", "upload_api_token": "" }
CONFIG = load_json_file(CONFIG_FILE, DEFAULT_CONFIG)
HISTORY = load_json_file(HISTORY_FILE, {"queries": []})
ANONYMOUS_KEYS = load_json_file(ANONYMOUS_KEYS_FILE, {})

def save_config(): save_json_file(CONFIG_FILE, CONFIG)
def save_anonymous_keys(): save_json_file(ANONYMOUS_KEYS_FILE, ANONYMOUS_KEYS)

def add_or_update_query(query_text, cache_data=None):
    existing_query = next((q for q in HISTORY['queries'] if q['query_text'] == query_text), None)
    if existing_query:
        HISTORY['queries'].remove(existing_query); existing_query['timestamp'] = datetime.now(tz.tzutc()).isoformat()
        if cache_data: existing_query['cache'] = cache_data
        HISTORY['queries'].insert(0, existing_query)
    else:
        new_query = {"query_text": query_text, "timestamp": datetime.now(tz.tzutc()).isoformat(), "cache": cache_data}
        HISTORY['queries'].insert(0, new_query)
    while len(HISTORY['queries']) > MAX_HISTORY_SIZE: HISTORY['queries'].pop()
    save_json_file(HISTORY_FILE, HISTORY)
def find_cached_query(query_text):
    query = next((q for q in HISTORY['queries'] if q['query_text'] == query_text), None)
    if query and query.get('cache'):
        if 'file_path' in query['cache'] and os.path.exists(query['cache']['file_path']):
            return query
    return None

# --- 辅助函数与装饰器 ---
def generate_filename_from_query(query_text: str, prefix: str = "fofa", ext: str = ".txt") -> str:
    sanitized_query = re.sub(r'[^a-z0-9\-_]+', '_', query_text.lower()).strip('_')
    max_len = 100
    if len(sanitized_query) > max_len: sanitized_query = sanitized_query[:max_len].rsplit('_', 1)[0]
    timestamp = int(time.time()); return f"{prefix}_{sanitized_query}_{timestamp}{ext}"
def get_proxies():
    proxies_list = CONFIG.get("proxies", [])
    if not proxies_list:
        single_proxy = CONFIG.get("proxy")
        if single_proxy: return {"http": single_proxy, "https": single_proxy}
        return None
    chosen_proxy = random.choice(proxies_list)
    return {"http": chosen_proxy, "https": chosen_proxy}
def is_admin(user_id: int) -> bool: return user_id in CONFIG.get('admins', [])
def admin_only(func):
    @wraps(func)
    def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
        if not is_admin(update.effective_user.id):
            message_text = "⛔️ 抱歉，您没有权限执行此管理操作。"
            if update.callback_query: update.callback_query.answer(message_text, show_alert=True)
            elif update.message: update.message.reply_text(message_text)
            return None
        return func(update, context, *args, **kwargs)
    return wrapped
def escape_markdown(text: str) -> str:
    if not isinstance(text, str): text = str(text)
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)
def create_progress_bar(percentage: float, length: int = 10) -> str:
    if percentage < 0: percentage = 0
    if percentage > 100: percentage = 100
    filled_length = int(length * percentage // 100)
    bar = '█' * filled_length + '░' * (length - filled_length)
    return f"[{bar}] {percentage:.1f}%"

# --- 文件上传辅助函数 ---
def upload_and_send_links(context: CallbackContext, chat_id: int, file_path: str):
    api_url = CONFIG.get("upload_api_url")
    api_token = CONFIG.get("upload_api_token")
    if not api_url or not api_token:
        logger.info("未配置上传API的URL或Token，跳过文件上传。")
        return
    try:
        with open(file_path, 'rb') as f:
            files = {'file': (os.path.basename(file_path), f)}
            headers = {'Authorization': api_token}
            response = requests.post(api_url, headers=headers, files=files, timeout=60, proxies=get_proxies())
            response.raise_for_status()
            result = response.json()

        if result and isinstance(result, list) and 'src' in result[0]:
            file_url_path = result[0]['src']
            parsed_main_url = urlparse(api_url)
            base_url = f"{parsed_main_url.scheme}://{parsed_main_url.netloc}"
            full_url = base_url + file_url_path
            file_name = os.path.basename(file_url_path)
            
            download_commands = (
                f"📥 *文件下载命令*\n\n"
                f"*cURL:*\n`curl -o \"{file_name}\" \"{full_url}\"`\n\n"
                f"*Wget:*\n`wget --content-disposition \"{full_url}\"`"
            )
            context.bot.send_message(chat_id, download_commands, parse_mode=ParseMode.MARKDOWN)
        else:
            raise ValueError(f"响应格式不正确: {result}")
    except Exception as e:
        logger.error(f"文件上传失败: {e}")
        # v9.8 FIX: Removed unnecessary markdown escape for error message
        context.bot.send_message(chat_id, f"⚠️ 文件上传到外部服务器失败: `{str(e)}`", parse_mode=ParseMode.MARKDOWN)

# --- FOFA API 核心逻辑 ---
def _make_api_request(url, params, timeout=60, use_b64=True, retries=10):
    if use_b64 and 'q' in params:
        params['qbase64'] = base64.b64encode(params.pop('q').encode('utf-8')).decode('utf-8')
    last_error = None
    for attempt in range(retries):
        try:
            proxies = get_proxies()
            response = requests.get(url, params=params, timeout=timeout, proxies=proxies, verify=False)
            if response.status_code == 429:
                wait_time = 5 * (attempt + 1)
                logger.warning(f"FOFA API rate limit hit (429). Retrying in {wait_time} seconds... (Attempt {attempt + 1}/{retries})")
                time.sleep(wait_time)
                last_error = f"API请求因速率限制(429)失败"
                continue
            response.raise_for_status()
            data = response.json()
            if data.get("error"):
                return None, data.get("errmsg", "未知的FOFA错误")
            return data, None
        except requests.exceptions.RequestException as e:
            last_error = f"网络请求失败: {e}"
            logger.error(f"RequestException on attempt {attempt + 1}: {e}")
            time.sleep(5)
        except json.JSONDecodeError as e:
            last_error = f"解析JSON响应失败: {e}"
            break
    logger.error(f"API request failed after {retries} retries. Last error: {last_error}")
    return None, last_error if last_error else "API请求未知错误"
def verify_fofa_api(key): return _make_api_request(FOFA_INFO_URL, {'key': key}, timeout=15, use_b64=False, retries=3)
def fetch_fofa_data(key, query, page=1, page_size=10000, fields="host"):
    query_lower = query.lower()
    if 'body=' in query_lower: page_size = min(page_size, 500)
    elif 'cert=' in query_lower: page_size = min(page_size, 2000)
    params = {'key': key, 'q': query, 'size': page_size, 'page': page, 'fields': fields, 'full': CONFIG.get("full_mode", False)}; return _make_api_request(FOFA_SEARCH_URL, params)
def fetch_fofa_stats(key, query):
    params = {'key': key, 'q': query, 'fields': FOFA_STATS_FIELDS}; return _make_api_request(FOFA_STATS_URL, params)

def check_and_classify_keys():
    """启动时检查并分类所有管理员API Key"""
    logger.info("--- 开始检查并分类API Keys ---")
    global KEY_LEVELS
    KEY_LEVELS.clear()
    for key in CONFIG.get('apis', []):
        data, error = verify_fofa_api(key)
        if error:
            logger.warning(f"Key '...{key[-4:]}' 无效: {error}")
            KEY_LEVELS[key] = -1 # -1 代表无效
            continue
        
        # v9.8 FIX: Corrected VIP level detection logic
        is_vip = data.get('is_vip', False)
        api_level = data.get('vip_level', 0)
        
        level = 0 # Default to Free
        if is_vip:
            if api_level in [1, 2]: # Personal Member (based on user feedback)
                level = 1
            elif api_level == 3: # Business Member (assumption)
                level = 2
            elif api_level >= 4: # Enterprise Member (assumption)
                level = 3
        
        KEY_LEVELS[key] = level
        level_name = {0: "免费会员", 1: "个人会员", 2: "商业会员", 3: "企业会员"}.get(level, "未知等级")
        logger.info(f"Key '...{key[-4:]}' ({data.get('username', 'N/A')}) - 等级: {level} ({level_name})")
    logger.info("--- API Keys 分类完成 ---")

def get_fields_by_level(level):
    if level >= 3: return ENTERPRISE_FIELDS
    if level == 2: return BUSINESS_FIELDS
    if level == 1: return PERSONAL_FIELDS
    return FREE_FIELDS

def execute_query_with_fallback(query_func, preferred_key_index=None):
    if not CONFIG['apis']: return None, None, None, "没有配置任何API Key。"
    keys_to_try = [k for k in CONFIG['apis'] if KEY_LEVELS.get(k, -1) != -1] # 只尝试有效的key
    if not keys_to_try: return None, None, None, "所有配置的API Key都无效。"
    
    start_index = 0
    if preferred_key_index is not None and 1 <= preferred_key_index <= len(CONFIG['apis']):
        preferred_key = CONFIG['apis'][preferred_key_index - 1]
        if preferred_key in keys_to_try:
            start_index = keys_to_try.index(preferred_key)

    for i in range(len(keys_to_try)):
        idx = (start_index + i) % len(keys_to_try)
        key = keys_to_try[idx]
        key_num = CONFIG['apis'].index(key) + 1
        key_level = KEY_LEVELS.get(key, 0)
        
        data, error = query_func(key)
        if not error:
            return data, key_num, key_level, None
        if "[820031]" in str(error):
            logger.warning(f"Key [#{key_num}] F点余额不足...");
            continue
        return None, key_num, key_level, error
    return None, None, None, "所有Key均尝试失败 (可能F点均不足)。"

# --- 异步扫描逻辑 (无变动) ---
async def async_check_port(host, port, timeout):
    try:
        fut = asyncio.open_connection(host, port)
        _, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close(); await writer.wait_closed()
        return f"{host}:{port}"
    except (asyncio.TimeoutError, ConnectionRefusedError, OSError, socket.gaierror): return None
    except Exception: return None

async def async_scanner_orchestrator(targets, concurrency, timeout, mode='tcping'):
    try:
        from tqdm.asyncio import tqdm as asyncio_tqdm
    except ImportError:
        logger.warning("tqdm 未安装，控制台将不显示进度条。请运行: pip install tqdm")
        async def dummy_gather(*args, **kwargs):
            return await asyncio.gather(*args)
        asyncio_tqdm_gather = dummy_gather
    else:
        asyncio_tqdm_gather = asyncio_tqdm.gather
        
    semaphore = asyncio.Semaphore(concurrency)
    scan_targets = []
    
    if mode == 'tcping':
        for t in targets:
            try:
                host, port_str = t.split(':', 1)
                scan_targets.append((host, int(port_str)))
            except (ValueError, IndexError): continue
    elif mode == 'subnet':
        subnets_to_ports = {}
        for line in targets:
            try:
                ip_str, port_str = line.strip().split(':'); port = int(port_str)
                subnet = ".".join(ip_str.split('.')[:3])
                if subnet not in subnets_to_ports: subnets_to_ports[subnet] = set()
                subnets_to_ports[subnet].add(port)
            except ValueError: continue
        for subnet, ports in subnets_to_ports.items():
            for i in range(1, 255):
                for port in ports:
                    scan_targets.append((f"{subnet}.{i}", port))

    async def worker(host, port):
        async with semaphore:
            return await async_check_port(host, port, timeout)

    tasks = [worker(host, port) for host, port in scan_targets]
    results = await asyncio_tqdm_gather(*tasks, desc=f"Scanning ({mode})", total=len(tasks), unit="host")
    
    return [res for res in results if res is not None]

def run_async_scan_job(context: CallbackContext):
    job_context = context.job.context
    chat_id, msg, query_hash, mode = job_context['chat_id'], job_context['msg'], job_context['query_hash'], job_context['mode']
    concurrency, timeout = job_context['concurrency'], job_context['timeout']

    original_query = context.bot_data.get(query_hash)
    if not original_query: msg.edit_text("❌ 扫描任务已过期或机器人刚刚重启。请重新发起查询以启用扫描。"); return
    
    cached_item = find_cached_query(original_query)
    if not cached_item: msg.edit_text("❌ 找不到结果文件的本地缓存记录。"); return

    msg.edit_text("1/3: 正在读取本地缓存文件...")
    try:
        with open(cached_item['cache']['file_path'], 'r', encoding='utf-8') as f:
            targets = [line.strip() for line in f if ':' in line.strip()]
    except Exception as e: msg.edit_text(f"❌ 读取缓存文件失败: {e}"); return
        
    scan_type_text = "TCP存活扫描" if mode == 'tcping' else "子网扫描"
    msg.edit_text(f"2/3: 已加载 {len(targets)} 个目标，开始异步{scan_type_text} (并发: {concurrency}, 超时: {timeout}s)...")
    
    live_results = asyncio.run(async_scanner_orchestrator(targets, concurrency, timeout, mode))

    if not live_results: msg.edit_text("🤷‍♀️ 扫描完成，但未发现任何存活的目标。"); return
    
    msg.edit_text("3/3: 正在打包并发送新结果...")
    output_filename = generate_filename_from_query(original_query, prefix=f"{mode}_scan")
    with open(output_filename, 'w', encoding='utf-8') as f: f.write("\n".join(sorted(list(live_results))))
    
    final_caption = f"✅ **异步{scan_type_text}完成!**\n\n共发现 *{len(live_results)}* 个存活目标。"
    with open(output_filename, 'rb') as doc:
        context.bot.send_document(chat_id, document=doc, caption=final_caption, parse_mode=ParseMode.MARKDOWN)
    
    upload_and_send_links(context, chat_id, output_filename)
    os.remove(output_filename); msg.delete()

# --- 扫描流程入口 (无变动) ---
def offer_post_download_actions(context: CallbackContext, chat_id, query_text):
    query_hash = hashlib.md5(query_text.encode()).hexdigest()
    context.bot_data[query_hash] = query_text
    keyboard = [[
        InlineKeyboardButton("⚡️ 异步TCP存活扫描", callback_data=f'start_scan_tcping_{query_hash}'),
        InlineKeyboardButton("🌐 异步子网扫描(/24)", callback_data=f'start_scan_subnet_{query_hash}')
    ]]
    context.bot.send_message(chat_id, "下载完成，需要对结果进行二次扫描吗？", reply_markup=InlineKeyboardMarkup(keyboard))

def start_scan_callback(update: Update, context: CallbackContext) -> int:
    query = update.callback_query; query.answer()
    _, mode, query_hash = query.data.split('_', 2)
    
    context.user_data['scan_query_hash'] = query_hash
    context.user_data['scan_mode'] = mode
    
    query.message.edit_text("请输入扫描并发数 (建议 100-1000):")
    return STATE_GET_SCAN_CONCURRENCY

def get_concurrency_callback(update: Update, context: CallbackContext) -> int:
    try:
        concurrency = int(update.message.text)
        if not 1 <= concurrency <= 5000: raise ValueError
        context.user_data['scan_concurrency'] = concurrency
        update.message.reply_text("请输入连接超时时间 (秒, 建议 1-3):")
        return STATE_GET_SCAN_TIMEOUT
    except ValueError:
        update.message.reply_text("无效输入，请输入 1-5000 之间的整数。")
        return STATE_GET_SCAN_CONCURRENCY

def get_timeout_callback(update: Update, context: CallbackContext) -> int:
    try:
        timeout = float(update.message.text)
        if not 0.1 <= timeout <= 10: raise ValueError
        
        msg = update.message.reply_text("✅ 参数设置完毕，任务已提交到后台。")
        job_context = {
            'chat_id': update.effective_chat.id, 'msg': msg,
            'query_hash': context.user_data['scan_query_hash'],
            'mode': context.user_data['scan_mode'],
            'concurrency': context.user_data['scan_concurrency'],
            'timeout': timeout
        }
        context.job_queue.run_once(run_async_scan_job, 1, context=job_context, name=f"scan_{update.effective_chat.id}")

        context.user_data.clear()
        return ConversationHandler.END
    except ValueError:
        update.message.reply_text("无效输入，请输入 0.1-10 之间的数字。")
        return STATE_GET_SCAN_TIMEOUT

# --- 后台下载任务 (无变动) ---
def start_download_job(context: CallbackContext, callback_func, job_data):
    chat_id = job_data['chat_id']; job_name = f"download_job_{chat_id}"
    for job in context.job_queue.get_jobs_by_name(job_name): job.schedule_removal()
    context.bot_data.pop(f'stop_job_{chat_id}', None)
    context.job_queue.run_once(callback_func, 1, context=job_data, name=job_name)
def run_full_download_query(context: CallbackContext):
    job_data = context.job.context; bot, chat_id, query_text, total_size = context.bot, job_data['chat_id'], job_data['query'], job_data['total_size']
    output_filename = generate_filename_from_query(query_text); unique_results, stop_flag = set(), f'stop_job_{chat_id}'
    msg = bot.send_message(chat_id, "⏳ 开始全量下载任务..."); pages_to_fetch = (total_size + 9999) // 10000
    for page in range(1, pages_to_fetch + 1):
        if context.bot_data.get(stop_flag): msg.edit_text("🌀 下载任务已手动停止."); break
        try: msg.edit_text(f"下载进度: {len(unique_results)}/{total_size} (Page {page}/{pages_to_fetch})...")
        except (BadRequest, RetryAfter): pass
        
        # 根据是访客还是管理员选择不同的查询方式
        guest_key = job_data.get('guest_key')
        if guest_key:
            data, error = fetch_fofa_data(guest_key, query_text, page, 10000, "host")
        else:
            data, _, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, query_text, page, 10000, "host"))

        if error: msg.edit_text(f"❌ 第 {page} 页下载出错: {error}"); break
        results = data.get('results', []);
        if not results: break
        unique_results.update(res for res in results if ':' in res)
    if unique_results:
        with open(output_filename, 'w', encoding='utf-8') as f: f.write("\n".join(unique_results))
        msg.edit_text(f"✅ 下载完成！共 {len(unique_results)} 条。正在发送...")
        cache_path = os.path.join(FOFA_CACHE_DIR, output_filename)
        shutil.move(output_filename, cache_path)
        with open(cache_path, 'rb') as doc: bot.send_document(chat_id, document=doc, filename=output_filename)
        upload_and_send_links(context, chat_id, cache_path)
        cache_data = {'file_path': cache_path, 'result_count': len(unique_results)}
        add_or_update_query(query_text, cache_data); offer_post_download_actions(context, chat_id, query_text)
    elif not context.bot_data.get(stop_flag): msg.edit_text("🤷‍♀️ 任务完成，但未能下载到任何数据。")
    context.bot_data.pop(stop_flag, None)
def run_traceback_download_query(context: CallbackContext):
    job_data = context.job.context; bot, chat_id, base_query = context.bot, job_data['chat_id'], job_data['query']; limit = job_data.get('limit')
    output_filename = generate_filename_from_query(base_query); unique_results, page_count, last_page_date, termination_reason, stop_flag, last_update_time = set(), 0, None, "", f'stop_job_{chat_id}', 0
    msg = bot.send_message(chat_id, "⏳ 开始深度追溯下载...")
    current_query = base_query
    guest_key = job_data.get('guest_key')
    while True:
        page_count += 1
        if context.bot_data.get(stop_flag): termination_reason = "\n\n🌀 任务已手动停止."; break
        
        if guest_key:
            data, error = fetch_fofa_data(guest_key, current_query, 1, 10000, "host,lastupdatetime")
        else:
            data, _, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, current_query, 1, 10000, "host,lastupdatetime"))

        if error: termination_reason = f"\n\n❌ 第 {page_count} 轮出错: {error}"; break
        results = data.get('results', [])
        if not results: termination_reason = "\n\nℹ️ 已获取所有查询结果."; break
        original_count = len(unique_results); unique_results.update([r[0] for r in results if r and r[0] and ':' in r[0]]); newly_added_count = len(unique_results) - original_count
        if limit and len(unique_results) >= limit: unique_results = set(list(unique_results)[:limit]); termination_reason = f"\n\nℹ️ 已达到您设置的 {limit} 条结果上限。"; break
        current_time = time.time()
        if current_time - last_update_time > 2:
            try: msg.edit_text(f"⏳ 已找到 {len(unique_results)} 条... (第 {page_count} 轮, 新增 {newly_added_count})")
            except (BadRequest, RetryAfter): pass
            last_update_time = current_time
        valid_anchor_found = False
        for i in range(len(results) - 1, -1, -1):
            if not results[i] or len(results[i]) < 2 or not results[i][1]: continue
            try:
                timestamp_str = results[i][1]; current_date_obj = datetime.strptime(timestamp_str.split(' ')[0], '%Y-%m-%d').date()
                if last_page_date and current_date_obj >= last_page_date: continue
                next_page_date_obj = current_date_obj
                if last_page_date and current_date_obj == last_page_date: next_page_date_obj -= timedelta(days=1)
                last_page_date = current_date_obj; current_query = f'({base_query}) && before="{next_page_date_obj.strftime("%Y-%m-%d")}"'; valid_anchor_found = True
                break
            except (ValueError, TypeError): continue
        if not valid_anchor_found: termination_reason = "\n\n⚠️ 无法找到有效的时间锚点以继续，可能已达查询边界."; break
    if unique_results:
        with open(output_filename, 'w', encoding='utf-8') as f: f.write("\n".join(sorted(list(unique_results))))
        msg.edit_text(f"✅ 深度追溯完成！共 {len(unique_results)} 条。{termination_reason}\n正在发送文件...")
        cache_path = os.path.join(FOFA_CACHE_DIR, output_filename)
        shutil.move(output_filename, cache_path)
        with open(cache_path, 'rb') as doc: bot.send_document(chat_id, document=doc, filename=output_filename)
        upload_and_send_links(context, chat_id, cache_path)
        cache_data = {'file_path': cache_path, 'result_count': len(unique_results)}
        add_or_update_query(base_query, cache_data); offer_post_download_actions(context, chat_id, base_query)
    else: msg.edit_text(f"🤷‍♀️ 任务完成，但未能下载到任何数据。{termination_reason}")
    context.bot_data.pop(stop_flag, None)
def run_incremental_update_query(context: CallbackContext):
    job_data = context.job.context; bot, chat_id, base_query = context.bot, job_data['chat_id'], job_data['query']; msg = bot.send_message(chat_id, "--- 增量更新启动 ---")
    msg.edit_text("1/5: 正在获取旧缓存..."); cached_item = find_cached_query(base_query)
    if not cached_item: msg.edit_text("❌ 错误：找不到本地缓存项。"); return
    old_file_path = cached_item['cache']['file_path']; old_results = set()
    try:
        with open(old_file_path, 'r', encoding='utf-8') as f: old_results = set(line.strip() for line in f if line.strip() and ':' in line)
    except Exception as e: msg.edit_text(f"❌ 读取本地缓存文件失败: {e}"); return
    msg.edit_text("2/5: 正在确定更新起始点..."); data, _, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, base_query, fields="lastupdatetime"))
    if error or not data.get('results'): msg.edit_text(f"❌ 无法获取最新记录时间戳: {error or '无结果'}"); return
    ts_str = data['results'][0][0] if isinstance(data['results'][0], list) else data['results'][0]; cutoff_date = ts_str.split(' ')[0]
    incremental_query = f'({base_query}) && after="{cutoff_date}"'
    msg.edit_text(f"3/5: 正在侦察自 {cutoff_date} 以来的新数据..."); data, _, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, incremental_query, page_size=1))
    if error: msg.edit_text(f"❌ 侦察查询失败: {error}"); return
    total_new_size = data.get('size', 0)
    if total_new_size == 0: msg.edit_text("✅ 未发现新数据。缓存已是最新。"); return
    new_results, stop_flag = set(), f'stop_job_{chat_id}'; pages_to_fetch = (total_new_size + 9999) // 10000
    for page in range(1, pages_to_fetch + 1):
        if context.bot_data.get(stop_flag): msg.edit_text("🌀 增量更新已手动停止。"); return
        msg.edit_text(f"3/5: 正在下载新数据... ( Page {page}/{pages_to_fetch} )")
        data, _, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, incremental_query, page=page, page_size=10000))
        if error: msg.edit_text(f"❌ 下载新数据失败: {error}"); return
        if data.get('results'): new_results.update(res for res in data.get('results', []) if ':' in res)
    msg.edit_text(f"4/5: 正在合并数据... (发现 {len(new_results)} 条新数据)"); combined_results = sorted(list(new_results.union(old_results)))
    with open(old_file_path, 'w', encoding='utf-8') as f: f.write("\n".join(combined_results))
    msg.edit_text(f"5/5: 发送更新后的文件... (共 {len(combined_results)} 条)")
    with open(old_file_path, 'rb') as doc: bot.send_document(chat_id, document=doc, filename=os.path.basename(old_file_path))
    upload_and_send_links(context, chat_id, old_file_path)
    cache_data = {'file_path': old_file_path, 'result_count': len(combined_results)}
    add_or_update_query(base_query, cache_data)
    msg.delete(); bot.send_message(chat_id, f"✅ 增量更新完成！"); offer_post_download_actions(context, chat_id, base_query)
def run_batch_download_query(context: CallbackContext):
    job_data = context.job.context; bot, chat_id, query_text, total_size, fields = context.bot, job_data['chat_id'], job_data['query'], job_data['total_size'], job_data['fields']
    output_filename = generate_filename_from_query(query_text, prefix="batch_export", ext=".csv"); results_list, stop_flag = [], f'stop_job_{chat_id}'
    msg = bot.send_message(chat_id, "⏳ 开始自定义字段批量导出任务..."); pages_to_fetch = (total_size + 9999) // 10000
    for page in range(1, pages_to_fetch + 1):
        if context.bot_data.get(stop_flag): msg.edit_text("🌀 下载任务已手动停止."); break
        try: msg.edit_text(f"下载进度: {len(results_list)}/{total_size} (Page {page}/{pages_to_fetch})...")
        except (BadRequest, RetryAfter): pass
        data, _, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, query_text, page, 10000, fields))
        if error: msg.edit_text(f"❌ 第 {page} 页下载出错: {error}"); break
        page_results = data.get('results', [])
        if not page_results: break
        results_list.extend(page_results)
    if results_list:
        msg.edit_text(f"✅ 下载完成！共 {len(results_list)} 条。正在生成CSV文件...")
        try:
            with open(output_filename, 'w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f); writer.writerow(fields.split(',')); writer.writerows(results_list)
            with open(output_filename, 'rb') as doc:
                bot.send_document(chat_id, document=doc, filename=output_filename, caption=f"✅ 自定义导出完成\n查询: `{escape_markdown(query_text)}`", parse_mode=ParseMode.MARKDOWN)
            upload_and_send_links(context, chat_id, output_filename)
        except Exception as e:
            msg.edit_text(f"❌ 生成或发送CSV文件失败: {e}"); logger.error(f"Failed to generate/send CSV for batch command: {e}")
        finally:
            if os.path.exists(output_filename): os.remove(output_filename)
            msg.delete()
    elif not context.bot_data.get(stop_flag): msg.edit_text("🤷‍♀️ 任务完成，但未能下载到任何数据。")
    context.bot_data.pop(stop_flag, None)
def run_batch_traceback_query(context: CallbackContext):
    job_data = context.job.context; bot, chat_id, base_query, fields, limit = context.bot, job_data['chat_id'], job_data['query'], job_data['fields'], job_data.get('limit')
    output_filename = generate_filename_from_query(base_query, prefix="batch_traceback", ext=".csv")
    unique_results, page_count, last_page_date, termination_reason, stop_flag, last_update_time = [], 0, None, "", f'stop_job_{chat_id}', 0
    msg = bot.send_message(chat_id, "⏳ 开始自定义字段深度追溯下载...")
    current_query = base_query; seen_hashes = set()
    while True:
        page_count += 1
        if context.bot_data.get(stop_flag): termination_reason = "\n\n🌀 任务已手动停止."; break
        data, _, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, current_query, 1, 10000, fields + ",lastupdatetime"))
        if error: termination_reason = f"\n\n❌ 第 {page_count} 轮出错: {error}"; break
        results = data.get('results', [])
        if not results: termination_reason = "\n\nℹ️ 已获取所有查询结果."; break
        newly_added_count = 0
        for r in results:
            r_hash = hashlib.md5(str(r).encode()).hexdigest()
            if r_hash not in seen_hashes:
                seen_hashes.add(r_hash); unique_results.append(r[:-1]); newly_added_count += 1 # Exclude lastupdatetime
        if limit and len(unique_results) >= limit: unique_results = unique_results[:limit]; termination_reason = f"\n\nℹ️ 已达到您设置的 {limit} 条结果上限。"; break
        current_time = time.time()
        if current_time - last_update_time > 2:
            try: msg.edit_text(f"⏳ 已找到 {len(unique_results)} 条... (第 {page_count} 轮, 新增 {newly_added_count})")
            except (BadRequest, RetryAfter): pass
            last_update_time = current_time
        valid_anchor_found = False
        for i in range(len(results) - 1, -1, -1):
            if not results[i] or len(results[i]) < 2 or not results[i][-1]: continue
            try:
                timestamp_str = results[i][-1]; current_date_obj = datetime.strptime(timestamp_str.split(' ')[0], '%Y-%m-%d').date()
                if last_page_date and current_date_obj >= last_page_date: continue
                next_page_date_obj = current_date_obj
                if last_page_date and current_date_obj == last_page_date: next_page_date_obj -= timedelta(days=1)
                last_page_date = current_date_obj; current_query = f'({base_query}) && before="{next_page_date_obj.strftime("%Y-%m-%d")}"'; valid_anchor_found = True
                break
            except (ValueError, TypeError): continue
        if not valid_anchor_found: termination_reason = "\n\n⚠️ 无法找到有效的时间锚点以继续，可能已达查询边界."; break
    if unique_results:
        msg.edit_text(f"✅ 追溯完成！共 {len(unique_results)} 条。{termination_reason}\n正在生成CSV...")
        try:
            with open(output_filename, 'w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f); writer.writerow(fields.split(',')); writer.writerows(unique_results)
            with open(output_filename, 'rb') as doc:
                bot.send_document(chat_id, document=doc, filename=output_filename)
            upload_and_send_links(context, chat_id, output_filename)
        except Exception as e:
            msg.edit_text(f"❌ 生成或发送CSV文件失败: {e}"); logger.error(f"Failed to generate/send CSV for batch traceback: {e}")
        finally:
            if os.path.exists(output_filename): os.remove(output_filename)
            msg.delete()
    else: msg.edit_text(f"🤷‍♀️ 任务完成，但未能下载到任何数据。{termination_reason}")
    context.bot_data.pop(stop_flag, None)

# --- 核心命令处理 ---
def start_command(update: Update, context: CallbackContext):
    update.message.reply_text('👋 欢迎使用 Fofa 查询机器人 v9.8！请使用 /help 查看命令手册。')
    if not CONFIG['admins']: first_admin_id = update.effective_user.id; CONFIG.setdefault('admins', []).append(first_admin_id); save_config(); update.message.reply_text(f"ℹ️ 已自动将您 (ID: `{first_admin_id}`) 添加为第一个管理员。")

def help_command(update: Update, context: CallbackContext):
    help_text = ( "📖 *Fofa 机器人指令手册 v9.8*\n\n"
                  "*🔍 资产查询*\n`/kkfofa [key] <query>`\n_FOFA搜索, 非管理员首次使用需提供Key_\n\n"
                  "*📦 主机详查 (智能)*\n`/host <ip|domain>`\n_根据Key等级获取最全主机信息 (管理员)_\n\n"
                  "*🔬 主机速查 (开放)*\n`/lowhost <ip|domain>`\n_获取主机基础信息 (所有用户可用)_\n\n"
                  "*📊 聚合统计*\n`/stats <query>`\n_获取全局聚合统计 (管理员)_\n\n"
                  "*📂 批量智能分析*\n`/batchfind`\n_上传IP列表, 分析特征并生成Excel (管理员)_\n\n"
                  "*📤 批量自定义导出 (交互式)*\n`/batch <query>`\n_进入交互式菜单选择字段导出 (管理员)_\n\n"
                  "*⚙️ 管理与设置*\n`/settings`\n_进入交互式设置菜单 (管理员)_\n\n"
                  "*💻 系统管理*\n"
                  "`/check` - 系统自检\n"
                  "`/update` - 在线更新脚本\n"
                  "`/shutdown` - 安全关闭/重启\n\n"
                  "*🛑 任务控制*\n`/stop` - 紧急停止下载任务\n`/cancel` - 取消当前操作\n\n"
                  "💡 *机器人现在能自动识别Key等级并为访客提供服务！*" )
    update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

def cancel(update: Update, context: CallbackContext) -> int:
    message = "操作已取消。"
    if update.message: update.message.reply_text(message)
    elif update.callback_query: update.callback_query.edit_message_text(message)
    context.user_data.clear()
    return ConversationHandler.END

# --- /kkfofa & 访客逻辑 ---
def kkfofa_entry(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    
    # 访客逻辑
    if not is_admin(user_id):
        guest_key = ANONYMOUS_KEYS.get(str(user_id))
        if not guest_key:
            update.message.reply_text("👋 欢迎！作为首次使用的访客，请输入您的FOFA API Key以继续。您的Key只会被您自己使用。")
            # 将查询参数暂存，以便获取Key后继续
            if context.args:
                context.user_data['pending_query'] = " ".join(context.args)
            return STATE_GET_GUEST_KEY
        # 如果访客已有key，将其存入上下文以便后续使用
        context.user_data['guest_key'] = guest_key

    # 管理员和已验证的访客逻辑
    query_obj = update.callback_query; message_obj = update.message
    if query_obj:
        query_obj.answer()
        try:
            preset_index = int(query_obj.data.replace("run_preset_", "")); preset = CONFIG["presets"][preset_index]
            context.user_data['original_query'] = preset['query']; context.user_data['key_index'] = None
            keyboard = [[InlineKeyboardButton("🌍 是的, 限定大洲", callback_data="continent_select"), InlineKeyboardButton("⏩ 不, 直接搜索", callback_data="continent_skip")]]
            query_obj.message.edit_text(f"预设查询: `{escape_markdown(preset['query'])}`\n\n是否要将此查询限定在特定大洲范围内？", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
            return STATE_ASK_CONTINENT
        except (ValueError, IndexError):
            query_obj.message.edit_text("❌ 预设查询失败。"); return ConversationHandler.END
    if not context.args:
        presets = CONFIG.get("presets", [])
        if not presets: message_obj.reply_text("欢迎使用FOFA查询机器人。\n\n➡️ 直接输入查询语法: `/kkfofa domain=\"example.com\"`\nℹ️ 当前没有可用的预设查询。管理员可通过 /settings 添加。"); return ConversationHandler.END
        keyboard = []
        for i, p in enumerate(presets):
            query_preview = p['query'][:25] + '...' if len(p['query']) > 25 else p['query']
            keyboard.append([InlineKeyboardButton(f"{p['name']} (`{query_preview}`)", callback_data=f"run_preset_{i}")])
        message_obj.reply_text("👇 请选择一个预设查询:", reply_markup=InlineKeyboardMarkup(keyboard)); return ConversationHandler.END
    
    key_index, query_text = None, " ".join(context.args)
    if context.args[0].isdigit() and is_admin(user_id): # 只有管理员可以指定key
        try:
            num = int(context.args[0])
            if 1 <= num <= len(CONFIG['apis']): key_index = num; query_text = " ".join(context.args[1:])
        except ValueError: pass
        
    context.user_data['original_query'] = query_text; context.user_data['key_index'] = key_index
    keyboard = [[InlineKeyboardButton("🌍 是的, 限定大洲", callback_data="continent_select"), InlineKeyboardButton("⏩ 不, 直接搜索", callback_data="continent_skip")]]
    message_obj.reply_text(f"查询: `{escape_markdown(query_text)}`\n\n是否要将此查询限定在特定大洲范围内？", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return STATE_ASK_CONTINENT

def get_guest_key(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    guest_key = update.message.text.strip()
    
    msg = update.message.reply_text("⏳ 正在验证您的API Key...")
    data, error = verify_fofa_api(guest_key)
    
    if error:
        msg.edit_text(f"❌ Key验证失败: {error}\n请重新输入一个有效的Key，或使用 /cancel 取消。")
        return STATE_GET_GUEST_KEY
    
    # 验证成功，保存Key
    ANONYMOUS_KEYS[str(user_id)] = guest_key
    save_anonymous_keys()
    msg.edit_text(f"✅ Key验证成功 ({data.get('username', 'N/A')})！您的Key已保存，现在可以开始查询了。")
    
    # 如果有待处理的查询，则继续
    if 'pending_query' in context.user_data:
        context.args = context.user_data.pop('pending_query').split()
        return kkfofa_entry(update, context)
    
    return ConversationHandler.END

def ask_continent_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); choice = query.data.split('_')[1]
    if choice == 'skip':
        context.user_data['query'] = context.user_data['original_query']
        query.message.edit_text(f"好的，将直接搜索: `{escape_markdown(context.user_data['query'])}`", parse_mode=ParseMode.MARKDOWN)
        return proceed_with_query(update, context, message_to_edit=query.message)
    elif choice == 'select':
        keyboard = [
            [InlineKeyboardButton("🌏 亚洲", callback_data="continent_Asia"), InlineKeyboardButton("🌍 欧洲", callback_data="continent_Europe")],
            [InlineKeyboardButton("🌎 北美洲", callback_data="continent_NorthAmerica"), InlineKeyboardButton("🌎 南美洲", callback_data="continent_SouthAmerica")],
            [InlineKeyboardButton("🌍 非洲", callback_data="continent_Africa"), InlineKeyboardButton("🌏 大洋洲", callback_data="continent_Oceania")],
            [InlineKeyboardButton("↩️ 跳过", callback_data="continent_skip")]]
        query.message.edit_text("请选择一个大洲:", reply_markup=InlineKeyboardMarkup(keyboard)); return STATE_CONTINENT_CHOICE
def continent_choice_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); continent = query.data.split('_', 1)[1]; original_query = context.user_data['original_query']
    if continent == 'skip':
        context.user_data['query'] = original_query
        query.message.edit_text(f"好的，将直接搜索: `{escape_markdown(original_query)}`", parse_mode=ParseMode.MARKDOWN)
        return proceed_with_query(update, context, message_to_edit=query.message)
    country_list = CONTINENT_COUNTRIES.get(continent)
    if not country_list: query.message.edit_text("❌ 错误：无效的大洲选项。"); return ConversationHandler.END
    country_fofa_string = " || ".join([f'country="{code}"' for code in country_list]); final_query = f"({original_query}) && ({country_fofa_string})"
    context.user_data['query'] = final_query
    query.message.edit_text(f"查询已构建:\n`{escape_markdown(final_query)}`\n\n正在处理...", parse_mode=ParseMode.MARKDOWN)
    return proceed_with_query(update, context, message_to_edit=query.message)
def proceed_with_query(update: Update, context: CallbackContext, message_to_edit):
    query_text = context.user_data['query']
    cached_item = find_cached_query(query_text)
    if cached_item:
        dt_utc = datetime.fromisoformat(cached_item['timestamp']); dt_local = dt_utc.astimezone(tz.tzlocal()); time_str = dt_local.strftime('%Y-%m-%d %H:%M')
        message_text = (f"✅ *发现缓存*\n\n查询: `{escape_markdown(query_text)}`\n缓存于: *{time_str}*\n\n")
        keyboard = []; is_expired = (datetime.now(tz.tzutc()) - dt_utc).total_seconds() > CACHE_EXPIRATION_SECONDS
        if is_expired or not is_admin(update.effective_user.id): # 访客不能增量更新
             message_text += "⚠️ *此缓存已过期或您是访客，无法增量更新。*" if is_expired else ""
             keyboard.append([InlineKeyboardButton("⬇️ 下载旧缓存", callback_data='cache_download'), InlineKeyboardButton("🔍 全新搜索", callback_data='cache_newsearch')])
        else: 
            message_text += "请选择操作："; keyboard.append([InlineKeyboardButton("🔄 增量更新", callback_data='cache_incremental')]); keyboard.append([InlineKeyboardButton("⬇️ 下载缓存", callback_data='cache_download'), InlineKeyboardButton("🔍 全新搜索", callback_data='cache_newsearch')])
        keyboard.append([InlineKeyboardButton("❌ 取消", callback_data='cache_cancel')])
        message_to_edit.edit_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        return STATE_CACHE_CHOICE
    return start_new_search(update, context, message_to_edit=message_to_edit)
def cache_choice_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); choice = query.data.split('_')[1]
    if choice == 'download':
        cached_item = find_cached_query(context.user_data['query'])
        if cached_item:
            query.message.edit_text("⬇️ 正在从本地缓存发送文件..."); file_path = cached_item['cache']['file_path']
            try:
                with open(file_path, 'rb') as doc: context.bot.send_document(chat_id=update.effective_chat.id, document=doc, filename=os.path.basename(file_path))
                upload_and_send_links(context, update.effective_chat.id, file_path)
                query.message.delete()
            except Exception as e: query.message.edit_text(f"❌ 发送缓存失败: {e}")
        else: query.message.edit_text("❌ 找不到本地缓存记录。")
        return ConversationHandler.END
    elif choice == 'newsearch': return start_new_search(update, context, message_to_edit=query.message)
    elif choice == 'incremental': query.edit_message_text("⏳ 准备增量更新..."); start_download_job(context, run_incremental_update_query, context.user_data); query.message.delete(); return ConversationHandler.END
    elif choice == 'cancel': query.message.edit_text("操作已取消。"); return ConversationHandler.END
def start_new_search(update: Update, context: CallbackContext, message_to_edit=None):
    query_text = context.user_data['query']; key_index = context.user_data.get('key_index'); add_or_update_query(query_text)
    msg = message_to_edit if message_to_edit else update.effective_message.reply_text("🔄 正在执行全新查询...")
    if message_to_edit: msg.edit_text("🔄 正在执行全新查询...")
    
    guest_key = context.user_data.get('guest_key')
    if guest_key:
        data, error = fetch_fofa_data(guest_key, query_text, page_size=1, fields="host")
        used_key_info = "您的Key"
    else:
        data, used_key_index, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, query_text, page_size=1, fields="host"), key_index)
        used_key_info = f"Key [#{used_key_index}]"

    if error: msg.edit_text(f"❌ 查询出错: {error}"); return ConversationHandler.END
    total_size = data.get('size', 0)
    if total_size == 0: msg.edit_text("🤷‍♀️ 未找到结果。"); return ConversationHandler.END
    context.user_data.update({'total_size': total_size, 'chat_id': update.effective_chat.id, 'is_batch_mode': False})
    success_message = f"✅ 使用 {used_key_info} 找到 {total_size} 条结果。"
    if total_size <= 10000:
        msg.edit_text(f"{success_message}\n开始下载..."); start_download_job(context, run_full_download_query, context.user_data)
        return ConversationHandler.END
    else:
        keyboard = [[InlineKeyboardButton("💎 全部下载 (前1万)", callback_data='mode_full'), InlineKeyboardButton("🌀 深度追溯下载", callback_data='mode_traceback')], [InlineKeyboardButton("❌ 取消", callback_data='mode_cancel')]]
        msg.edit_text(f"{success_message}\n请选择下载模式:", reply_markup=InlineKeyboardMarkup(keyboard)); return STATE_KKFOFA_MODE
def query_mode_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); mode = query.data.split('_')[1]
    if mode == 'cancel': query.message.edit_text("操作已取消."); return ConversationHandler.END
    if mode == 'traceback':
        keyboard = [[InlineKeyboardButton("♾️ 全部获取", callback_data='limit_none')], [InlineKeyboardButton("❌ 取消", callback_data='limit_cancel')]]
        query.message.edit_text("请输入深度追溯获取的结果数量上限 (例如: 50000)，或选择全部获取。", reply_markup=InlineKeyboardMarkup(keyboard))
        return STATE_GET_TRACEBACK_LIMIT
    job_func = run_batch_download_query if context.user_data.get('is_batch_mode') else run_full_download_query
    if mode == 'full' and job_func:
        query.message.edit_text(f"⏳ 开始下载..."); start_download_job(context, job_func, context.user_data); query.message.delete()
    return ConversationHandler.END
def get_traceback_limit(update: Update, context: CallbackContext):
    limit = None
    if update.callback_query:
        query = update.callback_query; query.answer()
        if query.data == 'limit_cancel': query.message.edit_text("操作已取消."); return ConversationHandler.END
    elif update.message:
        try:
            limit = int(update.message.text.strip()); assert limit > 0
        except (ValueError, AssertionError):
            update.message.reply_text("❌ 无效的数字，请输入一个正整数。"); return STATE_GET_TRACEBACK_LIMIT
    context.user_data['limit'] = limit
    job_func = run_batch_traceback_query if context.user_data.get('is_batch_mode') else run_traceback_download_query
    msg_target = update.callback_query.message if update.callback_query else update.message
    msg_target.reply_text(f"⏳ 开始深度追溯 (上限: {limit or '无'})...")
    start_download_job(context, job_func, context.user_data)
    if update.callback_query: msg_target.delete()
    return ConversationHandler.END

# --- /host 和 /lowhost 命令 ---
def _create_dict_from_fofa_result(result_list, fields_list):
    return {fields_list[i]: result_list[i] for i in range(len(fields_list))}

def get_common_host_info(results, fields_list):
    if not results: return {}
    first_entry = _create_dict_from_fofa_result(results[0], fields_list)
    info = {
        "IP": first_entry.get('ip', 'N/A'),
        "地理位置": f"{first_entry.get('country_name', '')} {first_entry.get('region', '')} {first_entry.get('city', '')}".strip(),
        "ASN": f"{first_entry.get('asn', 'N/A')} ({first_entry.get('org', 'N/A')})",
        "操作系统": first_entry.get('os', 'N/A'),
    }
    port_index = fields_list.index('port') if 'port' in fields_list else -1
    if port_index != -1:
        all_ports = sorted(list(set(res[port_index] for res in results if len(res) > port_index)))
        info["开放端口"] = all_ports
    return info

def create_host_summary(host_arg, results, fields_list):
    info = get_common_host_info(results, fields_list)
    summary = [f"📌 *主机概览: `{host_arg}`*"]
    for key, value in info.items():
        if value and value != 'N/A':
            if isinstance(value, list):
                summary.append(f"*{key}:* `{', '.join(map(str, value))}`")
            else:
                summary.append(f"*{key}:* `{value}`")
    summary.append("\n📄 *详细报告已作为文件发送。*")
    return "\n".join(summary)

def format_full_host_report(host_arg, results, fields_list):
    info = get_common_host_info(results, fields_list)
    report = [f"📌 *主机聚合报告: `{host_arg}`*\n"]
    for key, value in info.items():
        if value and value != 'N/A':
            if isinstance(value, list):
                report.append(f"*{key}:* `{', '.join(map(str, value))}`")
            else:
                report.append(f"*{key}:* `{value}`")

    report.append("\n--- *服务详情* ---\n")
    for res_list in results:
        d = _create_dict_from_fofa_result(res_list, fields_list)
        port_info = [f"🌐 *Port `{d.get('port')}` ({d.get('protocol', 'N/A')})*"]
        if d.get('title'): port_info.append(f"  - *标题:* `{d.get('title')}`")
        if d.get('server'): port_info.append(f"  - *服务:* `{d.get('server')}`")
        if d.get('icp'): port_info.append(f"  - *ICP:* `{d.get('icp')}`")
        if d.get('jarm'): port_info.append(f"  - *JARM:* `{d.get('jarm')}`")
        
        cert_str = d.get('cert', '{}')
        try:
            cert_info = json.loads(cert_str) if isinstance(cert_str, str) and cert_str.startswith('{') else {}
            if cert_info.get('issuer', {}).get('CN'): port_info.append(f"  - *证书颁发者:* `{cert_info['issuer']['CN']}`")
            if cert_info.get('subject', {}).get('CN'): port_info.append(f"  - *证书使用者:* `{cert_info['subject']['CN']}`")
        except json.JSONDecodeError:
            pass

        if d.get('header'): port_info.append(f"  - *Header:* ```\n{d.get('header')}\n```")
        if d.get('banner'): port_info.append(f"  - *Banner:* ```\n{d.get('banner')}\n```")
        report.append("\n".join(port_info))
    return "\n".join(report)

def host_command_logic(update: Update, context: CallbackContext, use_max_fields: bool):
    if not context.args:
        command_name = "/host" if use_max_fields else "/lowhost"
        update.message.reply_text(f"用法: `{command_name} <ip_or_domain>`\n\n示例:\n`{command_name} 1.1.1.1`", parse_mode=ParseMode.MARKDOWN)
        return
    
    host_arg = context.args[0]
    processing_message = update.message.reply_text(f"⏳ 正在查询主机 `{escape_markdown(host_arg)}`...")
    
    query = f'ip="{host_arg}"' if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host_arg) else f'domain="{host_arg}"'
    
    if use_max_fields:
        # 智能选择字段
        data, _, key_level, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, query, page_size=1, fields="host"))
        if error:
            # v9.8 FIX: Removed unnecessary markdown escape for error message
            processing_message.edit_text(f"查询失败 😞\n*原因:* `{error}`", parse_mode=ParseMode.MARKDOWN)
            return
        fields_list = get_fields_by_level(key_level)
        fields_str = ",".join(fields_list)
        data, _, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, query, page_size=100, fields=fields_str))
    else:
        # /lowhost, 使用免费字段
        fields_list = FREE_FIELDS
        fields_str = ",".join(fields_list)
        data, _, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, query, page_size=100, fields=fields_str))

    if error:
        # v9.8 FIX: Removed unnecessary markdown escape for error message
        processing_message.edit_text(f"查询失败 😞\n*原因:* `{error}`", parse_mode=ParseMode.MARKDOWN)
        return
    
    results = data.get('results', [])
    if not results:
        processing_message.edit_text(f"🤷‍♀️ 未找到关于 `{escape_markdown(host_arg)}` 的任何信息。")
        return

    full_report = format_full_host_report(host_arg, results, fields_list)
    
    if len(full_report) > 3800:
        summary_report = create_host_summary(host_arg, results, fields_list)
        processing_message.edit_text(summary_report, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        report_filename = f"host_details_{host_arg.replace('.', '_')}.txt"
        try:
            plain_text_report = re.sub(r'([*_`\[\]])', '', full_report)
            with open(report_filename, 'w', encoding='utf-8') as f: f.write(plain_text_report)
            with open(report_filename, 'rb') as doc: context.bot.send_document(chat_id=update.effective_chat.id, document=doc, caption="📄 完整的详细报告已附上。")
            upload_and_send_links(context, update.effective_chat.id, report_filename)
        finally:
            if os.path.exists(report_filename): os.remove(report_filename)
    else:
        processing_message.edit_text(full_report, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

@admin_only
def host_command(update: Update, context: CallbackContext):
    host_command_logic(update, context, use_max_fields=True)

def lowhost_command(update: Update, context: CallbackContext):
    host_command_logic(update, context, use_max_fields=False)

# --- /stats 命令 (无变动) ---
@admin_only
def stats_command(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("请输入要进行聚合统计的FOFA查询语法:")
        return STATE_GET_STATS_QUERY
    return get_fofa_stats_query(update, context)
def get_fofa_stats_query(update: Update, context: CallbackContext):
    query_text = " ".join(context.args) if context.args else update.message.text
    msg = update.message.reply_text(f"⏳ 正在对 `{escape_markdown(query_text)}` 进行聚合统计...")
    data, _, _, error = execute_query_with_fallback(lambda key: fetch_fofa_stats(key, query_text))
    if error: msg.edit_text(f"❌ 统计失败: {error}"); return ConversationHandler.END
    report = [f"📊 *聚合统计报告 for `{escape_markdown(query_text)}`*\n"]
    for field, aggs in data.items():
        if aggs:
            report.append(f"--- *{field.capitalize()}* ---")
            for item in aggs[:10]: # Top 10
                report.append(f"`{item['name']}`: {item['count']}")
            report.append("")
    msg.edit_text("\n".join(report), parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

# --- /batchfind 命令 (无变动) ---
BATCH_FEATURES = { "protocol": "协议", "domain": "域名", "os": "操作系统", "server": "服务/组件", "icp": "ICP备案号", "title": "标题", "jarm": "JARM指纹", "cert.issuer.org": "证书颁发组织", "cert.issuer.cn": "证书颁发CN", "cert.subject.org": "证书主体组织", "cert.subject.cn": "证书主体CN" }
@admin_only
def batchfind_command(update: Update, context: CallbackContext):
    update.message.reply_text("请上传一个包含 IP:Port 列表的 .txt 文件。")
    return STATE_GET_BATCH_FILE
def get_batch_file_handler(update: Update, context: CallbackContext):
    doc = update.message.document
    file = doc.get_file()
    file_path = os.path.join(FOFA_CACHE_DIR, doc.file_name)
    file.download(custom_path=file_path)
    context.user_data['batch_file_path'] = file_path
    context.user_data['selected_features'] = set()
    keyboard = []
    features_list = list(BATCH_FEATURES.items())
    for i in range(0, len(features_list), 2):
        row = [InlineKeyboardButton(f"☐ {features_list[i][1]}", callback_data=f"batchfeature_{features_list[i][0]}")]
        if i + 1 < len(features_list):
            row.append(InlineKeyboardButton(f"☐ {features_list[i+1][1]}", callback_data=f"batchfeature_{features_list[i+1][0]}"))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("✅ 全部选择", callback_data="batchfeature_all"), InlineKeyboardButton("➡️ 开始分析", callback_data="batchfeature_done")])
    update.message.reply_text("请选择您需要分析的特征:", reply_markup=InlineKeyboardMarkup(keyboard))
    return STATE_SELECT_BATCH_FEATURES
def select_batch_features_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); feature = query.data.split('_', 1)[1]
    selected = context.user_data['selected_features']
    if feature == 'done':
        if not selected: query.answer("请至少选择一个特征！", show_alert=True); return STATE_SELECT_BATCH_FEATURES
        query.message.edit_text("✅ 特征选择完毕，任务已提交到后台分析。")
        job_context = {'chat_id': query.message.chat_id, 'file_path': context.user_data['batch_file_path'], 'features': list(selected)}
        context.job_queue.run_once(run_batch_find_job, 1, context=job_context, name=f"batchfind_{query.message.chat_id}")
        return ConversationHandler.END
    if feature == 'all':
        if len(selected) == len(BATCH_FEATURES): selected.clear()
        else: selected.update(BATCH_FEATURES.keys())
    elif feature in selected: selected.remove(feature)
    else: selected.add(feature)
    keyboard = []
    features_list = list(BATCH_FEATURES.items())
    for i in range(0, len(features_list), 2):
        row = []
        key1 = features_list[i][0]; row.append(InlineKeyboardButton(f"{'☑' if key1 in selected else '☐'} {features_list[i][1]}", callback_data=f"batchfeature_{key1}"))
        if i + 1 < len(features_list):
            key2 = features_list[i+1][0]; row.append(InlineKeyboardButton(f"{'☑' if key2 in selected else '☐'} {features_list[i+1][1]}", callback_data=f"batchfeature_{key2}"))
        keyboard.append(row)
    all_text = "✅ 取消全选" if len(selected) == len(BATCH_FEATURES) else "✅ 全部选择"
    keyboard.append([InlineKeyboardButton(all_text, callback_data="batchfeature_all"), InlineKeyboardButton("➡️ 开始分析", callback_data="batchfeature_done")])
    query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
    return STATE_SELECT_BATCH_FEATURES
def run_batch_find_job(context: CallbackContext):
    job_data = context.job.context; chat_id, file_path, features = job_data['chat_id'], job_data['file_path'], job_data['features']
    bot = context.bot; msg = bot.send_message(chat_id, "⏳ 开始批量分析任务...")
    try:
        with open(file_path, 'r', encoding='utf-8') as f: targets = [line.strip() for line in f if line.strip()]
    except Exception as e: msg.edit_text(f"❌ 读取文件失败: {e}"); return
    if not targets: msg.edit_text("❌ 文件为空。"); return
    
    total_targets = len(targets); processed_count = 0; detailed_results_for_excel = []
    for target in targets:
        processed_count += 1
        if processed_count % 10 == 0:
            try: msg.edit_text(f"分析进度: {create_progress_bar(processed_count/total_targets*100)} ({processed_count}/{total_targets})")
            except (BadRequest, RetryAfter): pass
        query = f'ip="{target}"' if ':' not in target else f'host="{target}"'
        data, _, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, query, page_size=1, fields=",".join(features)))
        if not error and data.get('results'):
            result = data['results'][0]
            row_data = {'Target': target}
            row_data.update({BATCH_FEATURES.get(f, f): result[i] for i, f in enumerate(features)})
            detailed_results_for_excel.append(row_data)
    
    if detailed_results_for_excel:
        try:
            df = pd.DataFrame(detailed_results_for_excel)
            excel_filename = generate_filename_from_query(os.path.basename(file_path), prefix="analysis", ext=".xlsx")
            df.to_excel(excel_filename, index=False, engine='openpyxl')
            msg.edit_text("✅ 分析完成！正在发送Excel报告...")
            with open(excel_filename, 'rb') as doc:
                bot.send_document(chat_id=chat_id, document=doc, caption="📄 详细特征分析Excel报告")
            upload_and_send_links(context, chat_id, excel_filename)
            os.remove(excel_filename)
        except Exception as e: msg.edit_text(f"❌ 生成Excel失败: {e}")
    else: msg.edit_text("🤷‍♀️ 分析完成，但未找到任何匹配的FOFA数据。")
    if os.path.exists(file_path): os.remove(file_path)

# --- /batch (交互式) ---
def build_batch_fields_keyboard(user_data):
    selected_fields = user_data.get('selected_fields', set())
    page = user_data.get('page', 0)
    
    flat_fields = []
    for category, fields in FIELD_CATEGORIES.items():
        for field in fields:
            flat_fields.append((field, category))
            
    items_per_page = 12
    start_index = page * items_per_page
    end_index = start_index + items_per_page
    page_items = flat_fields[start_index:end_index]
    
    keyboard = []
    for i in range(0, len(page_items), 2):
        row = []
        field1, cat1 = page_items[i]
        prefix1 = "☑️" if field1 in selected_fields else "☐"
        row.append(InlineKeyboardButton(f"{prefix1} {field1}", callback_data=f"batchfield_toggle_{field1}"))
        if i + 1 < len(page_items):
            field2, cat2 = page_items[i+1]
            prefix2 = "☑️" if field2 in selected_fields else "☐"
            row.append(InlineKeyboardButton(f"{prefix2} {field2}", callback_data=f"batchfield_toggle_{field2}"))
        keyboard.append(row)
        
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ 上一页", callback_data="batchfield_prev"))
    if end_index < len(flat_fields):
        nav_row.append(InlineKeyboardButton("下一页 ➡️", callback_data="batchfield_next"))
    keyboard.append(nav_row)
    keyboard.append([InlineKeyboardButton("✅ 完成选择并开始", callback_data="batchfield_done")])
    
    return InlineKeyboardMarkup(keyboard)

@admin_only
def batch_command(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("用法: `/batch <fofa_query>`")
        return ConversationHandler.END
    
    query_text = " ".join(context.args)
    context.user_data['query'] = query_text
    context.user_data['selected_fields'] = set(FREE_FIELDS[:5]) # 默认选择前5个免费字段
    context.user_data['page'] = 0
    
    keyboard = build_batch_fields_keyboard(context.user_data)
    update.message.reply_text(f"查询: `{query_text}`\n请选择要导出的字段:", reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    return STATE_BATCH_SELECT_FIELDS

def batch_select_fields_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    
    action = query.data.split('_', 1)[1]
    
    if action == "next":
        context.user_data['page'] += 1
    elif action == "prev":
        context.user_data['page'] -= 1
    elif action.startswith("toggle_"):
        field = action.replace("toggle_", "")
        if field in context.user_data['selected_fields']:
            context.user_data['selected_fields'].remove(field)
        else:
            context.user_data['selected_fields'].add(field)
    elif action == "done":
        selected_fields = context.user_data.get('selected_fields')
        if not selected_fields:
            query.answer("请至少选择一个字段！", show_alert=True)
            return STATE_BATCH_SELECT_FIELDS
        
        query_text = context.user_data['query']
        fields_str = ",".join(list(selected_fields))
        
        msg = query.message.edit_text("正在执行查询以预估数据量...")
        data, used_key_index, key_level, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, query_text, page_size=1, fields="host"))
        if error: msg.edit_text(f"❌ 查询出错: {error}"); return ConversationHandler.END
        
        total_size = data.get('size', 0)
        if total_size == 0: msg.edit_text("🤷‍♀️ 未找到结果。"); return ConversationHandler.END
        
        # 检查所选字段是否超出Key的权限
        allowed_fields = get_fields_by_level(key_level)
        unauthorized_fields = [f for f in selected_fields if f not in allowed_fields]
        if unauthorized_fields:
            msg.edit_text(f"⚠️ 警告: 您选择的字段 `{', '.join(unauthorized_fields)}` 超出当前可用最高级Key (等级{key_level}) 的权限。请重新选择或升级Key。")
            return ConversationHandler.END

        context.user_data.update({'chat_id': update.effective_chat.id, 'fields': fields_str, 'total_size': total_size, 'is_batch_mode': True })
        success_message = f"✅ 使用 Key [#{used_key_index}] (等级{key_level}) 找到 {total_size} 条结果。"
        
        if total_size <= 10000:
            msg.edit_text(f"{success_message}\n开始自定义字段批量导出..."); start_download_job(context, run_batch_download_query, context.user_data)
            return ConversationHandler.END
        else:
            keyboard = [[InlineKeyboardButton("💎 导出前1万条", callback_data='mode_full'), InlineKeyboardButton("🌀 深度追溯导出", callback_data='mode_traceback')], [InlineKeyboardButton("❌ 取消", callback_data='mode_cancel')]]
            msg.edit_text(f"{success_message}\n请选择导出模式:", reply_markup=InlineKeyboardMarkup(keyboard)); return STATE_KKFOFA_MODE
            
    keyboard = build_batch_fields_keyboard(context.user_data)
    query.message.edit_reply_markup(reply_markup=keyboard)
    return STATE_BATCH_SELECT_FIELDS

# --- 其他管理命令 (无变动) ---
@admin_only
def check_command(update: Update, context: CallbackContext):
    msg = update.message.reply_text("⏳ 正在执行系统自检...")
    report = ["*📋 系统自检报告*"]
    try:
        global CONFIG; CONFIG = load_json_file(CONFIG_FILE, DEFAULT_CONFIG)
        report.append("✅ *配置文件*: `config.json` 加载正常")
    except Exception as e:
        report.append(f"❌ *配置文件*: 加载失败 - {escape_markdown(str(e))}")
        msg.edit_text("\n".join(report), parse_mode=ParseMode.MARKDOWN); return
    report.append("\n*🔑 API Keys:*")
    if not CONFIG.get('apis'): report.append("  - ⚠️ 未配置任何 API Key")
    else:
        for i, key in enumerate(CONFIG['apis']):
            level = KEY_LEVELS.get(key, -1)
            level_name = {-1: "❌ 无效", 0: "✅ 免费", 1: "✅ 个人", 2: "✅ 商业", 3: "✅ 企业"}.get(level, "未知")
            report.append(f"  `#{i+1}` (`...{key[-4:]}`): {level_name}")
            
    report.append("\n*🌐 代理:*")
    proxies_to_check = CONFIG.get("proxies", [])
    if not proxies_to_check and CONFIG.get("proxy"): proxies_to_check.append(CONFIG.get("proxy"))
    if not proxies_to_check: report.append("  - ℹ️ 未配置代理")
    else:
        for p in proxies_to_check:
            try:
                requests.get("https://fofa.info", proxies={"http": p, "https": p}, timeout=10, verify=False)
                report.append(f"  - `{escape_markdown(p)}`: ✅ 连接成功")
            except Exception as e: report.append(f"  - `{escape_markdown(p)}`: ❌ 连接失败 - `{escape_markdown(str(e))}`")
    msg.edit_text("\n".join(report), parse_mode=ParseMode.MARKDOWN)
@admin_only
def stop_all_tasks(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    context.bot_data[f'stop_job_{chat_id}'] = True
    update.message.reply_text("🛑 已发送停止信号，当前下载任务将在完成本页后停止。")
@admin_only
def backup_config_command(update: Update, context: CallbackContext):
    if os.path.exists(CONFIG_FILE):
        update.effective_chat.send_document(document=open(CONFIG_FILE, 'rb'))
        upload_and_send_links(context, update.effective_chat.id, CONFIG_FILE)
    else: update.effective_chat.send_message("❌ 找不到配置文件。")
@admin_only
def restore_config_command(update: Update, context: CallbackContext):
    update.message.reply_text("请发送您的 `config.json` 备份文件。")
    return STATE_GET_RESTORE_FILE
def receive_config_file(update: Update, context: CallbackContext):
    doc = update.message.document
    if doc.file_name != 'config.json':
        update.message.reply_text("❌ 文件名错误，请确保您上传的是 `config.json`。")
        return ConversationHandler.END
    file = doc.get_file()
    file.download(custom_path=CONFIG_FILE)
    global CONFIG; CONFIG = load_json_file(CONFIG_FILE, DEFAULT_CONFIG)
    update.message.reply_text("✅ 配置文件已恢复。机器人将自动重启以应用更改。")
    shutdown_command(update, context, restart=True)
    return ConversationHandler.END
@admin_only
def history_command(update: Update, context: CallbackContext):
    if not HISTORY['queries']: update.message.reply_text("查询历史为空。"); return
    history_text = "*🕰️ 最近查询历史*\n\n"
    for i, item in enumerate(HISTORY['queries'][:15]):
        dt_utc = datetime.fromisoformat(item['timestamp']); dt_local = dt_utc.astimezone(tz.tzlocal()); time_str = dt_local.strftime('%Y-%m-%d %H:%M')
        history_text += f"`{i+1}.` `{escape_markdown(item['query_text'])}`\n   _{time_str}_\n"
    update.message.reply_text(history_text, parse_mode=ParseMode.MARKDOWN)
@admin_only
def import_command(update: Update, context: CallbackContext):
    update.message.reply_text("请发送您要导入的旧缓存文件 (txt格式)。")
    return STATE_GET_IMPORT_QUERY
def get_import_query(update: Update, context: CallbackContext):
    doc = update.message.document
    if not doc.file_name.endswith('.txt'): update.message.reply_text("❌ 请上传 .txt 文件。"); return ConversationHandler.END
    file = doc.get_file()
    temp_path = os.path.join(FOFA_CACHE_DIR, f"import_{doc.file_id}.txt")
    file.download(custom_path=temp_path)
    try:
        with open(temp_path, 'r', encoding='utf-8') as f: result_count = sum(1 for _ in f)
    except Exception as e: update.message.reply_text(f"❌ 读取文件失败: {e}"); os.remove(temp_path); return ConversationHandler.END
    query_text = update.message.text
    if not query_text: update.message.reply_text("请输入与此文件关联的原始FOFA查询语法:"); return STATE_GET_IMPORT_QUERY
    
    final_filename = generate_filename_from_query(query_text)
    final_path = os.path.join(FOFA_CACHE_DIR, final_filename)
    shutil.move(temp_path, final_path)
    
    cache_data = {'file_path': final_path, 'result_count': result_count}
    add_or_update_query(query_text, cache_data)
    update.message.reply_text(f"✅ 成功导入缓存！\n查询: `{escape_markdown(query_text)}`\n共 {result_count} 条记录。")
    return ConversationHandler.END
@admin_only
def get_log_command(update: Update, context: CallbackContext):
    if os.path.exists(LOG_FILE):
        update.message.reply_document(document=open(LOG_FILE, 'rb'))
        upload_and_send_links(context, update.effective_chat.id, LOG_FILE)
    else: update.message.reply_text("❌ 未找到日志文件。")
@admin_only
def shutdown_command(update: Update, context: CallbackContext, restart=False):
    message = "🤖 机器人正在重启..." if restart else "🤖 机器人正在关闭..."
    update.message.reply_text(message)
    logger.info(f"Shutdown/Restart initiated by user {update.effective_user.id}")
    
    updater = context.bot_data.get('updater')
    if not updater:
        logger.error("Could not find updater object in bot_data for shutdown!")
        update.message.reply_text("❌ 内部错误: 无法找到核心组件，无法关闭。")
        return

    context.job_queue.stop()
    updater.stop()
    
    if restart:
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            logger.error(f"重启失败: {e}")
            update.message.reply_text(f"❌ 重启失败: {e}")
@admin_only
def update_script_command(update: Update, context: CallbackContext):
    update_url = CONFIG.get("update_url")
    if not update_url:
        update.message.reply_text("❌ 未在设置中配置更新URL。请使用 /settings -> 脚本更新 -> 设置URL。")
        return
    msg = update.message.reply_text("⏳ 正在从远程URL下载新脚本...")
    try:
        response = requests.get(update_url, timeout=30, proxies=get_proxies())
        response.raise_for_status()
        script_content = response.text
        with open(__file__, 'w', encoding='utf-8') as f:
            f.write(script_content)
        msg.edit_text("✅ 脚本更新成功！机器人将自动重启以应用新版本。")
        shutdown_command(update, context, restart=True)
    except Exception as e:
        msg.edit_text(f"❌ 更新失败: {e}")

# --- 设置菜单 (无变动) ---
@admin_only
def settings_command(update: Update, context: CallbackContext):
    keyboard = [
        [InlineKeyboardButton("🔑 API 管理", callback_data='settings_api'), InlineKeyboardButton("✨ 预设管理", callback_data='settings_preset')],
        [InlineKeyboardButton("🌐 代理池管理", callback_data='settings_proxypool'), InlineKeyboardButton("📤 上传接口设置", callback_data='settings_upload')],
        [InlineKeyboardButton("💾 备份与恢复", callback_data='settings_backup'), InlineKeyboardButton("🔄 脚本更新", callback_data='settings_update')],
        [InlineKeyboardButton("❌ 关闭菜单", callback_data='settings_close')]
    ]
    message_text = "⚙️ *设置菜单*"; reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query: update.callback_query.message.edit_text(message_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    else: update.message.reply_text(message_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    return STATE_SETTINGS_MAIN
def settings_callback_handler(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); menu = query.data.split('_', 1)[1]
    if menu == 'api': return show_api_menu(update, context, force_check=False)
    if menu == 'proxypool': return show_proxypool_menu(update, context)
    if menu == 'backup': return show_backup_restore_menu(update, context)
    if menu == 'preset': return show_preset_menu(update, context)
    if menu == 'update': return show_update_menu(update, context)
    if menu == 'upload': return show_upload_api_menu(update, context)
    if menu == 'close': query.message.edit_text("菜单已关闭."); return ConversationHandler.END
    return STATE_SETTINGS_ACTION
def settings_action_handler(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); action = query.data.split('_', 1)[1]
    if action == 'add_api': query.message.edit_text("请输入新的FOFA API Key:"); return STATE_GET_KEY
    if action == 'remove_api': query.message.edit_text("请输入要移除的API Key的编号:"); return STATE_REMOVE_API
    if action == 'check_api': return show_api_menu(update, context, force_check=True)
    if action == 'back': return settings_command(update, context)
def show_api_menu(update: Update, context: CallbackContext, force_check=False):
    query = update.callback_query
    if force_check: 
        query.message.edit_text("⏳ 正在重新检查所有API Key状态...")
        check_and_classify_keys() # 强制检查时重新分类
    api_list_text = ["*🔑 当前 API Keys:*"]
    if not CONFIG['apis']: api_list_text.append("  - _空_")
    else:
        for i, key in enumerate(CONFIG['apis']):
            level = KEY_LEVELS.get(key, -1)
            level_name = {-1: "❌ 无效", 0: "✅ 免费", 1: "✅ 个人", 2: "✅ 商业", 3: "✅ 企业"}.get(level, "未知")
            api_list_text.append(f"  `#{i+1}` `...{key[-4:]}` - {level_name}")
    keyboard = [
        [InlineKeyboardButton("➕ 添加", callback_data='action_add_api'), InlineKeyboardButton("➖ 移除", callback_data='action_remove_api')],
        [InlineKeyboardButton("🔄 状态检查", callback_data='action_check_api'), InlineKeyboardButton("🔙 返回", callback_data='action_back')]
    ]
    query.message.edit_text("\n".join(api_list_text), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return STATE_SETTINGS_ACTION
def get_key(update: Update, context: CallbackContext):
    new_key = update.message.text.strip()
    if new_key not in CONFIG['apis']: 
        CONFIG['apis'].append(new_key); save_config()
        check_and_classify_keys() # 添加后立即分类
        update.message.reply_text("✅ API Key 已添加。")
    else: update.message.reply_text("⚠️ 此 Key 已存在。")
    return settings_command(update, context)
def remove_api(update: Update, context: CallbackContext):
    try:
        index = int(update.message.text.strip()) - 1
        if 0 <= index < len(CONFIG['apis']):
            removed_key = CONFIG['apis'].pop(index); save_config()
            check_and_classify_keys() # 移除后重新分类
            update.message.reply_text(f"✅ 已移除 Key `...{removed_key[-4:]}`。")
        else: update.message.reply_text("❌ 无效的编号。")
    except ValueError: update.message.reply_text("❌ 请输入一个有效的数字编号。")
    return settings_command(update, context)
def show_preset_menu(update: Update, context: CallbackContext):
    query = update.callback_query; presets = CONFIG.get("presets", [])
    text = ["*✨ 预设查询管理*"]
    if not presets: text.append("  - _空_")
    else:
        for i, p in enumerate(presets): text.append(f"`{i+1}.` *{p['name']}*: `{p['query']}`")
    keyboard = [
        [InlineKeyboardButton("➕ 添加", callback_data='preset_add'), InlineKeyboardButton("➖ 移除", callback_data='preset_remove')],
        [InlineKeyboardButton("🔙 返回", callback_data='preset_back')]
    ]
    query.message.edit_text("\n".join(text), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return STATE_PRESET_MENU
def preset_menu_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); action = query.data.split('_')[1]
    if action == 'add': query.message.edit_text("请输入预设的名称:"); return STATE_GET_PRESET_NAME
    if action == 'remove': query.message.edit_text("请输入要移除的预设的编号:"); return STATE_REMOVE_PRESET
    if action == 'back': return settings_command(update, context)
def get_preset_name(update: Update, context: CallbackContext):
    context.user_data['preset_name'] = update.message.text.strip()
    update.message.reply_text("请输入此预设的FOFA查询语法:")
    return STATE_GET_PRESET_QUERY
def get_preset_query(update: Update, context: CallbackContext):
    preset_query = update.message.text.strip(); preset_name = context.user_data['preset_name']
    CONFIG.setdefault("presets", []).append({"name": preset_name, "query": preset_query}); save_config()
    update.message.reply_text("✅ 预设已添加。")
    return settings_command(update, context)
def remove_preset(update: Update, context: CallbackContext):
    try:
        index = int(update.message.text.strip()) - 1
        if 0 <= index < len(CONFIG['presets']):
            CONFIG['presets'].pop(index); save_config()
            update.message.reply_text("✅ 预设已移除。")
        else: update.message.reply_text("❌ 无效的编号。")
    except ValueError: update.message.reply_text("❌ 请输入一个有效的数字编号。")
    return settings_command(update, context)
def show_update_menu(update: Update, context: CallbackContext):
    query = update.callback_query
    url = CONFIG.get("update_url") or "未设置"
    text = f"🔄 *脚本更新设置*\n\n当前更新URL: `{escape_markdown(url)}`"
    keyboard = [[InlineKeyboardButton("✏️ 设置URL", callback_data='update_set_url'), InlineKeyboardButton("🔙 返回", callback_data='update_back')]]
    query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return STATE_SETTINGS_ACTION
def get_update_url(update: Update, context: CallbackContext):
    url = update.message.text.strip()
    if url.lower().startswith('http'): CONFIG['update_url'] = url; save_config(); update.message.reply_text("✅ 更新URL已设置。")
    else: update.message.reply_text("❌ 无效的URL格式。")
    return settings_command(update, context)
def show_backup_restore_menu(update: Update, context: CallbackContext):
    query = update.callback_query
    text = "💾 *备份与恢复*\n\n- *备份*: 发送当前的 `config.json` 文件给您。\n- *恢复*: 您需要向机器人发送一个 `config.json` 文件来覆盖当前配置。"
    keyboard = [[InlineKeyboardButton("📤 备份", callback_data='backup_now'), InlineKeyboardButton("📥 恢复", callback_data='restore_now')], [InlineKeyboardButton("🔙 返回", callback_data='backup_back')]]
    query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return STATE_SETTINGS_ACTION
def show_proxypool_menu(update: Update, context: CallbackContext):
    query = update.callback_query
    proxies = CONFIG.get("proxies", [])
    text = ["*🌐 代理池管理*"]
    if not proxies: text.append("  - _空_")
    else:
        for i, p in enumerate(proxies): text.append(f"`{i+1}.` `{p}`")
    keyboard = [
        [InlineKeyboardButton("➕ 添加", callback_data='proxypool_add'), InlineKeyboardButton("➖ 移除", callback_data='proxypool_remove')],
        [InlineKeyboardButton("🔙 返回", callback_data='proxypool_back')]
    ]
    query.message.edit_text("\n".join(text), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return STATE_PROXYPOOL_MENU
def proxypool_menu_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); action = query.data.split('_')[1]
    if action == 'add': query.message.edit_text("请输入要添加的代理 (格式: `http://user:pass@host:port`):"); return STATE_GET_PROXY_ADD
    if action == 'remove': query.message.edit_text("请输入要移除的代理的编号:"); return STATE_GET_PROXY_REMOVE
    if action == 'back': return settings_command(update, context)
def get_proxy_to_add(update: Update, context: CallbackContext):
    proxy = update.message.text.strip()
    if proxy not in CONFIG['proxies']: CONFIG['proxies'].append(proxy); save_config(); update.message.reply_text("✅ 代理已添加。")
    else: update.message.reply_text("⚠️ 此代理已存在。")
    return settings_command(update, context)
def get_proxy_to_remove(update: Update, context: CallbackContext):
    try:
        index = int(update.message.text.strip()) - 1
        if 0 <= index < len(CONFIG['proxies']):
            CONFIG['proxies'].pop(index); save_config()
            update.message.reply_text("✅ 代理已移除。")
        else: update.message.reply_text("❌ 无效的编号。")
    except ValueError: update.message.reply_text("❌ 请输入一个有效的数字编号。")
    return settings_command(update, context)
def show_upload_api_menu(update: Update, context: CallbackContext):
    query = update.callback_query
    url = CONFIG.get("upload_api_url") or "未设置"
    token_status = "已设置" if CONFIG.get("upload_api_token") else "未设置"
    text = (f"📤 *上传接口设置*\n\n"
            f"此功能可将机器人生成的所有文件自动上传到您指定的服务器，并返回下载命令。\n\n"
            f"*API URL:* `{escape_markdown(url)}`\n"
            f"*API Token:* `{token_status}`")
    kbd = [
        [InlineKeyboardButton("✏️ 设置 URL", callback_data='upload_set_url'), InlineKeyboardButton("🔑 设置 Token", callback_data='upload_set_token')],
        [InlineKeyboardButton("🔙 返回", callback_data='upload_back')]
    ]
    query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kbd), parse_mode=ParseMode.MARKDOWN)
    return STATE_UPLOAD_API_MENU
def upload_api_menu_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); action = query.data.split('_', 1)[1]
    if action == 'back': return settings_command(update, context)
    if action == 'set_url': query.message.edit_text("请输入您的上传接口 URL:"); return STATE_GET_UPLOAD_URL
    if action == 'set_token': query.message.edit_text("请输入您的上传接口 Token:"); return STATE_GET_UPLOAD_TOKEN
    return STATE_UPLOAD_API_MENU
def get_upload_url(update: Update, context: CallbackContext):
    url = update.message.text.strip()
    if url.lower().startswith('http'):
        CONFIG['upload_api_url'] = url; save_config()
        update.message.reply_text("✅ 上传 URL 已更新。")
    else: update.message.reply_text("❌ 无效的 URL 格式。")
    return settings_command(update, context)
def get_upload_token(update: Update, context: CallbackContext):
    token = update.message.text.strip()
    CONFIG['upload_api_token'] = token; save_config()
    update.message.reply_text("✅ 上传 Token 已更新。")
    return settings_command(update, context)

# --- 主函数与调度器 ---
def main() -> None:
    global CONFIG
    os.makedirs(FOFA_CACHE_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_FILE) or CONFIG.get("bot_token") == "YOUR_BOT_TOKEN_HERE":
        print("--- 首次运行或配置不完整，进入交互式设置 ---")
        bot_token = input("请输入您的 Telegram Bot Token: ").strip()
        admin_id = input("请输入您的 Telegram User ID (作为第一个管理员): ").strip()
        if not bot_token or not admin_id.isdigit(): print("错误：Bot Token 和 Admin ID 不能为空且ID必须是数字。请重新运行脚本。"); sys.exit(1)
        CONFIG["bot_token"] = bot_token; CONFIG["admins"] = [int(admin_id)]
        fofa_keys = []; print("请输入您的 FOFA API Key (输入空行结束):")
        while True:
            key = input(f"  - Key #{len(fofa_keys) + 1}: ").strip()
            if not key: break
            fofa_keys.append(key)
        CONFIG["apis"] = fofa_keys
        save_config(); print("✅ 配置已保存到 config.json。正在启动机器人...")
        CONFIG = load_json_file(CONFIG_FILE, DEFAULT_CONFIG)

    bot_token = CONFIG.get("bot_token")
    if not bot_token or bot_token == "YOUR_BOT_TOKEN_HERE": logger.critical("错误: 'bot_token' 未在 config.json 中设置!"); return
    
    check_and_classify_keys() # 启动时分类Key
    
    persistence = PicklePersistence(filename=PERSISTENCE_FILE)
    updater = Updater(token=bot_token, use_context=True, persistence=persistence)
    dispatcher = updater.dispatcher
    dispatcher.bot_data['updater'] = updater
    
    commands = [
        BotCommand("start", "🚀 启动机器人"), BotCommand("help", "❓ 命令手册"),
        BotCommand("kkfofa", "🔍 资产搜索/预设"), BotCommand("host", "📦 主机详查 (智能)"),
        BotCommand("lowhost", "🔬 主机速查 (开放)"), BotCommand("stats", "📊 全局聚合统计"),
        BotCommand("batchfind", "📂 批量智能分析 (Excel)"), BotCommand("batch", "📤 批量自定义导出 (交互式)"),
        BotCommand("check", "🩺 系统自检"), BotCommand("settings", "⚙️ 设置菜单"),
        BotCommand("history", "🕰️ 查询历史"), BotCommand("import", "🖇️ 导入旧缓存"),
        BotCommand("backup", "📤 备份配置"), BotCommand("restore", "📥 恢复配置"),
        BotCommand("update", "🔄 在线更新脚本"), BotCommand("getlog", "📄 获取日志"),
        BotCommand("shutdown", "🔌 关闭机器人"), BotCommand("stop", "🛑 停止任务"),
        BotCommand("cancel", "❌ 取消操作")
    ]
    try: updater.bot.set_my_commands(commands)
    except Exception as e: logger.warning(f"设置机器人命令失败: {e}")

    settings_conv = ConversationHandler(
        entry_points=[CommandHandler("settings", settings_command)],
        states={
            STATE_SETTINGS_MAIN: [CallbackQueryHandler(settings_callback_handler, pattern=r"^settings_")],
            STATE_SETTINGS_ACTION: [
                CallbackQueryHandler(settings_action_handler, pattern=r"^action_"),
                CallbackQueryHandler(show_update_menu, pattern=r"^settings_update"),
                CallbackQueryHandler(show_backup_restore_menu, pattern=r"^settings_backup"),
                CallbackQueryHandler(lambda u,c: backup_config_command(u.callback_query, c), pattern=r"^backup_now"),
                CallbackQueryHandler(lambda u,c: restore_config_command(u.callback_query.message, c), pattern=r"^restore_now"),
                CallbackQueryHandler(get_update_url, pattern=r"^update_set_url"),
                CallbackQueryHandler(settings_command, pattern=r"^(update_back|backup_back)"),
            ],
            STATE_GET_KEY: [MessageHandler(Filters.text & ~Filters.command, get_key)],
            STATE_REMOVE_API: [MessageHandler(Filters.text & ~Filters.command, remove_api)],
            STATE_PRESET_MENU: [CallbackQueryHandler(preset_menu_callback, pattern=r"^preset_")],
            STATE_GET_PRESET_NAME: [MessageHandler(Filters.text & ~Filters.command, get_preset_name)],
            STATE_GET_PRESET_QUERY: [MessageHandler(Filters.text & ~Filters.command, get_preset_query)],
            STATE_REMOVE_PRESET: [MessageHandler(Filters.text & ~Filters.command, remove_preset)],
            STATE_GET_UPDATE_URL: [MessageHandler(Filters.text & ~Filters.command, get_update_url)],
            STATE_PROXYPOOL_MENU: [CallbackQueryHandler(proxypool_menu_callback, pattern=r"^proxypool_")],
            STATE_GET_PROXY_ADD: [MessageHandler(Filters.text & ~Filters.command, get_proxy_to_add)],
            STATE_GET_PROXY_REMOVE: [MessageHandler(Filters.text & ~Filters.command, get_proxy_to_remove)],
            STATE_UPLOAD_API_MENU: [CallbackQueryHandler(upload_api_menu_callback, pattern=r"^upload_")],
            STATE_GET_UPLOAD_URL: [MessageHandler(Filters.text & ~Filters.command, get_upload_url)],
            STATE_GET_UPLOAD_TOKEN: [MessageHandler(Filters.text & ~Filters.command, get_upload_token)],
        },
        fallbacks=[CommandHandler("cancel", cancel)], conversation_timeout=300,
        persistent=True, name="settings_conv"
    )
    kkfofa_conv = ConversationHandler(
        entry_points=[ CommandHandler("kkfofa", kkfofa_entry), CallbackQueryHandler(kkfofa_entry, pattern=r"^run_preset_") ],
        states={
            STATE_GET_GUEST_KEY: [MessageHandler(Filters.text & ~Filters.command, get_guest_key)],
            STATE_ASK_CONTINENT: [CallbackQueryHandler(ask_continent_callback, pattern=r"^continent_")], 
            STATE_CONTINENT_CHOICE: [CallbackQueryHandler(continent_choice_callback, pattern=r"^continent_")], 
            STATE_CACHE_CHOICE: [CallbackQueryHandler(cache_choice_callback, pattern=r"^cache_")],
            STATE_KKFOFA_MODE: [CallbackQueryHandler(query_mode_callback, pattern=r"^mode_")],
            STATE_GET_TRACEBACK_LIMIT: [MessageHandler(Filters.text & ~Filters.command, get_traceback_limit), CallbackQueryHandler(get_traceback_limit, pattern=r"^limit_")]
        },
        fallbacks=[CommandHandler("cancel", cancel)], conversation_timeout=300,
        persistent=True, name="kkfofa_conv"
    )
    batch_conv = ConversationHandler(
        entry_points=[CommandHandler("batch", batch_command)], 
        states={
            STATE_BATCH_SELECT_FIELDS: [CallbackQueryHandler(batch_select_fields_callback, pattern=r"^batchfield_")],
            STATE_KKFOFA_MODE: [CallbackQueryHandler(query_mode_callback, pattern=r"^mode_")],
            STATE_GET_TRACEBACK_LIMIT: [MessageHandler(Filters.text & ~Filters.command, get_traceback_limit), CallbackQueryHandler(get_traceback_limit, pattern=r"^limit_")]
        },
        fallbacks=[CommandHandler('cancel', cancel)], conversation_timeout=600,
        persistent=True, name="batch_conv"
    )
    import_conv = ConversationHandler(entry_points=[CommandHandler("import", import_command)], states={STATE_GET_IMPORT_QUERY: [MessageHandler(Filters.document.mime_type("text/plain"), get_import_query)]}, fallbacks=[CommandHandler("cancel", cancel)], conversation_timeout=300, persistent=True, name="import_conv")
    stats_conv = ConversationHandler(entry_points=[CommandHandler("stats", stats_command)], states={STATE_GET_STATS_QUERY: [MessageHandler(Filters.text & ~Filters.command, get_fofa_stats_query)]}, fallbacks=[CommandHandler("cancel", cancel)], conversation_timeout=300, persistent=True, name="stats_conv")
    batchfind_conv = ConversationHandler(entry_points=[CommandHandler("batchfind", batchfind_command)], states={STATE_GET_BATCH_FILE: [MessageHandler(Filters.document.mime_type("text/plain"), get_batch_file_handler)], STATE_SELECT_BATCH_FEATURES: [CallbackQueryHandler(select_batch_features_callback, pattern=r"^batchfeature_")]}, fallbacks=[CommandHandler("cancel", cancel)], conversation_timeout=300, persistent=True, name="batchfind_conv")
    restore_conv = ConversationHandler(entry_points=[CommandHandler("restore", restore_config_command)], states={STATE_GET_RESTORE_FILE: [MessageHandler(Filters.document, receive_config_file)]}, fallbacks=[CommandHandler("cancel", cancel)], conversation_timeout=300, persistent=True, name="restore_conv")
    scan_conv = ConversationHandler(entry_points=[CallbackQueryHandler(start_scan_callback, pattern=r'^start_scan_')], states={STATE_GET_SCAN_CONCURRENCY: [MessageHandler(Filters.text & ~Filters.command, get_concurrency_callback)], STATE_GET_SCAN_TIMEOUT: [MessageHandler(Filters.text & ~Filters.command, get_timeout_callback)]}, fallbacks=[CommandHandler('cancel', cancel)], conversation_timeout=120, persistent=True, name="scan_conv")

    dispatcher.add_handler(CommandHandler("start", start_command)); dispatcher.add_handler(CommandHandler("help", help_command)); dispatcher.add_handler(CommandHandler("host", host_command)); dispatcher.add_handler(CommandHandler("lowhost", lowhost_command)); dispatcher.add_handler(CommandHandler("check", check_command)); dispatcher.add_handler(CommandHandler("stop", stop_all_tasks)); dispatcher.add_handler(CommandHandler("backup", backup_config_command)); dispatcher.add_handler(CommandHandler("history", history_command)); dispatcher.add_handler(CommandHandler("getlog", get_log_command)); dispatcher.add_handler(CommandHandler("shutdown", shutdown_command)); dispatcher.add_handler(CommandHandler("update", update_script_command));
    dispatcher.add_handler(settings_conv); dispatcher.add_handler(kkfofa_conv); dispatcher.add_handler(batch_conv); dispatcher.add_handler(import_conv); dispatcher.add_handler(stats_conv); dispatcher.add_handler(batchfind_conv); dispatcher.add_handler(restore_conv); dispatcher.add_handler(scan_conv)

    logger.info(f"🚀 Fofa Bot v9.8 (会员逻辑修复版) 已启动...")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
