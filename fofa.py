#
# fofa_final_complete_v8.6.py (最终完整版 for python-telegram-bot v13.x)
#
# 核心修改 (v8.6): 1. /host 命令生成的详细报告文件中，Banner和Header不再被截断，保证信息完整性。
# 核心修改 (v8.6): 2. /batchfind 功能升级，使用正则表达式智能解析文件，兼容各种复杂的 "ip:port..." 格式。
#
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
from functools import wraps
from datetime import datetime, timedelta
from dateutil import tz
from concurrent.futures import ThreadPoolExecutor, as_completed

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
from telegram.error import BadRequest, RetryAfter

# --- 全局变量和常量 ---
CONFIG_FILE = 'config.json'
HISTORY_FILE = 'history.json'
LOG_FILE = 'fofa_bot.log'
FOFA_CACHE_DIR = 'fofa_file'
MAX_HISTORY_SIZE = 50
CACHE_EXPIRATION_SECONDS = 24 * 60 * 60
FOFA_SEARCH_URL = "https://fofa.info/api/v1/search/all"
FOFA_INFO_URL = "https://fofa.info/api/v1/info/my"
FOFA_STATS_URL = "https://fofa.info/api/v1/search/stats"
FOFA_HOST_BASE_URL = "https://fofa.info/api/v1/host/"
FOFA_STATS_FIELDS = "protocol,domain,port,title,os,server,country,asn,org,asset_type,fid,icp"
SCAN_TIMEOUT = 3
SCAN_CONCURRENCY = 1000

# --- Banner/Header 指纹提取 ---
def normalize_banner(banner_text):
    if not isinstance(banner_text, str): return ""
    normalized = re.sub(r'(Date|Last-Modified|Expires):\s+.*?GMT', r'\1: [TIMESTAMP]', banner_text, flags=re.IGNORECASE)
    normalized = re.sub(r'CF-RAY:\s+[a-f0-9\-]+', 'CF-RAY: [ID]', normalized, flags=re.IGNORECASE)
    normalized = re.sub(r'ETag:\s+.*?"', 'ETag: [HASH]"', normalized, flags=re.IGNORECASE)
    normalized = re.sub(r'Set-Cookie:\s+([^=]+)=.*?(;|$)', r'Set-Cookie: \1=[SESSION];', normalized, flags=re.IGNORECASE)
    normalized = re.sub(r'(X-Request-Id|Trace-Id|Request-Id):\s+[a-zA-Z0-9\-]+', r'\1: [ID]', normalized, flags=re.IGNORECASE)
    normalized = re.sub(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', '[IP_ADDRESS]', normalized)
    normalized = re.sub(r'[a-f0-9]{32,}', '[LONG_HASH]', normalized, flags=re.IGNORECASE)
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return normalized

# 特征分析功能相关
BATCH_FEATURES = {
    "protocol": "协议", "domain": "域名", "os": "操作系统", "server": "服务/组件",
    "icp": "ICP备案号", "title": "标题", "jarm": "JARM指纹",
    "cert.issuer.org": "证书颁发组织", "cert.issuer.cn": "证书颁发CN",
    "cert.subject.org": "证书主体组织", "cert.subject.cn": "证书主体CN",
    "banner": "Banner指纹", "header": "Header指纹"
}

CONTINENT_COUNTRIES = {
    "Asia": ["AF", "AM", "AZ", "BH", "BD", "BT", "BN", "KH", "CN", "CY", "GE", "HK", "IN", "ID", "IR", "IQ", "IL", "JP", "JO", "KZ", "KW", "KG", "LA", "LB", "MO", "MY", "MV", "MN", "MM", "NP", "KP", "OM", "PK", "PS", "PH", "QA", "SA", "SG", "KR", "LK", "SY", "TW", "TJ", "TH", "TL", "TR", "TM", "AE", "UZ", "VN", "YE"],
    "Europe": ["AL", "AD", "AT", "BY", "BE", "BA", "BG", "HR", "CZ", "DK", "EE", "FO", "FI", "FR", "DE", "GI", "GR", "HU", "IS", "IE", "IT", "LV", "LI", "LT", "LU", "MK", "MT", "MD", "MC", "ME", "NL", "NO", "PL", "PT", "RO", "RU", "SM", "RS", "SK", "SI", "ES", "SE", "CH", "UA", "GB", "VA"],
    "NorthAmerica": ["AG", "BS", "BB", "BZ", "CA", "CR", "CU", "DM", "DO", "SV", "GD", "GT", "HT", "HN", "JM", "MX", "NI", "PA", "KN", "LC", "VC", "TT", "US"],
    "SouthAmerica": ["AR", "BO", "BR", "CL", "CO", "EC", "FK", "GY", "PY", "PE", "SR", "UY", "VE"],
    "Africa": ["DZ", "AO", "BJ", "BW", "BF", "BI", "CM", "CV", "CF", "TD", "KM", "CG", "CD", "CI", "DJ", "EG", "GQ", "ER", "ET", "GA", "GM", "GH", "GN", "GW", "KE", "LS", "LR", "LY", "MG", "MW", "ML", "MR", "MU", "YT", "MA", "MZ", "NA", "NE", "NG", "RE", "RW", "ST", "SN", "SC", "SL", "SO", "ZA", "SS", "SD", "SZ", "TZ", "TG", "TN", "UG", "EH", "ZM", "ZW"],
    "Oceania": ["AS", "AU", "FJ", "GU", "KI", "MH", "FM", "NR", "NZ", "PW", "PG", "WS", "SB", "TO", "TV", "VU"]
}

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
    STATE_SETTINGS_MAIN, STATE_SETTINGS_ACTION, STATE_GET_KEY, STATE_REMOVE_API, STATE_GET_PROXY,
    STATE_KKFOFA_MODE, STATE_CACHE_CHOICE, STATE_GET_IMPORT_QUERY, STATE_GET_STATS_QUERY,
    STATE_PRESET_MENU, STATE_GET_PRESET_NAME, STATE_GET_PRESET_QUERY, STATE_REMOVE_PRESET,
    STATE_GET_UPDATE_URL,
    STATE_ASK_CONTINENT, STATE_CONTINENT_CHOICE,
    STATE_GET_BATCH_FILE, STATE_SELECT_BATCH_FEATURES,
    STATE_GET_RESTORE_FILE
) = range(19)

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

DEFAULT_CONFIG = { "bot_token": "YOUR_BOT_TOKEN_HERE", "apis": [], "admins": [], "proxy": "", "full_mode": False, "public_mode": False, "presets": [], "update_url": "" }
CONFIG = load_json_file(CONFIG_FILE, DEFAULT_CONFIG)
HISTORY = load_json_file(HISTORY_FILE, {"queries": []})

def save_config(): save_json_file(CONFIG_FILE, CONFIG)
def save_history(): save_json_file(HISTORY_FILE, HISTORY)
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
    save_history()
def find_cached_query(query_text):
    query = next((q for q in HISTORY['queries'] if q['query_text'] == query_text), None)
    if query and query.get('cache'):
        if 'file_path' in query['cache'] and os.path.exists(query['cache']['file_path']):
            return query
    return None

# --- 辅助函数与装饰器 ---
def generate_filename_from_query(query_text: str, prefix: str = "fofa") -> str:
    sanitized_query = re.sub(r'[^a-z0-9\-_]+', '_', query_text.lower()).strip('_')
    max_len = 100
    if len(sanitized_query) > max_len: sanitized_query = sanitized_query[:max_len].rsplit('_', 1)[0]
    timestamp = int(time.time()); return f"{prefix}_{sanitized_query}_{timestamp}.txt"
def get_proxies():
    if CONFIG.get("proxy"): return {"http": CONFIG["proxy"], "https": CONFIG["proxy"]}
    return None
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
    escape_chars = r'_*`['; return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)
def create_progress_bar(percentage: float, length: int = 10) -> str:
    if percentage < 0: percentage = 0
    if percentage > 100: percentage = 100
    filled_length = int(length * percentage // 100)
    bar = '█' * filled_length + '░' * (length - filled_length)
    return f"[{bar}] {percentage:.1f}%"

# --- FOFA API 核心逻辑 ---
def _make_api_request(url, params, timeout=60, use_b64=True, retries=10):
    if use_b64 and 'q' in params:
        params['qbase64'] = base64.b64encode(params.pop('q').encode('utf-8')).decode('utf-8')
    
    last_error = None
    for attempt in range(retries):
        try:
            response = requests.get(url, params=params, timeout=timeout, proxies=get_proxies(), verify=False)
            
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
def execute_query_with_fallback(query_func, preferred_key_index=None):
    if not CONFIG['apis']: return None, None, "没有配置任何API Key。"
    keys_to_try = CONFIG['apis']; start_index = 0
    if preferred_key_index is not None and 1 <= preferred_key_index <= len(keys_to_try): start_index = preferred_key_index - 1
    for i in range(len(keys_to_try)):
        idx = (start_index + i) % len(keys_to_try); key = keys_to_try[idx]; key_num = idx + 1
        data, error = query_func(key)
        if not error: return data, key_num, None
        if "[820031]" in str(error): logger.warning(f"Key [#{key_num}] F点余额不足..."); continue
        return None, key_num, error
    return None, None, "所有Key均尝试失败。"

# --- /host & /stats 命令处理 ---
def get_common_host_info(data):
    fields = "ip,port,protocol,country,country_name,region,city,longitude,latitude,asn,org,host,domain,os,server,icp,title,jarm,header,banner,cert,base_protocol,link,cert.issuer.org,cert.issuer.cn,cert.subject.org,cert.subject.cn,tls.ja3s,tls.version,cert.sn,cert.not_before,cert.not_after,cert.domain".split(',')
    field_map = {name: idx for idx, name in enumerate(fields)}
    
    common_info = {'ip': set(), 'asn': set(), 'org': set(), 'country': set(), 'os': set(), 'domain': set()}
    for res in data['results']:
        common_info['ip'].add(res[field_map['ip']]); common_info['asn'].add(res[field_map['asn']]); common_info['org'].add(res[field_map['org']])
        common_info['country'].add(f"{res[field_map['country_name']]} ({res[field_map['country']]})")
        if res[field_map['os']]: common_info['os'].add(res[field_map['os']])
        if res[field_map['domain']]: common_info['domain'].add(res[field_map['domain']])
    
    ports_data = {}
    for res in data['results']:
        port = res[field_map['port']]; ports_data.setdefault(port, []).append(res)
        
    return common_info, ports_data, field_map

def create_host_summary(query_host, data):
    if not data or not data.get('results'):
        return f"🤷‍♀️ 未找到关于 `{escape_markdown(query_host)}` 的详细信息。"
    
    common_info, ports_data, field_map = get_common_host_info(data)
    
    def join_set(s):
        s_list = sorted([item for item in s if item])
        return '`, `'.join(map(escape_markdown, map(str, s_list))) if s_list else "N/A"

    lines = [f"📋 *主机摘要: `{escape_markdown(query_host)}`*"]
    lines.append(f"*IP:* `{join_set(common_info['ip'])}`"); lines.append(f"*ASN:* `{join_set(common_info['asn'])}`"); lines.append(f"*组织:* `{join_set(common_info['org'])}`"); lines.append(f"*国家:* `{join_set(common_info['country'])}`")
    if common_info['os']: lines.append(f"*操作系统:* `{join_set(common_info['os'])}`")
    if common_info['domain']: lines.append(f"*关联域名:* `{join_set(common_info['domain'])}`")
    
    lines.append("\n*开放端口:*")
    if not ports_data:
        lines.append("  _未发现开放端口_")
    else:
        for port in sorted(ports_data.keys()):
            first_res = ports_data[port][0]
            protocol = first_res[field_map['protocol']]; title = first_res[field_map['title']]
            line = f"  - `{port}` ({protocol})"
            if title: line += f": `{escape_markdown(title)}`"
            lines.append(line)
            
    return "\n".join(lines)

def format_full_host_report(query_host, data):
    if not data or not data.get('results'):
        return f"🤷‍♀️ 未找到关于 `{escape_markdown(query_host)}` 的详细信息。"
    
    common_info, ports_data, field_map = get_common_host_info(data)
    
    def join_set(s):
        s_list = sorted([item for item in s if item])
        return '`, `'.join(map(escape_markdown, map(str, s_list))) if s_list else "N/A"

    lines = [f"📋 *主机详细信息: `{escape_markdown(query_host)}`*"]
    lines.append(f"*IP:* `{join_set(common_info['ip'])}`"); lines.append(f"*ASN:* `{join_set(common_info['asn'])}`"); lines.append(f"*组织:* `{join_set(common_info['org'])}`"); lines.append(f"*国家:* `{join_set(common_info['country'])}`")
    if common_info['os']: lines.append(f"*操作系统:* `{join_set(common_info['os'])}`")
    if common_info['domain']: lines.append(f"*关联域名:* `{join_set(common_info['domain'])}`")
    
    for port in sorted(ports_data.keys()):
        first_res = ports_data[port][0]; protocol = first_res[field_map['protocol']]
        lines.append(f"\n--- *端口: {port}* ({protocol}) ---")
        if first_res[field_map['title']]: lines.append(f"  *标题:* `{escape_markdown(first_res[field_map['title']])}`")
        if first_res[field_map['server']]: lines.append(f"  *Server:* `{escape_markdown(first_res[field_map['server']])}`")
        if first_res[field_map['jarm']]: lines.append(f"  *JARM:* `{escape_markdown(first_res[field_map['jarm']])}`")
        if first_res[field_map['cert.subject.cn']]: lines.append(f"  *证书主体:* `{escape_markdown(first_res[field_map['cert.subject.cn']])}`")
        if first_res[field_map['cert.issuer.cn']]: lines.append(f"  *证书颁发者:* `{escape_markdown(first_res[field_map['cert.issuer.cn']])}`")
        if first_res[field_map['cert.domain']]:
            cert_domains = first_res[field_map['cert.domain']].split(',')
            lines.append(f"  *证书域名:* `{escape_markdown(', '.join(cert_domains[:3]))}`" + ( "..." if len(cert_domains) > 3 else ""))
        
        header = first_res[field_map['header']]
        if header:
            lines.append(f"  *Header:*\n  ```\n{escape_markdown(header.strip())}\n  ```")
            
        banner = first_res[field_map['banner']]
        if banner:
            lines.append(f"  *Banner:*\n  ```\n{escape_markdown(banner.strip())}\n  ```")
            
    return "\n".join(lines)

@admin_only
def host_command(update: Update, context: CallbackContext) -> None:
    if not context.args:
        update.message.reply_text("用法: `/host <ip_or_domain>`\n\n示例:\n`/host 1.1.1.1`\n`/host example.com`", parse_mode=ParseMode.MARKDOWN)
        return
    host_arg = context.args[0]; processing_message = update.message.reply_text(f"⏳ 正在查询主机 `{escape_markdown(host_arg)}`...")
    query = f'ip="{host_arg}"' if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", host_arg) else f'domain="{host_arg}"'
    fields = "ip,port,protocol,country,country_name,region,city,longitude,latitude,asn,org,host,domain,os,server,icp,title,jarm,header,banner,cert,base_protocol,link,cert.issuer.org,cert.issuer.cn,cert.subject.org,cert.subject.cn,tls.ja3s,tls.version,cert.sn,cert.not_before,cert.not_after,cert.domain"
    data, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, query, page_size=100, fields=fields))
    if error:
        processing_message.edit_text(f"查询失败 😞\n*原因:* `{error}`", parse_mode=ParseMode.MARKDOWN)
        return
    
    full_report = format_full_host_report(host_arg, data)
    
    if len(full_report) > 1500:
        summary_report = create_host_summary(host_arg, data)
        processing_message.edit_text(summary_report, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        
        report_filename = f"host_details_{host_arg.replace('.', '_')}.txt"
        try:
            with open(report_filename, 'w', encoding='utf-8') as f:
                f.write(re.sub(r'([*_`])', '', full_report))
            with open(report_filename, 'rb') as doc:
                context.bot.send_document(chat_id=update.effective_chat.id, document=doc, caption="📄 完整的详细报告已附上。")
        finally:
            if os.path.exists(report_filename):
                os.remove(report_filename)
    else:
        processing_message.edit_text(full_report, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

@admin_only
def get_fofa_stats_query(update: Update, context: CallbackContext) -> int:
    query_text = update.message.text; processing_message = update.message.reply_text(f"⏳ 正在为 `{escape_markdown(query_text)}` 查询聚合统计...")
    data, _, error = execute_query_with_fallback(lambda key: fetch_fofa_stats(key, query_text))
    if error: processing_message.edit_text(f"查询失败 😞\n*原因:* `{error}`", parse_mode=ParseMode.MARKDOWN); return ConversationHandler.END
    stats_data = data; aggs = stats_data.get("aggs", {})
    try: total_size_formatted = f"{stats_data.get('size', 0):,}"
    except (ValueError, TypeError): total_size_formatted = str(stats_data.get('size', 'N/A'))
    message_lines = [ f"*📊 FOFA 聚合统计信息*", f"*查询:* `{escape_markdown(query_text)}`", f"*总数:* *{total_size_formatted}*", f"*最后更新:* `{stats_data.get('lastupdatetime', 'N/A')}`", "" ]
    display_map = { "🌍 Top 5 国家/地区": "countries", "🏢 Top 5 组织 (ORG)": "org", "📛 Top 5 ASN": "asn", "🖥️ Top 5 服务/组件": "server", "🔌 Top 5 协议": "protocol", "⚙️ Top 5 操作系统": "os", "🚪 Top 5 端口": "port", }
    for title, key in display_map.items():
        items = aggs.get(key)
        if items:
            message_lines.append(f"*{title}:*")
            for item in items[:5]:
                try: name = escape_markdown(item.get('name', 'N/A')); count_formatted = f"{item.get('count', 0):,}"
                except (ValueError, TypeError): name = str(item.get('name', 'N/A')); count_formatted = str(item.get('count', 0))
                message_lines.append(f"  - `{name}`: *{count_formatted}*")
            message_lines.append("")
    processing_message.edit_text("\n".join(message_lines), parse_mode=ParseMode.MARKDOWN); return ConversationHandler.END
@admin_only
def stats_command(update: Update, context: CallbackContext) -> int:
    update.message.reply_text("请输入你想要进行聚合统计的 FOFA 语法。\n例如: `app=\"nginx\"`\n\n随时可以发送 /cancel 来取消。", parse_mode=ParseMode.MARKDOWN); return STATE_GET_STATS_QUERY

# --- 后台任务与扫描逻辑 ---
def offer_post_download_actions(context: CallbackContext, chat_id, query_text):
    query_hash = hashlib.md5(query_text.encode()).hexdigest()
    context.bot_data[query_hash] = query_text
    keyboard = [[ InlineKeyboardButton("⚡️ 存活检测", callback_data=f'liveness_{query_hash}'), InlineKeyboardButton("🌐 子网扫描(/24)", callback_data=f'subnet_{query_hash}') ]]
    context.bot.send_message(chat_id, "下载完成，需要对结果进行二次扫描吗？", reply_markup=InlineKeyboardMarkup(keyboard))
def download_and_process_file(context: CallbackContext, query_hash, prefix, processor_func, final_message_func):
    bot = context.bot; job_context = context.job.context; chat_id, msg = job_context['chat_id'], job_context['msg']
    original_query = context.bot_data.get(query_hash)
    if not original_query: msg.edit_text("❌ 扫描任务已过期或无法找到原始查询。"); return
    cached_item = find_cached_query(original_query)
    if not cached_item: msg.edit_text("❌ 找不到结果文件的本地缓存记录。"); return
    msg.edit_text("1/3: 正在准备本地缓存文件...")
    cached_path = cached_item['cache']['file_path']
    temp_path = f"temp_{os.path.basename(cached_path)}"
    try: shutil.copy(cached_path, temp_path)
    except Exception as e: msg.edit_text(f"❌ 复制本地缓存文件失败: {e}"); return
    try: results = processor_func(temp_path, msg)
    finally:
        if os.path.exists(temp_path): os.remove(temp_path)
    if not results: msg.edit_text("🤷‍♀️ 扫描完成，但未发现任何存活的目标。"); return
    msg.edit_text("3/3: 正在打包并发送新结果...")
    output_filename = generate_filename_from_query(original_query, prefix=prefix)
    with open(output_filename, 'w', encoding='utf-8') as f: f.write("\n".join(sorted(list(results))))
    final_caption = final_message_func(len(results))
    with open(output_filename, 'rb') as doc: bot.send_document(chat_id, document=doc, caption=final_caption, parse_mode=ParseMode.MARKDOWN)
    os.remove(output_filename); msg.delete()
def process_liveness_check(file_path, msg):
    with open(file_path, 'r', encoding='utf-8') as f: targets = [line.strip() for line in f if line.strip()]
    live_results = set(); total = len(targets)
    msg.edit_text(f"2/3: 已加载 {total} 个目标，开始存活检测...")
    def check_port(target):
        try:
            ip, port_str = target.split(':'); port = int(port_str)
            with socket.create_connection((ip, port), timeout=SCAN_TIMEOUT) as sock: live_results.add(target)
        except (ValueError, socket.error): pass
    with ThreadPoolExecutor(max_workers=SCAN_CONCURRENCY) as executor: executor.map(check_port, targets)
    return live_results
def process_subnet_scan(file_path, msg):
    subnets_to_ports = {}
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            try:
                ip_str, port_str = line.strip().split(':'); port = int(port_str); subnet = ".".join(ip_str.split('.')[:3]) + ".0/24"
                if subnet not in subnets_to_ports: subnets_to_ports[subnet] = set()
                subnets_to_ports[subnet].add(port)
            except ValueError: continue
    if not subnets_to_ports: return set()
    total_targets = sum(len(ports) * 254 for ports in subnets_to_ports.values())
    if total_targets == 0: return set()
    msg.edit_text(f"2/3: 分析出 {len(subnets_to_ports)} 个/24子网，共计 {total_targets} 个扫描目标。开始扫描...")
    live_results = set(); completed_count = 0; last_update_time = time.time()
    def check_port(ip, port):
        try:
            with socket.create_connection((ip, port), timeout=SCAN_TIMEOUT) as sock: return f"{ip}:{port}"
        except socket.error: return None
    with ThreadPoolExecutor(max_workers=SCAN_CONCURRENCY) as executor:
        futures = []
        for subnet, ports in subnets_to_ports.items():
            base_ip = subnet.split('/')[0].rsplit('.', 1)[0]
            for i in range(1, 255):
                for port in ports: futures.append(executor.submit(check_port, f"{base_ip}.{i}", port))
        for future in as_completed(futures):
            completed_count += 1; result = future.result()
            if result: live_results.add(result)
            current_time = time.time()
            if current_time - last_update_time > 2.5:
                progress = (completed_count / total_targets) * 100
                try:
                    msg.edit_text(f"2/3: 扫描进度: {progress:.1f}% ({completed_count}/{total_targets})\n已发现: {len(live_results)} 个")
                    last_update_time = current_time
                except (BadRequest, RetryAfter): pass
    return live_results
def run_liveness_check_job(context: CallbackContext):
    download_and_process_file(context, context.job.context['query_hash'], prefix="live", processor_func=process_liveness_check, final_message_func=lambda count: f"✅ **存活检测完成!**\n\n共发现 *{count}* 个存活目标。")
def run_subnet_scan_job(context: CallbackContext):
    download_and_process_file(context, context.job.context['query_hash'], prefix="subnet_scan", processor_func=process_subnet_scan, final_message_func=lambda count: f"✅ **子网扫描完成!**\n\n在新IP中额外发现 *{count}* 个存活目标。")
def start_job(update: Update, context: CallbackContext, job_name_prefix, callback_func, query_hash):
    chat_id = update.effective_chat.id; msg = update.effective_message.reply_text("⏳ 任务已提交，准备开始...")
    job_context = {'chat_id': chat_id, 'msg': msg, 'query_hash': query_hash}
    context.job_queue.run_once(callback_func, 1, context=job_context, name=f"{job_name_prefix}_{chat_id}")
@admin_only
def liveness_check_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); query_hash = query.data.split('_', 1)[1]; start_job(update, context, "liveness", run_liveness_check_job, query_hash)
@admin_only
def subnet_scan_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); query_hash = query.data.split('_', 1)[1]; start_job(update, context, "subnet", run_subnet_scan_job, query_hash)
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
        data, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, query_text, page, 10000, "host"))
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
        cache_data = {'file_path': cache_path, 'result_count': len(unique_results)}
        add_or_update_query(query_text, cache_data); offer_post_download_actions(context, chat_id, query_text)
    elif not context.bot_data.get(stop_flag): msg.edit_text("🤷‍♀️ 任务完成，但未能下载到任何数据。")
    context.bot_data.pop(stop_flag, None)
def run_traceback_download_query(context: CallbackContext):
    job_data = context.job.context; bot, chat_id, base_query = context.bot, job_data['chat_id'], job_data['query']; output_filename = generate_filename_from_query(base_query)
    unique_results, page_count, last_page_date, termination_reason, stop_flag, last_update_time = set(), 0, None, "", f'stop_job_{chat_id}', 0
    msg = bot.send_message(chat_id, "⏳ 开始深度追溯下载...")
    current_query = base_query
    while True:
        page_count += 1
        if context.bot_data.get(stop_flag): termination_reason = "\n\n🌀 任务已手动停止."; break
        data, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, current_query, 1, 10000, "host,lastupdatetime"))
        if error: termination_reason = f"\n\n❌ 第 {page_count} 轮出错: {error}"; break
        results = data.get('results', [])
        if not results: termination_reason = "\n\nℹ️ 已获取所有查询结果."; break
        original_count = len(unique_results); unique_results.update([r[0] for r in results if r and r[0] and ':' in r[0]]); newly_added_count = len(unique_results) - original_count
        current_time = time.time()
        if current_time - last_update_time > 2:
            try:
                msg.edit_text(f"⏳ 已找到 {len(unique_results)} 条... (第 {page_count} 轮, 新增 {newly_added_count})")
                last_update_time = current_time
            except RetryAfter as e:
                logger.warning(f"Telegram flood control triggered. Waiting for {e.retry_after} seconds.")
                time.sleep(e.retry_after)
            except BadRequest: pass
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
    msg.edit_text("2/5: 正在确定更新起始点..."); data, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, base_query, fields="lastupdatetime"))
    if error or not data.get('results'): msg.edit_text(f"❌ 无法获取最新记录时间戳: {error or '无结果'}"); return
    ts_str = data['results'][0][0] if isinstance(data['results'][0], list) else data['results'][0]; cutoff_date = ts_str.split(' ')[0]
    incremental_query = f'({base_query}) && after="{cutoff_date}"'
    msg.edit_text(f"3/5: 正在侦察自 {cutoff_date} 以来的新数据..."); data, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, incremental_query, page_size=1))
    if error: msg.edit_text(f"❌ 侦察查询失败: {error}"); return
    total_new_size = data.get('size', 0)
    if total_new_size == 0: msg.edit_text("✅ 未发现新数据。缓存已是最新。"); return
    new_results, stop_flag = set(), f'stop_job_{chat_id}'; pages_to_fetch = (total_new_size + 9999) // 10000
    for page in range(1, pages_to_fetch + 1):
        if context.bot_data.get(stop_flag): msg.edit_text("🌀 增量更新已手动停止。"); return
        msg.edit_text(f"3/5: 正在下载新数据... ( Page {page}/{pages_to_fetch} )")
        data, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, incremental_query, page=page, page_size=10000))
        if error: msg.edit_text(f"❌ 下载新数据失败: {error}"); return
        if data.get('results'): new_results.update(res for res in data.get('results', []) if ':' in res)
    msg.edit_text(f"4/5: 正在合并数据... (发现 {len(new_results)} 条新数据)"); combined_results = sorted(list(new_results.union(old_results)))
    with open(old_file_path, 'w', encoding='utf-8') as f: f.write("\n".join(combined_results))
    msg.edit_text(f"5/5: 发送更新后的文件... (共 {len(combined_results)} 条)")
    with open(old_file_path, 'rb') as doc: bot.send_document(chat_id, document=doc, filename=os.path.basename(old_file_path))
    cache_data = {'file_path': old_file_path, 'result_count': len(combined_results)}
    add_or_update_query(base_query, cache_data)
    msg.delete(); bot.send_message(chat_id, f"✅ 增量更新完成！"); offer_post_download_actions(context, chat_id, base_query)

# --- 核心命令处理 ---
def start_command(update: Update, context: CallbackContext):
    update.message.reply_text('👋 欢迎使用 Fofa 查询机器人！请使用 /help 查看命令手册。')
    if not CONFIG['admins']: first_admin_id = update.effective_user.id; CONFIG.setdefault('admins', []).append(first_admin_id); save_config(); update.message.reply_text(f"ℹ️ 已自动将您 (ID: `{first_admin_id}`) 添加为第一个管理员。")
def help_command(update: Update, context: CallbackContext):
    help_text = ( "📖 *Fofa 机器人指令手册*\n\n"
                  "*🔍 资产查询*\n`/kkfofa [key] <query>` - FOFA搜索\n_不带参数则显示预设菜单_\n\n"
                  "*📦 主机详查*\n`/host <ip|domain>`\n_获取单个主机的详细聚合信息_\n\n"
                  "*📊 聚合统计*\n`/stats <query>` - 获取全局聚合统计\n\n"
                  "*📂 批量分析*\n`/batchfind` - 上传IP列表以分析共同特征\n\n"
                  "*⚙️ 管理与设置*\n`/settings` - 进入交互式设置菜单\n\n"
                  "*💾 高级功能*\n"
                  "`/backup` / `/restore` - 备份/恢复\n"
                  "`/history` - 查询历史\n"
                  "`/import` - 导入旧结果\n\n"
                  "*💻 系统管理*\n"
                  "`/update` - 在线更新脚本\n"
                  "`/getlog` - 获取日志\n"
                  "`/shutdown` - 安全关闭机器人\n\n"
                  "*🛑 任务控制*\n`/stop` - 紧急停止下载任务\n`/cancel` - 取消当前操作" )
    update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

# --- /kkfofa 查询流程 ---
def start_new_search(update: Update, context: CallbackContext, message_to_edit=None):
    query_text = context.user_data['query']; key_index = context.user_data.get('key_index'); add_or_update_query(query_text)
    msg = message_to_edit if message_to_edit else update.effective_message.reply_text("🔄 正在执行全新查询...")
    if message_to_edit: msg.edit_text("🔄 正在执行全新查询...")
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
        keyboard = [[InlineKeyboardButton("💎 全部下载 (前1万)", callback_data='mode_full'), InlineKeyboardButton("🌀 深度追溯下载", callback_data='mode_traceback')], [InlineKeyboardButton("❌ 取消", callback_data='mode_cancel')]]
        msg.edit_text(f"{success_message}\n请选择下载模式:", reply_markup=InlineKeyboardMarkup(keyboard)); return STATE_KKFOFA_MODE
def proceed_with_query(update: Update, context: CallbackContext, message_to_edit):
    query_text = context.user_data['query']
    cached_item = find_cached_query(query_text)
    if cached_item:
        dt_utc = datetime.fromisoformat(cached_item['timestamp']); dt_local = dt_utc.astimezone(tz.tzlocal()); time_str = dt_local.strftime('%Y-%m-%d %H:%M')
        message_text = (f"✅ *发现缓存*\n\n查询: `{escape_markdown(query_text)}`\n缓存于: *{time_str}*\n\n")
        keyboard = []; is_expired = (datetime.now(tz.tzutc()) - dt_utc).total_seconds() > CACHE_EXPIRATION_SECONDS
        if is_expired: message_text += "⚠️ *此缓存已过期，无法增量更新。*"; keyboard.append([InlineKeyboardButton("⬇️ 下载旧缓存", callback_data='cache_download'), InlineKeyboardButton("🔍 全新搜索", callback_data='cache_newsearch')])
        else: message_text += "请选择操作："; keyboard.append([InlineKeyboardButton("🔄 增量更新", callback_data='cache_incremental')]); keyboard.append([InlineKeyboardButton("⬇️ 下载缓存", callback_data='cache_download'), InlineKeyboardButton("🔍 全新搜索", callback_data='cache_newsearch')])
        keyboard.append([InlineKeyboardButton("❌ 取消", callback_data='cache_cancel')])
        message_to_edit.edit_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        return STATE_CACHE_CHOICE
    return start_new_search(update, context, message_to_edit=message_to_edit)

def kkfofa_entry(update: Update, context: CallbackContext):
    query_obj = update.callback_query
    message_obj = update.message
    
    if query_obj:
        query_obj.answer()
        try:
            preset_index = int(query_obj.data.replace("run_preset_", ""))
            preset = CONFIG["presets"][preset_index]
            query_text = preset['query']
            context.user_data['original_query'] = query_text
            context.user_data['key_index'] = None
            
            keyboard = [[InlineKeyboardButton("🌍 是的, 限定大洲", callback_data="continent_select"), InlineKeyboardButton("⏩ 不, 直接搜索", callback_data="continent_skip")]]
            query_obj.message.edit_text(
                f"预设查询: `{escape_markdown(query_text)}`\n\n是否要将此查询限定在特定大洲范围内？",
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN
            )
            return STATE_ASK_CONTINENT
        except (ValueError, IndexError):
            query_obj.message.edit_text("❌ 预设查询失败。")
            return ConversationHandler.END

    if not context.args:
        presets = CONFIG.get("presets", [])
        if not presets:
            message_obj.reply_text("欢迎使用FOFA查询机器人。\n\n➡️ 直接输入查询语法: `/kkfofa domain=\"example.com\"`\nℹ️ 当前没有可用的预设查询。管理员可通过 /settings 添加。")
            return ConversationHandler.END
        
        keyboard = []
        for i, p in enumerate(presets):
            query_preview = p['query']
            if len(query_preview) > 25: query_preview = query_preview[:25] + '...'
            button_text = f"{p['name']} (`{query_preview}`)"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"run_preset_{i}")])
        
        message_obj.reply_text("👇 请选择一个预设查询:", reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END
    
    key_index, query_text = None, " ".join(context.args)
    if context.args[0].isdigit():
        try:
            num = int(context.args[0])
            if 1 <= num <= len(CONFIG['apis']): key_index = num; query_text = " ".join(context.args[1:])
        except ValueError: pass
    
    context.user_data['original_query'] = query_text
    context.user_data['key_index'] = key_index
    
    keyboard = [[InlineKeyboardButton("🌍 是的, 限定大洲", callback_data="continent_select"), InlineKeyboardButton("⏩ 不, 直接搜索", callback_data="continent_skip")]]
    message_obj.reply_text(
        f"查询: `{escape_markdown(query_text)}`\n\n是否要将此查询限定在特定大洲范围内？",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN
    )
    return STATE_ASK_CONTINENT

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
    query = update.callback_query; query.answer(); continent = query.data.split('_', 1)[1]
    original_query = context.user_data['original_query']
    if continent == 'skip':
        context.user_data['query'] = original_query
        query.message.edit_text(f"好的，将直接搜索: `{escape_markdown(original_query)}`", parse_mode=ParseMode.MARKDOWN)
        return proceed_with_query(update, context, message_to_edit=query.message)
    country_list = CONTINENT_COUNTRIES.get(continent)
    if not country_list: query.message.edit_text("❌ 错误：无效的大洲选项。"); return ConversationHandler.END
    country_fofa_string = " || ".join([f'country="{code}"' for code in country_list])
    final_query = f"({original_query}) && ({country_fofa_string})"
    context.user_data['query'] = final_query
    query.message.edit_text(f"查询已构建:\n`{escape_markdown(final_query)}`\n\n正在处理...", parse_mode=ParseMode.MARKDOWN)
    return proceed_with_query(update, context, message_to_edit=query.message)
def cache_choice_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); choice = query.data.split('_')[1]
    if choice == 'download':
        cached_item = find_cached_query(context.user_data['query'])
        if cached_item:
            query.message.edit_text("⬇️ 正在从本地缓存发送文件...");
            file_path = cached_item['cache']['file_path']
            try:
                with open(file_path, 'rb') as doc:
                    context.bot.send_document(chat_id=update.effective_chat.id, document=doc, filename=os.path.basename(file_path))
                query.message.delete()
            except Exception as e: query.message.edit_text(f"❌ 发送缓存失败: {e}")
        else: query.message.edit_text("❌ 找不到本地缓存记录。")
        return ConversationHandler.END
    elif choice == 'newsearch': return start_new_search(update, context, message_to_edit=query.message)
    elif choice == 'incremental': query.edit_message_text("⏳ 准备增量更新..."); start_download_job(context, run_incremental_update_query, context.user_data); query.message.delete(); return ConversationHandler.END
    elif choice == 'cancel': query.message.edit_text("操作已取消。"); return ConversationHandler.END
def query_mode_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); mode = query.data.split('_')[1]
    if mode == 'full': query.message.edit_text(f"⏳ 开始全量下载..."); start_download_job(context, run_full_download_query, context.user_data); query.message.delete()
    elif mode == 'traceback': query.message.edit_text(f"⏳ 开始深度追溯下载..."); start_download_job(context, run_traceback_download_query, context.user_data); query.message.delete()
    elif mode == 'cancel': query.message.edit_text("操作已取消。")
    return ConversationHandler.END

# --- /batchfind 批量特征分析 ---
@admin_only
def batchfind_command(update: Update, context: CallbackContext) -> int:
    update.message.reply_text("请上传一个包含 `ip:port` 列表的 `.txt` 文件 (每行一个)。\n\n我将提取前100行进行批量特征分析。")
    return STATE_GET_BATCH_FILE

def build_feature_keyboard(selected_features=None):
    if selected_features is None: selected_features = set()
    keyboard = []
    features = list(BATCH_FEATURES.items())
    for i in range(0, len(features), 2):
        row = []
        key, name = features[i]
        text = f"{'✅ ' if key in selected_features else ''}{name}"
        row.append(InlineKeyboardButton(text, callback_data=f"batchfeature_{key}"))
        if i + 1 < len(features):
            key, name = features[i+1]
            text = f"{'✅ ' if key in selected_features else ''}{name}"
            row.append(InlineKeyboardButton(text, callback_data=f"batchfeature_{key}"))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🚀 开始分析", callback_data="batchfeature_done")])
    keyboard.append([InlineKeyboardButton("❌ 取消", callback_data="batchfeature_cancel")])
    return InlineKeyboardMarkup(keyboard)

@admin_only
def get_batch_file_handler(update: Update, context: CallbackContext) -> int:
    doc = update.message.document
    if not doc.file_name.lower().endswith('.txt'):
        update.message.reply_text("❌ 文件格式错误，请上传 `.txt` 文件。")
        return ConversationHandler.END

    msg = update.message.reply_text("正在下载并解析文件...")
    try:
        file = doc.get_file()
        temp_path = os.path.join(FOFA_CACHE_DIR, f"batch_{doc.file_id}.txt")
        file.download(temp_path)
        context.user_data['batch_file_path'] = temp_path
        context.user_data['selected_features'] = set()
        
        targets = []
        # Regex to find an IP:PORT at the beginning of a line, ignoring surrounding whitespace
        ip_port_pattern = re.compile(r"^\s*(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{1,5})")
        with open(temp_path, 'r', encoding='utf-8') as f:
            for line in f:
                if len(targets) >= 100:
                    break
                match = ip_port_pattern.match(line)
                if match:
                    targets.append(match.group(1))
        
        if not targets:
            msg.edit_text("❌ 文件为空或无法从中提取任何 `ip:port` 格式的数据，操作已取消。")
            os.remove(temp_path)
            return ConversationHandler.END

        context.user_data['targets'] = targets
        msg.edit_text(f"✅ 文件处理成功，共提取 {len(targets)} 个目标。\n\n请选择您想分析的特征 (可多选):",
                      reply_markup=build_feature_keyboard())
        return STATE_SELECT_BATCH_FEATURES

    except Exception as e:
        logger.error(f"处理批量文件失败: {e}")
        msg.edit_text(f"❌ 处理文件时出错: {e}")
        return ConversationHandler.END

@admin_only
def select_batch_features_callback(update: Update, context: CallbackContext) -> int:
    query = update.callback_query; query.answer(); action = query.data.split('_', 1)[1]

    if action == 'cancel':
        query.message.edit_text("操作已取消。")
        if 'batch_file_path' in context.user_data and os.path.exists(context.user_data['batch_file_path']):
            os.remove(context.user_data['batch_file_path'])
        return ConversationHandler.END

    if action == 'done':
        if not context.user_data.get('selected_features'):
            query.answer("⚠️ 请至少选择一个要分析的特征!", show_alert=True)
            return STATE_SELECT_BATCH_FEATURES
        
        query.message.edit_text("⏳ 任务已提交到后台，正在批量查询和分析...")
        job_data = {
            'chat_id': update.effective_chat.id,
            'targets': context.user_data['targets'],
            'selected_features': list(context.user_data['selected_features']),
            'file_path': context.user_data['batch_file_path'],
            'msg_id': query.message.message_id
        }
        context.job_queue.run_once(run_batch_find_job, 1, context=job_data)
        return ConversationHandler.END

    selected_features = context.user_data.get('selected_features', set())
    if action in selected_features: selected_features.remove(action)
    else: selected_features.add(action)
    context.user_data['selected_features'] = selected_features
    query.message.edit_reply_markup(reply_markup=build_feature_keyboard(selected_features))
    return STATE_SELECT_BATCH_FEATURES

def run_batch_find_job(context: CallbackContext):
    job_context = context.job.context
    chat_id, targets, selected_features, file_path, msg_id = job_context['chat_id'], job_context['targets'], job_context['selected_features'], job_context['file_path'], job_context['msg_id']
    bot = context.bot; feature_analysis = {feature: {} for feature in selected_features}
    
    fields_to_fetch_set = set(selected_features)
    fields_to_fetch_set.update(["ip", "port", "banner", "header"])
    fields_to_fetch = ",".join(list(fields_to_fetch_set))
    
    total_targets = len(targets); completed_count = 0; last_update_time = time.time()

    def fetch_single_target(target):
        nonlocal completed_count, last_update_time
        try:
            try: ip, port = target.rsplit(':', 1)
            except ValueError: logger.warning(f"Skipping malformed target in batchfind: {target}"); return None
            query_text = f'ip="{ip}" && port="{port}"'
            data, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, query_text, page_size=1, fields=fields_to_fetch))
            if not error and data and data.get('results'): return data['results'][0]
            return None
        finally:
            completed_count += 1; current_time = time.time()
            if current_time - last_update_time > 2.5:
                try:
                    progress_percent = (completed_count / total_targets) * 100
                    progress_bar = create_progress_bar(progress_percent)
                    bot.edit_message_text(f"⏳ 正在分析...\n`{progress_bar}`", chat_id=chat_id, message_id=msg_id, parse_mode=ParseMode.MARKDOWN)
                    last_update_time = current_time
                except (BadRequest, RetryAfter): pass

    with ThreadPoolExecutor(max_workers=20) as executor:
        all_results = list(executor.map(fetch_single_target, targets))

    field_map = {name: idx for idx, name in enumerate(fields_to_fetch.split(','))}
    
    success_count = 0
    for result in all_results:
        if result is None: continue
        success_count += 1
        for feature in selected_features:
            value = result[field_map[feature]]
            if value is not None and value != '':
                if feature in ['banner', 'header']:
                    fingerprint = normalize_banner(value)
                    if not fingerprint: continue
                    if fingerprint in feature_analysis[feature]: feature_analysis[feature][fingerprint]['count'] += 1
                    else: feature_analysis[feature][fingerprint] = {'count': 1, 'example': value}
                else:
                    feature_analysis[feature].setdefault(value, 0); feature_analysis[feature][value] += 1
    
    report_lines = [f"📊 *批量特征分析报告*"]
    report_lines.append("\n--- *查询概览* ---")
    report_lines.append(f"*   总目标数:* `{total_targets}`")
    report_lines.append(f"*   成功找到:* `{success_count}`")
    report_lines.append(f"*   未找到数据:* `{total_targets - success_count}`")
    if success_count > 0:
        report_lines.append(f"\n*（注意：以下特征分析仅基于成功找到的 {success_count} 个目标）*")
    report_lines.append("")

    if success_count > 0:
        for feature, counts in feature_analysis.items():
            feature_name = BATCH_FEATURES.get(feature, feature)
            report_lines.append(f"--- *Top 5 {feature_name}* ---")
            if not counts: report_lines.append("_未发现该特征的数据_")
            else:
                if feature in ['banner', 'header']:
                    sorted_items = sorted(counts.values(), key=lambda item: item['count'], reverse=True)
                    for item in sorted_items[:5]:
                        count = item['count']; display_value = (item['example'][:70] + '...') if len(item['example']) > 70 else item['example']
                        report_lines.append(f"`{escape_markdown(display_value)}`: *{count}*")
                else:
                    sorted_items = sorted(counts.items(), key=lambda item: item[1], reverse=True)
                    for value, count in sorted_items[:5]:
                        display_value = (str(value)[:70] + '...') if len(str(value)) > 70 else value
                        report_lines.append(f"`{escape_markdown(display_value)}`: *{count}*")
            report_lines.append("")

        dominant_query_parts = []
        query_builder_features = ["protocol", "os", "server", "cert.issuer.cn", "cert.subject.org", "domain", "icp"]
        threshold = success_count / 2

        for feature in query_builder_features:
            if feature in feature_analysis and feature_analysis[feature]:
                counts = feature_analysis[feature]
                top_item = max(counts.items(), key=lambda item: item[1])
                top_value, top_count = top_item
                if top_count >= threshold:
                    dominant_query_parts.append(f'{feature}="{top_value}"' if " " in str(top_value) else f'{feature}={top_value}')

        if dominant_query_parts:
            suggested_query = " && ".join(dominant_query_parts)
            report_lines.append("--- *💡 建议的FOFA查询* ---")
            report_lines.append("根据分析，以下查询可覆盖大部分*已找到*的目标:")
            report_lines.append(f"`{escape_markdown(suggested_query)}`")
            report_lines.append("")

    final_report = "\n".join(report_lines)
    if len(final_report) > 4096: final_report = final_report[:4090] + "\n...内容过长已截断..."
    bot.edit_message_text(final_report, chat_id=chat_id, message_id=msg_id, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    if os.path.exists(file_path): os.remove(file_path)

# --- 其他管理命令 ---
@admin_only
def stop_all_tasks(update: Update, context: CallbackContext): context.bot_data[f'stop_job_{update.effective_chat.id}'] = True; update.message.reply_text("✅ 已发送停止信号。")
@admin_only
def backup_config_command(update: Update, context: CallbackContext):
    if os.path.exists(CONFIG_FILE): update.effective_chat.send_document(document=open(CONFIG_FILE, 'rb'))
    else: update.effective_chat.send_message("❌ 找不到配置文件。")

@admin_only
def restore_config_command(update: Update, context: CallbackContext) -> int:
    update.message.reply_text("📥 请上传您的 `config.json` 文件以恢复配置。\n\n随时可以发送 /cancel 来取消。")
    return STATE_GET_RESTORE_FILE

@admin_only
def receive_config_file(update: Update, context: CallbackContext) -> int:
    global CONFIG
    if not update.message.document or update.message.document.file_name != CONFIG_FILE:
        update.message.reply_text(f"❌ 操作失败，必须上传一个名为 `{CONFIG_FILE}` 的文件。")
        return ConversationHandler.END
    
    try:
        file = update.message.document.get_file(); temp_path = f"{CONFIG_FILE}.tmp"; file.download(temp_path)
        with open(temp_path, 'r', encoding='utf-8') as f: json.load(f)
        os.replace(temp_path, CONFIG_FILE); CONFIG = load_json_file(CONFIG_FILE, DEFAULT_CONFIG)
        update.message.reply_text("✅ 配置已成功恢复！机器人功能可能需要重启后完全生效。")
    except Exception as e:
        logger.error(f"恢复配置失败: {e}"); update.message.reply_text(f"❌ 恢复失败: {e}")
        if os.path.exists(temp_path): os.remove(temp_path)
    
    return ConversationHandler.END

@admin_only
def history_command(update: Update, context: CallbackContext):
    if not HISTORY['queries']: update.message.reply_text("🕰️ 暂无历史记录。"); return
    message_text = "🕰️ *最近10条查询记录:*\n\n"
    for i, query_hist in enumerate(HISTORY['queries'][:10]):
        dt_utc = datetime.fromisoformat(query_hist['timestamp']); dt_local = dt_utc.astimezone(tz.tzlocal()); time_str = dt_local.strftime('%Y-%m-%d %H:%M'); cache_icon = "✅" if query_hist.get('cache') else "❌"
        message_text += f"`{i+1}.` {escape_markdown(query_hist['query_text'])}\n_{time_str}_  (缓存: {cache_icon})\n\n"
    update.message.reply_text(message_text, parse_mode=ParseMode.MARKDOWN)
@admin_only
def import_command(update: Update, context: CallbackContext):
    if not update.message.reply_to_message or not update.message.reply_to_message.document: update.message.reply_text("❌ *用法错误*\n请*回复 (Reply)*一个您想导入的`.txt`文件，再输入此命令。", parse_mode=ParseMode.MARKDOWN); return
    context.user_data['import_doc'] = update.message.reply_to_message.document; update.message.reply_text("好的，已收到文件。\n现在请输入与此文件关联的 *FOFA 查询语句*：", parse_mode=ParseMode.MARKDOWN); return STATE_GET_IMPORT_QUERY
def get_import_query(update: Update, context: CallbackContext):
    doc = context.user_data.get('import_doc'); query_text = update.message.text.strip()
    if not doc or not query_text: update.message.reply_text("❌ 操作已过时或查询为空。"); return ConversationHandler.END
    cache_path = os.path.join(FOFA_CACHE_DIR, f"imported_{doc.file_name}_{int(time.time())}.txt")
    msg = update.message.reply_text("正在下载并保存导入文件到本地缓存...")
    try:
        file = doc.get_file(); file.download(cache_path)
        with open(cache_path, 'r', encoding='utf-8') as f: counted_lines = sum(1 for line in f if line.strip())
        cache_data = {'file_path': cache_path, 'result_count': counted_lines}
        add_or_update_query(query_text, cache_data)
        msg.edit_text(f"✅ *导入成功！*\n\n查询 `{escape_markdown(query_text)}` 已成功关联本地缓存，共 *{counted_lines}* 条结果。", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"导入文件失败: {e}"); msg.edit_text(f"❌ 导入失败: {e}")
        if os.path.exists(cache_path): os.remove(cache_path)
    context.user_data.clear(); return ConversationHandler.END
@admin_only
def get_log_command(update: Update, context: CallbackContext):
    if os.path.exists(LOG_FILE): update.message.reply_document(document=open(LOG_FILE, 'rb'))
    else: update.message.reply_text("❌ 未找到日志文件。")
@admin_only
def shutdown_command(update: Update, context: CallbackContext):
    update.message.reply_text("✅ 收到指令！机器人正在平稳关闭..."); logger.info(f"接收到来自用户 {update.effective_user.id} 的关闭指令。")
    context.job_queue.run_once(lambda _: os.kill(os.getpid(), signal.SIGINT), 1)
@admin_only
def update_script_command(update: Update, context: CallbackContext, from_menu=False):
    url = CONFIG.get("update_url")
    if not url:
        msg_target = update.callback_query.message if from_menu else update.message
        msg_target.reply_text("❌ 未在配置中设置更新URL。\n请在 /settings -> 脚本更新 中设置。")
        return
    msg = update.callback_query.message if from_menu else update.message.reply_text("⏳ 正在从配置的URL检查更新...")
    if from_menu: msg.edit_text("⏳ 正在从配置的URL检查更新...")
    try:
        response = requests.get(url, timeout=30, proxies=get_proxies()); response.raise_for_status()
        new_script_content = response.text
    except requests.exceptions.RequestException as e: msg.edit_text(f"❌ 下载更新失败: {e}"); return
    if 'if __name__ == "__main__":' not in new_script_content or 'Updater(' not in new_script_content:
        msg.edit_text("❌ 下载的文件似乎不是一个有效的机器人脚本，已中止更新。"); return
    script_path = os.path.abspath(sys.argv[0]); temp_path = script_path + ".new"
    try:
        with open(temp_path, 'w', encoding='utf-8') as f: f.write(new_script_content)
    except IOError as e: msg.edit_text(f"❌ 无法写入临时文件: {e}"); return
    try: os.replace(temp_path, script_path)
    except OSError as e:
        msg.edit_text(f"❌ 替换脚本文件失败: {e}")
        if os.path.exists(temp_path): os.remove(temp_path)
        return
    msg.edit_text("✅ 更新成功！机器人将在2秒后重启以应用新版本...")
    logger.info(f"脚本已由用户 {update.effective_user.id} 更新。正在重启...")
    def restart(context: CallbackContext): os.execv(sys.executable, [sys.executable] + sys.argv)
    context.job_queue.run_once(restart, 2)

# --- 设置菜单 (Settings Conversation) ---
@admin_only
def settings_command(update: Update, context: CallbackContext):
    keyboard = [
        [InlineKeyboardButton("🔑 API 管理", callback_data='settings_api')],
        [InlineKeyboardButton("✨ 预设管理", callback_data='settings_preset')],
        [InlineKeyboardButton("🌐 代理设置", callback_data='settings_proxy')],
        [InlineKeyboardButton("💾 备份与恢复", callback_data='settings_backup')],
        [InlineKeyboardButton("🔄 脚本更新", callback_data='settings_update')],
        [InlineKeyboardButton("❌ 关闭菜单", callback_data='settings_close')]
    ]
    message_text = "⚙️ *设置菜单*"; reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query: update.callback_query.message.edit_text(message_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    else: update.message.reply_text(message_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    return STATE_SETTINGS_MAIN
def settings_callback_handler(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); menu = query.data.split('_', 1)[1]
    if menu == 'api': return show_api_menu(update, context)
    if menu == 'proxy': return show_proxy_menu(update, context)
    if menu == 'backup': return show_backup_restore_menu(update, context)
    if menu == 'preset': return show_preset_menu(update, context)
    if menu == 'update': return show_update_menu(update, context)
    if menu == 'close': query.message.edit_text("菜单已关闭."); return ConversationHandler.END
    return STATE_SETTINGS_ACTION
def show_api_menu(update: Update, context: CallbackContext):
    msg = update.callback_query.message
    msg.edit_text("🔄 正在查询API Key状态...")
    api_details = []
    for i, key in enumerate(CONFIG['apis']):
        data, error = verify_fofa_api(key)
        key_masked = f"`...{key[-4:]}`"
        if error: status = f"❌ *无效*: {error}"
        else:
            username = escape_markdown(data.get('username', 'N/A')); vip_level = data.get('vip_level', 0)
            vip_status = f"👑 VIP L{vip_level}" if data.get('is_vip') else "👤 普通"
            f_points = data.get('fofa_point', 0); free_points = data.get('remain_free_point', 0)
            status = f"{vip_status} ({username}) | F点: *{f_points}*, 免费点: *{free_points}*"
        api_details.append(f"`#{i+1}` {key_masked}\n  {status}")
    api_message = "\n\n".join(api_details) if api_details else "_无_"
    keyboard = [[InlineKeyboardButton(f"查询范围: {'✅ 完整历史' if CONFIG.get('full_mode') else '⏳ 近一年'}", callback_data='action_toggle_full')], [InlineKeyboardButton("➕ 添加Key", callback_data='action_add_api'), InlineKeyboardButton("➖ 删除Key", callback_data='action_remove_api')], [InlineKeyboardButton("🔙 返回", callback_data='action_back_main')]]
    msg.edit_text(f"🔑 *API 管理*\n\n{api_message}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return STATE_SETTINGS_ACTION
def show_proxy_menu(update: Update, context: CallbackContext):
    keyboard = [[InlineKeyboardButton("✏️ 设置/更新", callback_data='action_set_proxy')], [InlineKeyboardButton("🗑️ 清除", callback_data='action_delete_proxy')], [InlineKeyboardButton("🔙 返回", callback_data='action_back_main')]]
    update.callback_query.message.edit_text(f"🌐 *代理设置*\n当前: `{CONFIG.get('proxy') or '未设置'}`", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN); return STATE_SETTINGS_ACTION
def show_backup_restore_menu(update: Update, context: CallbackContext):
    message_text = ("💾 *备份与恢复*\n\n📤 *备份*\n点击下方按钮，或使用 /backup 命令。\n\n📥 *恢复*\n使用 /restore 命令，然后按提示上传文件。"); keyboard = [[InlineKeyboardButton("📤 立即备份", callback_data='action_backup_now')], [InlineKeyboardButton("🔙 返回", callback_data='action_back_main')]]
    update.callback_query.message.edit_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN); return STATE_SETTINGS_ACTION
def show_update_menu(update: Update, context: CallbackContext):
    current_url = CONFIG.get("update_url") or "未设置"
    message_text = f"🔄 *脚本更新*\n\n此功能允许机器人从指定的URL下载最新脚本并自动重启。\n\n*当前更新源 URL:*\n`{escape_markdown(current_url)}`"
    keyboard = [
        [InlineKeyboardButton("✏️ 设置/更新 URL", callback_data='action_set_update_url')],
        [InlineKeyboardButton("🚀 立即更新", callback_data='action_run_update')],
        [InlineKeyboardButton("🔙 返回", callback_data='action_back_main')]
    ]
    update.callback_query.message.edit_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return STATE_SETTINGS_ACTION
def settings_action_handler(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); action = query.data.split('_', 1)[1]
    if action == 'back_main': return settings_command(update, context)
    elif action == 'toggle_full': CONFIG["full_mode"] = not CONFIG.get("full_mode", False); save_config(); return show_api_menu(update, context)
    elif action == 'add_api': query.message.edit_text("请发送您的 Fofa API Key。"); return STATE_GET_KEY
    elif action == 'remove_api': query.message.edit_text("请输入要删除的API Key编号(#)。"); return STATE_REMOVE_API
    elif action == 'set_proxy': query.message.edit_text("请输入代理地址 (例如 http://127.0.0.1:7890)。"); return STATE_GET_PROXY
    elif action == 'delete_proxy': CONFIG['proxy'] = ""; save_config(); query.answer("代理已清除"); return show_proxy_menu(update, context)
    elif action == 'backup_now': backup_config_command(update, context); return STATE_SETTINGS_ACTION
    elif action == 'set_update_url': query.message.edit_text("请发送新的脚本更新URL (必须是可直接访问的 raw 文件链接)。"); return STATE_GET_UPDATE_URL
    elif action == 'run_update': update_script_command(query, context, from_menu=True); return ConversationHandler.END
    return STATE_SETTINGS_ACTION
def get_key(update: Update, context: CallbackContext):
    key = update.message.text.strip(); msg = update.message.reply_text("正在验证...")
    data, error = verify_fofa_api(key)
    if not error: CONFIG['apis'].append(key); save_config(); msg.edit_text(f"✅ 添加成功！")
    else: msg.edit_text(f"❌ 验证失败: {error}")
    return settings_command(update, context)
def get_proxy(update: Update, context: CallbackContext):
    CONFIG['proxy'] = update.message.text.strip(); save_config(); update.message.reply_text(f"✅ 代理已更新。"); return settings_command(update, context)
def remove_api(update: Update, context: CallbackContext):
    try:
        index = int(update.message.text.strip()) - 1
        if 0 <= index < len(CONFIG['apis']): CONFIG['apis'].pop(index); save_config(); update.message.reply_text(f"✅ Key已删除。")
        else: update.message.reply_text("❌ 无效编号。")
    except (ValueError, IndexError): update.message.reply_text("❌ 请输入数字。")
    return settings_command(update, context)
def get_update_url(update: Update, context: CallbackContext):
    url = update.message.text.strip()
    if url.startswith("http://") or url.startswith("https://"):
        CONFIG['update_url'] = url; save_config(); update.message.reply_text("✅ 更新URL已设置。")
    else: update.message.reply_text("❌ URL格式无效，请输入以 http:// 或 https:// 开头的链接。")
    return settings_command(update, context)
def show_preset_menu(update: Update, context: CallbackContext):
    preset_list = "\n".join([f"`#{i+1}`: `{p['name']}`" for i, p in enumerate(CONFIG['presets'])]) or "_无_"
    text = f"✨ *预设管理*\n\n{preset_list}"; kbd = [[InlineKeyboardButton("➕ 添加", callback_data='preset_add'), InlineKeyboardButton("➖ 移除", callback_data='preset_remove')], [InlineKeyboardButton("🔙 返回", callback_data='preset_back')]]
    update.callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kbd), parse_mode=ParseMode.MARKDOWN); return STATE_PRESET_MENU
def preset_menu_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); action = query.data.split('_', 1)[1]
    if action == 'back': return settings_command(update, context)
    if action == 'add': query.message.edit_text("请输入预设的名称 (例如: 海康威视摄像头):"); return STATE_GET_PRESET_NAME
    if action == 'remove': query.message.edit_text("请输入要移除的预设的编号(#):"); return STATE_REMOVE_PRESET
    return STATE_PRESET_MENU
def get_preset_name(update: Update, context: CallbackContext):
    context.user_data['preset_name'] = update.message.text.strip(); update.message.reply_text(f"名称: `{context.user_data['preset_name']}`\n\n现在请输入完整的FOFA查询语法:"); return STATE_GET_PRESET_QUERY
def get_preset_query(update: Update, context: CallbackContext):
    new_preset = {"name": context.user_data['preset_name'], "query": update.message.text.strip()}; CONFIG['presets'].append(new_preset); save_config()
    update.message.reply_text("✅ 预设添加成功！"); context.user_data.clear(); return settings_command(update, context)
def remove_preset(update: Update, context: CallbackContext):
    try:
        idx = int(update.message.text.strip()) - 1
        if 0 <= idx < len(CONFIG['presets']): CONFIG['presets'].pop(idx); save_config(); update.message.reply_text("✅ 预设已移除。")
        else: update.message.reply_text("❌ 无效的编号。")
    except ValueError: update.message.reply_text("❌ 请输入数字编号。")
    return settings_command(update, context)
def cancel(update: Update, context: CallbackContext):
    if update.callback_query: update.callback_query.message.edit_text('操作已取消。')
    elif update.message: update.message.reply_text('操作已取消。')
    context.user_data.clear(); return ConversationHandler.END

# --- 主函数与调度器 ---
def main() -> None:
    os.makedirs(FOFA_CACHE_DIR, exist_ok=True)
    bot_token = CONFIG.get("bot_token")
    if not bot_token or bot_token == "YOUR_BOT_TOKEN_HERE":
        logger.critical("严重错误：config.json 中的 'bot_token' 未设置！")
        if not os.path.exists(CONFIG_FILE): save_config()
        return

    updater = Updater(token=bot_token, use_context=True)
    dispatcher = updater.dispatcher
    commands = [
        BotCommand("start", "🚀 启动与帮助"), BotCommand("help", "❓ 命令手册"),
        BotCommand("kkfofa", "🔍 资产搜索/预设"), BotCommand("host", "📦 主机详查"),
        BotCommand("stats", "📊 全局聚合统计"), BotCommand("batchfind", "📂 批量特征分析"),
        BotCommand("settings", "⚙️ 设置菜单"), BotCommand("history", "🕰️ 查询历史"),
        BotCommand("import", "🖇️ 导入旧缓存"), BotCommand("backup", "📤 备份配置"),
        BotCommand("restore", "📥 恢复配置"), BotCommand("update", "🔄 在线更新脚本"),
        BotCommand("getlog", "📄 获取日志"), BotCommand("shutdown", "🔌 关闭机器人"),
        BotCommand("stop", "🛑 停止任务"), BotCommand("cancel", "❌ 取消操作")
    ]
    try: updater.bot.set_my_commands(commands)
    except Exception as e: logger.warning(f"设置机器人命令失败: {e}")

    settings_conv = ConversationHandler(
        entry_points=[CommandHandler("settings", settings_command)],
        states={
            STATE_SETTINGS_MAIN: [CallbackQueryHandler(settings_callback_handler, pattern=r"^settings_")],
            STATE_SETTINGS_ACTION: [CallbackQueryHandler(settings_action_handler, pattern=r"^action_")],
            STATE_GET_KEY: [MessageHandler(Filters.text & ~Filters.command, get_key)],
            STATE_GET_PROXY: [MessageHandler(Filters.text & ~Filters.command, get_proxy)],
            STATE_REMOVE_API: [MessageHandler(Filters.text & ~Filters.command, remove_api)],
            STATE_PRESET_MENU: [CallbackQueryHandler(preset_menu_callback, pattern=r"^preset_")],
            STATE_GET_PRESET_NAME: [MessageHandler(Filters.text & ~Filters.command, get_preset_name)],
            STATE_GET_PRESET_QUERY: [MessageHandler(Filters.text & ~Filters.command, get_preset_query)],
            STATE_REMOVE_PRESET: [MessageHandler(Filters.text & ~Filters.command, remove_preset)],
            STATE_GET_UPDATE_URL: [MessageHandler(Filters.text & ~Filters.command, get_update_url)],
        },
        fallbacks=[CommandHandler("cancel", cancel)], conversation_timeout=300
    )
    kkfofa_conv = ConversationHandler(
        entry_points=[ CommandHandler("kkfofa", kkfofa_entry), CallbackQueryHandler(kkfofa_entry, pattern=r"^run_preset_") ],
        states={
            STATE_ASK_CONTINENT: [CallbackQueryHandler(ask_continent_callback, pattern=r"^continent_")],
            STATE_CONTINENT_CHOICE: [CallbackQueryHandler(continent_choice_callback, pattern=r"^continent_")],
            STATE_CACHE_CHOICE: [CallbackQueryHandler(cache_choice_callback, pattern=r"^cache_")],
            STATE_KKFOFA_MODE: [CallbackQueryHandler(query_mode_callback, pattern=r"^mode_")],
        },
        fallbacks=[CommandHandler("cancel", cancel)], conversation_timeout=300
    )
    import_conv = ConversationHandler(entry_points=[CommandHandler("import", import_command)], states={STATE_GET_IMPORT_QUERY: [MessageHandler(Filters.text & ~Filters.command, get_import_query)]}, fallbacks=[CommandHandler("cancel", cancel)], conversation_timeout=300)
    stats_conv = ConversationHandler(entry_points=[CommandHandler("stats", stats_command)], states={STATE_GET_STATS_QUERY: [MessageHandler(Filters.text & ~Filters.command, get_fofa_stats_query)]}, fallbacks=[CommandHandler("cancel", cancel)], conversation_timeout=300)
    
    batchfind_conv = ConversationHandler(
        entry_points=[CommandHandler("batchfind", batchfind_command)],
        states={
            STATE_GET_BATCH_FILE: [MessageHandler(Filters.document.mime_type("text/plain"), get_batch_file_handler)],
            STATE_SELECT_BATCH_FEATURES: [CallbackQueryHandler(select_batch_features_callback, pattern=r"^batchfeature_")]
        },
        fallbacks=[CommandHandler("cancel", cancel)], conversation_timeout=300
    )
    restore_conv = ConversationHandler(
        entry_points=[CommandHandler("restore", restore_config_command)],
        states={
            STATE_GET_RESTORE_FILE: [MessageHandler(Filters.document, receive_config_file)]
        },
        fallbacks=[CommandHandler("cancel", cancel)], conversation_timeout=300
    )

    dispatcher.add_handler(CommandHandler("start", start_command))
    dispatcher.add_handler(CommandHandler("help", help_command))
    dispatcher.add_handler(CommandHandler("host", host_command))
    dispatcher.add_handler(CommandHandler("stop", stop_all_tasks))
    dispatcher.add_handler(CommandHandler("backup", backup_config_command))
    dispatcher.add_handler(CommandHandler("history", history_command))
    dispatcher.add_handler(CommandHandler("getlog", get_log_command))
    dispatcher.add_handler(CommandHandler("shutdown", shutdown_command))
    dispatcher.add_handler(CommandHandler("update", update_script_command))
    dispatcher.add_handler(CallbackQueryHandler(liveness_check_callback, pattern=r'^liveness_'))
    dispatcher.add_handler(CallbackQueryHandler(subnet_scan_callback, pattern=r'^subnet_'))
    
    dispatcher.add_handler(settings_conv)
    dispatcher.add_handler(kkfofa_conv)
    dispatcher.add_handler(import_conv)
    dispatcher.add_handler(stats_conv)
    dispatcher.add_handler(batchfind_conv)
    dispatcher.add_handler(restore_conv)

    logger.info(f"🚀 终极版机器人已启动 (v8.6 - 兼容性与报告优化)...")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
