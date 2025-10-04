#
# fofa_ultimate_v13_fixed.py (最终融合版 for python-telegram-bot v13.x)
#
# 继承了 fofa.py 的高级缓存、增量更新、深度追溯、任务控制等专业功能。
# 继承了 go.py 的 /kkfofa 预设菜单、/stats 全球统计和图形化预设管理。
# 新增: 1. 根据查询语法生成智能文件名。
# 新增: 2. 集成Go语言的TCP存活检测工具，可通过下载任务或 /getscanner 命令获取。
# 修复: 修复了 kkfofa_command 中的 IndentationError。
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
from dateutil import tz # <-- 依赖: pip install python-dateutil

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

# --- Go 扫描工具模板 ---
# TCP 端口活性测试模板
TCP_ACTIVE_GO_TEMPLATE_LINES = [
    "package main",
    "import (",
    "	\"bufio\"",
    "	\"fmt\"",
    "	\"net\"",
    "	\"os\"",
    "	\"strings\"",
    "	\"sync\"",
    "	\"time\"",
    ")",
    "func worker(tasks <-chan string, resultChan chan<- string, wg *sync.WaitGroup) {",
    "	defer wg.Done()",
    "	for line := range tasks {",
    "		ipPort := strings.TrimSpace(line)",
    "		if _, _, err := net.SplitHostPort(ipPort); err != nil { continue }",
    "		conn, err := net.DialTimeout(\"tcp\", ipPort, 3*time.Second)",
    "		if err == nil {",
    "			conn.Close()",
    "			resultChan <- ipPort",
    "		}",
    "	}",
    "}",
    "func writer(resultChan <-chan string, file *os.File, doneChan chan<- bool) {",
    "    for result := range resultChan {",
    "        _, err := file.WriteString(result + \"\\n\")",
    "        if err != nil {",
    "            fmt.Printf(\"Error writing to file: %s\\n\", err)",
    "        }",
    "    }",
    "    doneChan <- true",
    "}",
    "func main() {",
    "	if len(os.Args) < 3 {",
    "       fmt.Println(\"Usage: ./checker <inputFile> <outputFile>\")",
    "       os.Exit(1)",
    "   }",
    "	inputFile, outputFile := os.Args[1], os.Args[2]",
    "	batch, err := os.Open(inputFile)",
    "	if err != nil { fmt.Printf(\"Error opening input file: %s\\n\", err); return }",
    "	defer batch.Close()",
    "	outFile, err := os.OpenFile(outputFile, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)",
    "	if err != nil { fmt.Printf(\"Error opening output file: %s\\n\", err); return }",
    "	defer outFile.Close()",
    "   semaphoreSize := 100",
    "	tasks := make(chan string, semaphoreSize)",
    "	results := make(chan string, semaphoreSize)",
    "   done := make(chan bool)",
    "	var wg sync.WaitGroup",
    "	fmt.Println(\"Starting workers...\")",
    "	for i := 0; i < semaphoreSize; i++ {",
    "		wg.Add(1)",
    "		go worker(tasks, results, &wg)",
    "	}",
    "   go writer(results, outFile, done)",
    "	scanner := bufio.NewScanner(batch)",
    "   count := 0",
    "	for scanner.Scan() {",
    "		line := strings.TrimSpace(scanner.Text())",
    "		if line != \"\" { tasks <- line; count++ }",
    "       fmt.Printf(\"\\rLoaded %d tasks...\", count)",
    "	}",
    "   fmt.Println(\"\\nAll tasks loaded. Waiting for workers to finish...\")",
    "	close(tasks)",
    "	wg.Wait()",
    "   close(results)",
    "   <-done",
    "   fmt.Println(\"\\nDone!\")",
    "}",
]

# 子网TCP扫描模板 (已修复缺失的包和代码逻辑)
SUBNET_TCP_SCANNER_GO_TEMPLATE_LINES = [
    "package main",
    "",
    "import (",
    "	\"fmt\"",
    "	\"net\"",
    "	\"os\"",
    "	\"strconv\"",
    "	\"sync\"",
    "	\"time\"",
    ")",
    "",
    "func inc(ip net.IP) {",
    "	for j := len(ip) - 1; j >= 0; j-- {",
    "		ip[j]++",
    "		if ip[j] > 0 { break }",
    "	}",
    "}",
    "",
    "func worker(jobs <-chan net.IP, results chan<- string, port string, wg *sync.WaitGroup) {",
    "	defer wg.Done()",
    "	for ip := range jobs {",
    "		target := fmt.Sprintf(\"%s:%s\", ip.String(), port)",
    "		conn, err := net.DialTimeout(\"tcp\", target, 3*time.Second)",
    "		if err == nil {",
    "			conn.Close()",
    "			results <- target",
    "		}",
    "	}",
    "}",
    "",
    "func main() {",
    "	if len(os.Args) < 5 {",
    "		fmt.Println(\"Usage: ./subnet_scanner <cidr> <port> <outputFile> <concurrency>\")",
    "		os.Exit(1)",
    "	}",
    "	cidr := os.Args[1]",
    "	port := os.Args[2]",
    "	outputFile := os.Args[3]",
    "	concurrency, _ := strconv.Atoi(os.Args[4])",
    "",
    "	outFile, err := os.OpenFile(outputFile, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)",
    "	if err != nil { fmt.Println(\"Cannot open output file:\", err); return }",
    "	defer outFile.Close()",
    "",
    "	ip, ipnet, err := net.ParseCIDR(cidr)",
    "	if err != nil { fmt.Println(\"Invalid CIDR:\", err); return }",
    "",
    "	jobs := make(chan net.IP, concurrency)",
    "	results := make(chan string, concurrency)",
    "	var wg sync.WaitGroup",
    "",
    "	for i := 0; i < concurrency; i++ {",
    "		wg.Add(1)",
    "		go worker(jobs, results, port, &wg)",
    "	}",
    "",
    "	go func() {",
    "		for ip := ip.Mask(ipnet.Mask); ipnet.Contains(ip); inc(ip) {",
    "			ipCopy := make(net.IP, len(ip))",
    "			copy(ipCopy, ip)",
    "			jobs <- ipCopy",
    "		}",
    "		close(jobs)",
    "	}()",
    "",
    "   var writeWg sync.WaitGroup",
    "   writeWg.Add(1)",
    "   go func() {",
    "       defer writeWg.Done()",
    "       for r := range results {",
    "           outFile.WriteString(r + \"\\n\")",
    "           fmt.Println(\"Found:\", r)",
    "       }",
    "   }()",
    "",
    "	wg.Wait()",
    "   close(results)",
    "   writeWg.Wait()",
    "   fmt.Println(\"Scan finished.\")",
    "}",
]

# --- 全局变量和常量 ---
CONFIG_FILE = 'config.json'
HISTORY_FILE = 'history.json'
LOG_FILE = 'fofa_bot.log'
MAX_HISTORY_SIZE = 50
CACHE_EXPIRATION_SECONDS = 24 * 60 * 60  # 24 hours
FOFA_SEARCH_URL = "https://fofa.info/api/v1/search/all"
FOFA_INFO_URL = "https://fofa.info/api/v1/info/my"
FOFA_STATS_URL = "https://fofa.info/api/v1/stats/statistical"

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
    STATE_KKFOFA_MODE, STATE_CACHE_CHOICE, STATE_GET_IMPORT_QUERY, STATE_GET_STATS_QUERY,
    STATE_PRESET_MENU, STATE_GET_PRESET_NAME, STATE_GET_PRESET_QUERY, STATE_REMOVE_PRESET
) = range(13)

# --- 配置管理 ---
def load_json_file(filename, default_content):
    if not os.path.exists(filename):
        with open(filename, 'w', encoding='utf-8') as f: json.dump(default_content, f, indent=4)
        return default_content
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            config = json.load(f)
            for key, value in default_content.items(): config.setdefault(key, value)
            return config
    except (json.JSONDecodeError, IOError):
        logger.error(f"{filename} 损坏，将使用默认配置重建。")
        with open(filename, 'w', encoding='utf-8') as f: json.dump(default_content, f, indent=4)
        return default_content

def save_json_file(filename, data):
    with open(filename, 'w', encoding='utf-8') as f: json.dump(data, f, indent=4, ensure_ascii=False)

DEFAULT_CONFIG = { "bot_token": "YOUR_BOT_TOKEN_HERE", "apis": [], "admins": [], "proxy": "", "full_mode": False, "public_mode": False, "presets": [] }
CONFIG = load_json_file(CONFIG_FILE, DEFAULT_CONFIG)
HISTORY = load_json_file(HISTORY_FILE, {"queries": []})

def save_config(): save_json_file(CONFIG_FILE, CONFIG)
def save_history(): save_json_file(HISTORY_FILE, HISTORY)

# --- 缓存和历史记录核心函数 ---
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
    if query and query.get('cache'): return query
    return None

# --- 辅助函数与装饰器 ---
def generate_filename_from_query(query_text: str) -> str:
    """根据查询语句生成安全的文件名"""
    sanitized_query = re.sub(r'[^a-z0-9\-_]+', '_', query_text.lower())
    sanitized_query = sanitized_query.strip('_')
    max_len = 100
    if len(sanitized_query) > max_len:
        sanitized_query = sanitized_query[:max_len].rsplit('_', 1)[0]
    timestamp = int(time.time())
    return f"fofa_{sanitized_query}_{timestamp}.txt"

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
    escape_chars = r'_*`['
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

# --- FOFA API 核心逻辑 ---
def _make_api_request(url, params, timeout=60, use_b64=True):
    try:
        if use_b64 and 'q' in params: params['qbase64'] = base64.b64encode(params.pop('q').encode('utf-8')).decode('utf-8')
        response = requests.get(url, params=params, timeout=timeout, proxies=get_proxies(), verify=False)
        response.raise_for_status()
        data = response.json()
        if data.get("error"): return None, data.get("errmsg", "未知的FOFA错误")
        return data, None
    except requests.exceptions.RequestException as e: return None, f"网络请求失败: {e}"
    except json.JSONDecodeError: return None, "解析JSON响应失败"
def verify_fofa_api(key): return _make_api_request(FOFA_INFO_URL, {'key': key}, timeout=15, use_b64=False)
def fetch_fofa_data(key, query, page=1, page_size=10000, fields="host"):
    params = {'key': key, 'q': query, 'size': page_size, 'page': page, 'fields': fields, 'full': CONFIG.get("full_mode", False)}
    return _make_api_request(FOFA_SEARCH_URL, params)
def fetch_fofa_stats(key, query):
    params = {'key': key, 'q': query}
    return _make_api_request(FOFA_STATS_URL, params)
def execute_query_with_fallback(query_func, preferred_key_index=None):
    if not CONFIG['apis']: return None, None, "没有配置任何API Key。"
    keys_to_try = CONFIG['apis']; start_index = 0
    if preferred_key_index is not None and 1 <= preferred_key_index <= len(keys_to_try): start_index = preferred_key_index - 1
    for i in range(len(keys_to_try)):
        idx = (start_index + i) % len(keys_to_try); key = keys_to_try[idx]; key_num = idx + 1
        data, error = query_func(key)
        if not error: return data, key_num, None
        if "[820031]" in str(error): logger.warning(f"Key [#{key_num}] F点余额不足，尝试下一个..."); continue
        return None, key_num, error
    return None, None, "所有Key均尝试失败。"

# --- 存活检测工具辅助函数 ---
def offer_liveness_checker(bot, chat_id, input_filename):
    """发送一个带有获取存活检测工具按钮的消息"""
    keyboard = [[InlineKeyboardButton("⚡️ 获取存活检测工具", callback_data=f'get_liveness_checker_{input_filename}')]]
    bot.send_message(chat_id, "需要对结果进行TCP活性检测吗？", reply_markup=InlineKeyboardMarkup(keyboard))

# --- 后台下载任务 ---
def start_download_job(context: CallbackContext, callback_func, job_data):
    chat_id = job_data['chat_id']; job_name = f"download_job_{chat_id}"
    for job in context.job_queue.get_jobs_by_name(job_name): job.schedule_removal()
    context.bot_data.pop(f'stop_job_{chat_id}', None)
    context.job_queue.run_once(callback_func, 1, context=job_data, name=job_name)

def run_full_download_query(context: CallbackContext):
    job_data = context.job.context; bot, chat_id, query_text, total_size = context.bot, job_data['chat_id'], job_data['query'], job_data['total_size']
    output_filename = generate_filename_from_query(query_text)
    unique_results, stop_flag = set(), f'stop_job_{chat_id}'
    msg = bot.send_message(chat_id, "⏳ 开始全量下载任务...")
    pages_to_fetch = (total_size + 9999) // 10000
    for page in range(1, pages_to_fetch + 1):
        if context.bot_data.get(stop_flag): msg.edit_text("🌀 下载任务已手动停止."); break
        try: msg.edit_text(f"下载进度: {len(unique_results)}/{total_size} (Page {page}/{pages_to_fetch})...")
        except BadRequest: pass
        data, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, query_text, page, 10000, "host"))
        if error: msg.edit_text(f"❌ 第 {page} 页下载出错: {error}"); break
        results = data.get('results', []);
        if not results: break
        unique_results.update(res for res in results if ':' in res)
    if unique_results:
        with open(output_filename, 'w', encoding='utf-8') as f: f.write("\n".join(unique_results))
        msg.edit_text(f"✅ 下载完成！共 {len(unique_results)} 条。正在发送...")
        with open(output_filename, 'rb') as doc: sent_message = bot.send_document(chat_id, document=doc, filename=output_filename)
        os.remove(output_filename)
        cache_data = {'file_id': sent_message.document.file_id, 'file_name': output_filename, 'result_count': len(unique_results)}
        add_or_update_query(query_text, cache_data)
        offer_liveness_checker(bot, chat_id, sent_message.document.file_name)
    elif not context.bot_data.get(stop_flag): msg.edit_text("🤷‍♀️ 任务完成，但未能下载到任何数据。")
    context.bot_data.pop(stop_flag, None)

def run_traceback_download_query(context: CallbackContext):
    job_data = context.job.context; bot, chat_id, base_query = context.bot, job_data['chat_id'], job_data['query']
    output_filename = generate_filename_from_query(base_query)
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
        original_count = len(unique_results)
        unique_results.update([r[0] for r in results if r and r[0] and ':' in r[0]])
        newly_added_count = len(unique_results) - original_count
        try: msg.edit_text(f"⏳ 已找到 {len(unique_results)} 条... (第 {page_count} 轮, 新增 {newly_added_count})")
        except BadRequest: pass
        valid_anchor_found = False
        for i in range(len(results) - 1, -1, -1):
            if not results[i] or len(results[i]) < 2 or not results[i][1]: continue
            try:
                timestamp_str = results[i][1]; current_date_obj = datetime.strptime(timestamp_str.split(' ')[0], '%Y-%m-%d').date()
                if last_page_date and current_date_obj >= last_page_date: continue
                next_page_date_obj = current_date_obj
                if last_page_date and current_date_obj == last_page_date: next_page_date_obj -= timedelta(days=1)
                last_page_date = current_date_obj
                current_query = f'({base_query}) && before="{next_page_date_obj.strftime("%Y-%m-%d")}"'
                valid_anchor_found = True
                break
            except (ValueError, TypeError): continue
        if not valid_anchor_found: termination_reason = "\n\n⚠️ 无法找到有效的时间锚点以继续，可能已达查询边界."; break
    if unique_results:
        with open(output_filename, 'w', encoding='utf-8') as f: f.write("\n".join(sorted(list(unique_results))))
        msg.edit_text(f"✅ 深度追溯完成！共 {len(unique_results)} 条。{termination_reason}\n正在发送文件...")
        with open(output_filename, 'rb') as doc: sent_message = bot.send_document(chat_id, document=doc, filename=output_filename)
        os.remove(output_filename)
        cache_data = {'file_id': sent_message.document.file_id, 'file_name': output_filename, 'result_count': len(unique_results)}
        add_or_update_query(base_query, cache_data)
        offer_liveness_checker(bot, chat_id, sent_message.document.file_name)
    else: msg.edit_text(f"🤷‍♀️ 任务完成，但未能下载到任何数据。{termination_reason}")
    context.bot_data.pop(stop_flag, None)

def run_incremental_update_query(context: CallbackContext):
    job_data = context.job.context; bot, chat_id, base_query = context.bot, job_data['chat_id'], job_data['query']
    msg = bot.send_message(chat_id, "--- 增量更新启动 ---")
    msg.edit_text("1/5: 正在获取旧缓存..."); cached_item = find_cached_query(base_query)
    if not cached_item: msg.edit_text("❌ 错误：找不到缓存项。"); return
    old_file_path = f"old_{cached_item['cache']['file_name']}"; old_results = set()
    try:
        file = bot.get_file(cached_item['cache']['file_id']); file.download(old_file_path)
        with open(old_file_path, 'r', encoding='utf-8') as f: old_results = set(line.strip() for line in f if line.strip() and ':' in line)
    except BadRequest: msg.edit_text("❌ **错误：缓存文件已无法下载**\n请选择 **🔍 全新搜索**。", parse_mode=ParseMode.MARKDOWN); return
    except Exception as e: msg.edit_text(f"❌ 读取缓存文件失败: {e}"); return
    msg.edit_text("2/5: 正在确定更新起始点..."); data, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, base_query, fields="lastupdatetime"))
    if error or not data.get('results'): msg.edit_text(f"❌ 无法获取最新记录时间戳: {error or '无结果'}"); os.remove(old_file_path); return
    ts_str = data['results'][0][0] if isinstance(data['results'][0], list) else data['results'][0]; cutoff_date = ts_str.split(' ')[0]
    incremental_query = f'({base_query}) && after="{cutoff_date}"'
    msg.edit_text(f"3/5: 正在侦察自 {cutoff_date} 以来的新数据..."); data, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, incremental_query, page_size=1))
    if error: msg.edit_text(f"❌ 侦察查询失败: {error}"); os.remove(old_file_path); return
    total_new_size = data.get('size', 0)
    if total_new_size == 0: msg.edit_text("✅ 未发现新数据。缓存已是最新。"); os.remove(old_file_path); return
    new_results, stop_flag = set(), f'stop_job_{chat_id}'; pages_to_fetch = (total_new_size + 9999) // 10000
    for page in range(1, pages_to_fetch + 1):
        if context.bot_data.get(stop_flag): msg.edit_text("🌀 增量更新已手动停止。"); os.remove(old_file_path); return
        msg.edit_text(f"3/5: 正在下载新数据... ( Page {page}/{pages_to_fetch} )")
        data, _, error = execute_query_with_fallback(lambda key: fetch_fofa_data(key, incremental_query, page=page, page_size=10000))
        if error: msg.edit_text(f"❌ 下载新数据失败: {error}"); os.remove(old_file_path); return
        if data.get('results'): new_results.update(res for res in data.get('results', []) if ':' in res)
    msg.edit_text(f"4/5: 正在合并数据... (发现 {len(new_results)} 条新数据)"); combined_results = sorted(list(new_results.union(old_results)))
    output_filename = generate_filename_from_query(base_query)
    with open(output_filename, 'w', encoding='utf-8') as f: f.write("\n".join(combined_results))
    msg.edit_text(f"5/5: 发送更新后的文件... (共 {len(combined_results)} 条)")
    with open(output_filename, 'rb') as doc: sent_message = bot.send_document(chat_id, document=doc, filename=output_filename)
    cache_data = {'file_id': sent_message.document.file_id, 'file_name': output_filename, 'result_count': len(combined_results)}
    add_or_update_query(base_query, cache_data); os.remove(old_file_path); os.remove(output_filename)
    msg.delete()
    bot.send_message(chat_id, f"✅ 增量更新完成！")
    offer_liveness_checker(bot, chat_id, sent_message.document.file_name)


# --- 主要命令 ---
def start_command(update: Update, context: CallbackContext):
    update.message.reply_text('👋 欢迎使用 Fofa 查询机器人！请使用 /help 查看命令手册。')
    if not CONFIG['admins']: first_admin_id = update.effective_user.id; CONFIG.setdefault('admins', []).append(first_admin_id); save_config(); update.message.reply_text(f"ℹ️ 已自动将您 (ID: `{first_admin_id}`) 添加为第一个管理员。")
def help_command(update: Update, context: CallbackContext):
    help_text = ( "📖 *Fofa 机器人指令手册*\n\n"
                  "*🔍 资产查询*\n`/kkfofa [key编号] <查询语句>`\n_不带参数则显示预设菜单_\n\n"
                  "*📊 资产统计*\n`/stats` - 获取全球资产统计信息\n\n"
                  "*🛠️ 辅助工具*\n`/getscanner` - 获取Go语言扫描工具\n\n"
                  "*⚙️ 管理与设置*\n`/settings` - 进入交互式设置菜单\n_可管理API, 代理, 预设等_\n\n"
                  "*💾 高级功能*\n"
                  "`/backup` / `/restore` - 备份/恢复配置\n"
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
    if not context.args:
        presets = CONFIG.get("presets", [])
        if not presets:
            update.message.reply_text("欢迎使用FOFA查询机器人。\n\n➡️ 直接输入查询语法: `/kkfofa domain=\"example.com\"`\nℹ️ 当前没有可用的预设查询。管理员可通过 /settings 添加。")
            return ConversationHandler.END
        keyboard = [[InlineKeyboardButton(p['name'], callback_data=f"run_preset_{i}")] for i, p in enumerate(presets)]
        update.message.reply_text("👇 请选择一个预设查询，或直接输入查询语法:", reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END

    key_index, query_text = None, " ".join(context.args)

    # --- START: IndentationError FIX ---
    if context.args[0].isdigit():
        try:
            num = int(context.args[0])
            if 1 <= num <= len(CONFIG['apis']):
                key_index = num
                query_text = " ".join(context.args[1:])
        except ValueError:
            pass # Ignore if conversion fails, treat as part of the query
    # --- END: IndentationError FIX ---

    context.user_data.update({'query': query_text, 'key_index': key_index, 'chat_id': update.effective_chat.id})
    cached_item = find_cached_query(query_text)

    if cached_item:
        dt_utc = datetime.fromisoformat(cached_item['timestamp']); dt_local = dt_utc.astimezone(tz.tzlocal()); time_str = dt_local.strftime('%Y-%m-%d %H:%M')
        result_count = cached_item['cache']['result_count']
        is_expired = (datetime.now(tz.tzutc()) - dt_utc).total_seconds() > CACHE_EXPIRATION_SECONDS
        message_text = (f"✅ *发现缓存*\n\n查询: `{escape_markdown(query_text)}`\n缓存于: *{time_str}* (含 *{result_count}* 条结果)\n\n")
        keyboard = []
        if is_expired:
            message_text += "⚠️ *此缓存已超过24小时，无法用于增量更新。*"
            keyboard.append([InlineKeyboardButton("⬇️ 下载旧缓存", callback_data='cache_download'), InlineKeyboardButton("🔍 全新搜索", callback_data='cache_newsearch')])
        else:
            message_text += "请选择操作："
            keyboard.append([InlineKeyboardButton("🔄 增量更新", callback_data='cache_incremental')])
            keyboard.append([InlineKeyboardButton("⬇️ 下载缓存", callback_data='cache_download'), InlineKeyboardButton("🔍 全新搜索", callback_data='cache_newsearch')])
        keyboard.append([InlineKeyboardButton("❌ 取消", callback_data='cache_cancel')])
        update.message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        return STATE_CACHE_CHOICE
    
    return start_new_search(update, context)

def start_new_search(update: Update, context: CallbackContext, message_to_edit=None):
    query_text = context.user_data['query']; key_index = context.user_data.get('key_index'); add_or_update_query(query_text)
    if message_to_edit: msg = message_to_edit; msg.edit_text("🔄 正在执行全新查询...")
    else: msg = update.effective_message.reply_text("🔄 正在执行全新查询...")
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

def run_preset_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer()
    try:
        preset_index = int(query.data.replace("run_preset_", "")); preset = CONFIG["presets"][preset_index]
        context.user_data.update({'query': preset['query'], 'key_index': None, 'chat_id': update.effective_chat.id})
        start_new_search(update, context, message_to_edit=query.message)
    except (ValueError, IndexError): query.edit_message_text("❌ 预设查询失败，可能该预设已被移除。")
def cache_choice_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); choice = query.data.split('_')[1]
    if choice == 'download':
        cached_item = find_cached_query(context.user_data['query'])
        if cached_item:
            query.edit_message_text("⬇️ 正在从缓存发送文件...");
            try: context.bot.send_document(chat_id=update.effective_chat.id, document=cached_item['cache']['file_id']); query.delete_message()
            except BadRequest as e: query.edit_message_text(f"❌ 发送缓存失败: {e}")
        else: query.edit_message_text("❌ 找不到缓存记录。")
        return ConversationHandler.END
    elif choice == 'newsearch': return start_new_search(update, context, message_to_edit=query.message)
    elif choice == 'incremental': query.edit_message_text("⏳ 准备增量更新..."); start_download_job(context, run_incremental_update_query, context.user_data); query.delete_message(); return ConversationHandler.END
    elif choice == 'cancel': query.edit_message_text("操作已取消。"); return ConversationHandler.END
def query_mode_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); mode = query.data.split('_')[1]
    if mode == 'full': query.edit_message_text(f"⏳ 开始全量下载任务..."); start_download_job(context, run_full_download_query, context.user_data); query.delete_message()
    elif mode == 'traceback': query.edit_message_text(f"⏳ 开始深度追溯下载任务..."); start_download_job(context, run_traceback_download_query, context.user_data); query.delete_message()
    elif mode == 'cancel': query.edit_message_text("操作已取消。")
    return ConversationHandler.END
@admin_only
def stats_command(update: Update, context: CallbackContext) -> int:
    update.message.reply_text("请输入你想要统计的 FOFA 语法。\n例如: `app=\"nginx\"`\n\n随时可以发送 /cancel 来取消。", parse_mode=ParseMode.MARKDOWN); return STATE_GET_STATS_QUERY
def get_fofa_stats_query(update: Update, context: CallbackContext) -> int:
    query_text = update.message.text; processing_message = update.message.reply_text("正在查询 FOFA 统计, 请稍候...")
    data, _, error = execute_query_with_fallback(lambda key: fetch_fofa_stats(key, query_text))
    if error: processing_message.edit_text(f"查询失败 😞\n*原因:* `{error}`", parse_mode=ParseMode.MARKDOWN); return ConversationHandler.END
    stats_data = data
    message_lines = [ f"*📊 FOFA 全球统计信息*", f"*查询:* `{escape_markdown(query_text)}`", f"*最后更新:* `{stats_data.get('last_update_time', 'N/A')}`", "", "*🌍 Top 5 国家/地区:*",]
    for item in stats_data.get("countries", [])[:5]: message_lines.append(f"  - `{escape_markdown(item['name'])}`: *{item['count']}*")
    message_lines.append("\n*💻 Top 5 服务/组件:*");
    for item in stats_data.get("as_servers", [])[:5]: message_lines.append(f"  - `{escape_markdown(item['name'])}`: *{item['count']}*")
    message_lines.append("\n*🔌 Top 5 协议:*");
    for item in stats_data.get("protocols", [])[:5]: message_lines.append(f"  - `{escape_markdown(item['name'])}`: *{item['count']}*")
    processing_message.edit_text("\n".join(message_lines), parse_mode=ParseMode.MARKDOWN); return ConversationHandler.END

# --- 扫描工具相关 ---
def get_scanner_command(update: Update, context: CallbackContext):
    keyboard = [
        [InlineKeyboardButton("⚡️ IP:PORT 存活检测器", callback_data='get_liveness_checker_standalone')],
        [InlineKeyboardButton("🌐 子网C段扫描器", callback_data='get_subnet_scanner')],
    ]
    update.message.reply_text("请选择您需要的Go扫描工具：", reply_markup=InlineKeyboardMarkup(keyboard))

def get_liveness_checker_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer()
    parts = query.data.split('_', 3)
    input_filename = parts[3] if len(parts) > 3 else "input.txt"
    go_code = "\n".join(TCP_ACTIVE_GO_TEMPLATE_LINES)
    checker_filename = "liveness_checker.go"
    with open(checker_filename, "w", encoding='utf-8') as f: f.write(go_code)
    caption = ( "✅ **Go语言存活检测工具**\n\n"
                "**用法:**\n1. 将此文件与您的 `ip:port` 结果文件放在同一目录。\n"
                "2. 确保您已安装 Go 环境。\n"
                "3. 在终端中运行以下命令：\n\n"
                f"`go run {checker_filename} {input_filename} live_results.txt`\n\n"
                f"存活的IP将会被保存在 `live_results.txt` 文件中。" )
    with open(checker_filename, 'rb') as doc:
        context.bot.send_document(chat_id=query.effective_chat.id, document=doc, caption=caption, parse_mode=ParseMode.MARKDOWN)
    os.remove(checker_filename)
    if query.message.reply_markup and 'standalone' not in query.data:
        query.edit_message_text("✅ 工具已发送，请查收。")

def get_subnet_scanner_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer()
    go_code = "\n".join(SUBNET_TCP_SCANNER_GO_TEMPLATE_LINES)
    scanner_filename = "subnet_scanner.go"
    with open(scanner_filename, "w", encoding='utf-8') as f: f.write(go_code)
    caption = ( "✅ **Go语言子网扫描工具**\n\n"
                "**用法:**\n1. 将此文件保存在任意位置。\n"
                "2. 确保您已安装 Go 环境。\n"
                "3. 在终端中运行以下命令：\n\n"
                "`go run subnet_scanner.go <CIDR> <端口> <输出文件名> <并发数>`\n\n"
                "**示例:**\n"
                "`go run subnet_scanner.go 192.168.1.0/24 80 results.txt 100`")
    with open(scanner_filename, 'rb') as doc:
        context.bot.send_document(chat_id=query.effective_chat.id, document=doc, caption=caption, parse_mode=ParseMode.MARKDOWN)
    os.remove(scanner_filename)

@admin_only
def stop_all_tasks(update: Update, context: CallbackContext): context.bot_data[f'stop_job_{update.effective_chat.id}'] = True; update.message.reply_text("✅ 已发送停止信号。")
@admin_only
def backup_config_command(update: Update, context: CallbackContext):
    if os.path.exists(CONFIG_FILE): update.effective_chat.send_document(document=open(CONFIG_FILE, 'rb'))
    else: update.effective_chat.send_message("❌ 找不到配置文件。")
@admin_only
def restore_config_command(update: Update, context: CallbackContext): update.message.reply_text("📥 要恢复配置，请直接将您的 `config.json` 文件作为文档发送给我。")
@admin_only
def receive_config_file(update: Update, context: CallbackContext):
    global CONFIG
    if update.message.document.file_name != CONFIG_FILE: update.message.reply_text(f"❌ 文件名错误，必须为 `{CONFIG_FILE}`。"); return
    try:
        file = update.message.document.get_file(); temp_path = f"{CONFIG_FILE}.tmp"; file.download(temp_path)
        with open(temp_path, 'r', encoding='utf-8') as f: json.load(f)
        os.replace(temp_path, CONFIG_FILE); CONFIG = load_json_file(CONFIG_FILE, DEFAULT_CONFIG)
        update.message.reply_text("✅ 配置已成功恢复！机器人功能可能需要重启后完全生效。")
    except Exception as e:
        logger.error(f"恢复配置失败: {e}"); update.message.reply_text(f"❌ 恢复失败: {e}")
        if os.path.exists(temp_path): os.remove(temp_path)
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
    cache_data = {'file_id': doc.file_id, 'file_name': doc.file_name, 'result_count': -1}; msg = update.message.reply_text("正在统计文件行数...")
    try:
        temp_path = f"import_{doc.file_name}"; file = doc.get_file(); file.download(temp_path)
        with open(temp_path, 'r', encoding='utf-8') as f: counted_lines = sum(1 for line in f if line.strip())
        cache_data['result_count'] = counted_lines; os.remove(temp_path); msg.edit_text(f"✅ *导入成功！*\n\n查询 `{escape_markdown(query_text)}` 已成功关联 *{counted_lines}* 条结果的缓存。", parse_mode=ParseMode.MARKDOWN)
    except Exception as e: logger.warning(f"无法下载或统计导入文件: {e}，将作为大文件模式导入。"); msg.edit_text(f"✅ *导入成功 (大文件模式)！*\n\n查询 `{escape_markdown(query_text)}` 已成功关联缓存（结果数未知）。", parse_mode=ParseMode.MARKDOWN)
    add_or_update_query(query_text, cache_data); context.user_data.clear(); return ConversationHandler.END
@admin_only
def get_log_command(update: Update, context: CallbackContext):
    if os.path.exists(LOG_FILE): update.message.reply_document(document=open(LOG_FILE, 'rb'))
    else: update.message.reply_text("❌ 未找到日志文件。")
@admin_only
def shutdown_command(update: Update, context: CallbackContext): update.message.reply_text("✅ 收到指令！机器人正在关闭..."); logger.info(f"接收到来自用户 {update.effective_user.id} 的关闭指令。"); context.job_queue.run_once(lambda ctx: ctx.dispatcher.updater.stop(), 1)
@admin_only
def settings_command(update: Update, context: CallbackContext):
    keyboard = [[InlineKeyboardButton("🔑 API 管理", callback_data='settings_api')], [InlineKeyboardButton("✨ 预设管理", callback_data='settings_preset')], [InlineKeyboardButton("🌐 代理设置", callback_data='settings_proxy')], [InlineKeyboardButton("💾 备份与恢复", callback_data='settings_backup')], [InlineKeyboardButton("❌ 关闭菜单", callback_data='settings_close')]]
    message_text = "⚙️ *设置菜单*"; reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query: update.callback_query.edit_message_text(message_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    else: update.message.reply_text(message_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    return STATE_SETTINGS_MAIN
def settings_callback_handler(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); menu = query.data.split('_', 1)[1]
    if menu == 'api': return show_api_menu(update, context)
    if menu == 'proxy': return show_proxy_menu(update, context)
    if menu == 'backup': return show_backup_restore_menu(update, context)
    if menu == 'preset': return show_preset_menu(update, context)
    if menu == 'close': query.edit_message_text("菜单已关闭."); return ConversationHandler.END
    return STATE_SETTINGS_ACTION
def show_api_menu(update: Update, context: CallbackContext):
    msg = update.callback_query.message; msg.edit_text("🔄 正在查询API Key状态..."); api_details = []
    for i, key in enumerate(CONFIG['apis']):
        data, error = verify_fofa_api(key); key_masked = f"`...{key[-4:]}`"
        status = f"❌ 无效: {error}" if error else f"({escape_markdown(data.get('username', 'N/A'))}, {'✅ VIP' if data.get('is_vip') else '👤 普通'}, F币: {data.get('fcoin', 0)})"
        api_details.append(f"`#{i+1}` {key_masked} {status}")
    api_message = "\n\n".join(api_details) if api_details else "_无_"; keyboard = [[InlineKeyboardButton(f"查询范围: {'✅ 完整历史' if CONFIG.get('full_mode') else '⏳ 近一年'}", callback_data='action_toggle_full')], [InlineKeyboardButton("➕ 添加Key", callback_data='action_add_api'), InlineKeyboardButton("➖ 删除Key", callback_data='action_remove_api')], [InlineKeyboardButton("🔙 返回", callback_data='action_back_main')]]
    msg.edit_text(f"🔑 *API 管理*\n\n{api_message}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN); return STATE_SETTINGS_ACTION
def show_proxy_menu(update: Update, context: CallbackContext):
    keyboard = [[InlineKeyboardButton("✏️ 设置/更新", callback_data='action_set_proxy')], [InlineKeyboardButton("🗑️ 清除", callback_data='action_delete_proxy')], [InlineKeyboardButton("🔙 返回", callback_data='action_back_main')]]
    update.callback_query.edit_message_text(f"🌐 *代理设置*\n当前: `{CONFIG.get('proxy') or '未设置'}`", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN); return STATE_SETTINGS_ACTION
def show_backup_restore_menu(update: Update, context: CallbackContext):
    message_text = ("💾 *备份与恢复*\n\n📤 *备份*\n点击下方按钮，或使用 /backup 命令。\n\n📥 *恢复*\n直接向机器人*发送* `config.json` 文件即可。"); keyboard = [[InlineKeyboardButton("📤 立即备份", callback_data='action_backup_now')], [InlineKeyboardButton("🔙 返回", callback_data='action_back_main')]]
    update.callback_query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN); return STATE_SETTINGS_ACTION
def settings_action_handler(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); action = query.data.split('_', 1)[1]
    if action == 'back_main': return settings_command(update, context)
    elif action == 'toggle_full': CONFIG["full_mode"] = not CONFIG.get("full_mode", False); save_config(); return show_api_menu(update, context)
    elif action == 'add_api': query.edit_message_text("请发送您的 Fofa API Key。"); return STATE_GET_KEY
    elif action == 'remove_api': query.edit_message_text("请输入要删除的API Key编号(#)。"); return STATE_REMOVE_API
    elif action == 'set_proxy': query.edit_message_text("请输入代理地址 (例如 http://127.0.0.1:7890)。"); return STATE_GET_PROXY
    elif action == 'delete_proxy': CONFIG['proxy'] = ""; save_config(); query.answer("代理已清除"); return show_proxy_menu(update, context)
    elif action == 'backup_now': backup_config_command(update, context); return STATE_SETTINGS_ACTION
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
def show_preset_menu(update: Update, context: CallbackContext):
    preset_list = "\n".join([f"`#{i+1}`: `{p['name']}`" for i, p in enumerate(CONFIG['presets'])]) or "_无_"
    text = f"✨ *预设管理*\n\n{preset_list}"; kbd = [[InlineKeyboardButton("➕ 添加", callback_data='preset_add'), InlineKeyboardButton("➖ 移除", callback_data='preset_remove')], [InlineKeyboardButton("🔙 返回", callback_data='preset_back')]]
    update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kbd), parse_mode=ParseMode.MARKDOWN); return STATE_PRESET_MENU
def preset_menu_callback(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer(); action = query.data.split('_', 1)[1]
    if action == 'back': return settings_command(update, context)
    if action == 'add': query.edit_message_text("请输入预设的名称 (例如: 海康威视摄像头):"); return STATE_GET_PRESET_NAME
    if action == 'remove': query.edit_message_text("请输入要移除的预设的编号(#):"); return STATE_REMOVE_PRESET
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
    if update.callback_query: update.callback_query.edit_message_text('操作已取消。')
    elif update.message: update.message.reply_text('操作已取消。')
    context.user_data.clear(); return ConversationHandler.END

# --- 主程序入口 ---
def main() -> None:
    bot_token = CONFIG.get("bot_token")
    if not bot_token or bot_token == "YOUR_BOT_TOKEN_HERE":
        logger.critical("严重错误：config.json 中的 'bot_token' 未设置！请修改配置文件。")
        if not os.path.exists(CONFIG_FILE): save_config()
        return

    updater = Updater(token=bot_token, use_context=True); dispatcher = updater.dispatcher
    commands = [
        BotCommand("start", "🚀 启动机器人与帮助"),
        BotCommand("help", "❓ 获取命令手册"),
        BotCommand("kkfofa", "🔍 资产搜索 (或显示预设)"),
        BotCommand("stats", "📊 全球资产统计"),
        BotCommand("getscanner", "🛠️ 获取扫描工具"),
        BotCommand("settings", "⚙️ (管理员) 设置菜单"),
        BotCommand("history", "🕰️ (管理员) 查询历史"),
        BotCommand("import", "🖇️ (管理员) 导入旧缓存"),
        BotCommand("backup", "📤 (管理员) 备份配置"),
        BotCommand("restore", "📥 (管理员) 恢复配置"),
        BotCommand("getlog", "📄 (管理员) 获取日志"),
        BotCommand("shutdown", "🔌 (管理员) 关闭机器人"),
        BotCommand("stop", "🛑 (管理员) 停止任务"),
        BotCommand("cancel", "❌ 取消当前操作")
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
        }, fallbacks=[CommandHandler("cancel", cancel)]
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
    stats_conv = ConversationHandler(
        entry_points=[CommandHandler("stats", stats_command)],
        states={STATE_GET_STATS_QUERY: [MessageHandler(Filters.text & ~Filters.command, get_fofa_stats_query)]},
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    # 添加所有处理器
    dispatcher.add_handler(CommandHandler("start", start_command))
    dispatcher.add_handler(CommandHandler("help", help_command))
    dispatcher.add_handler(CommandHandler("stop", stop_all_tasks))
    dispatcher.add_handler(CommandHandler("backup", backup_config_command))
    dispatcher.add_handler(CommandHandler("restore", restore_config_command))
    dispatcher.add_handler(CommandHandler("history", history_command))
    dispatcher.add_handler(CommandHandler("getlog", get_log_command))
    dispatcher.add_handler(CommandHandler("shutdown", shutdown_command))
    dispatcher.add_handler(CommandHandler("getscanner", get_scanner_command))
    dispatcher.add_handler(settings_conv)
    dispatcher.add_handler(kkfofa_conv)
    dispatcher.add_handler(import_conv)
    dispatcher.add_handler(stats_conv)
    dispatcher.add_handler(CallbackQueryHandler(run_preset_callback, pattern=r"^run_preset_"))
    dispatcher.add_handler(CallbackQueryHandler(get_liveness_checker_callback, pattern=r"^get_liveness_checker"))
    dispatcher.add_handler(CallbackQueryHandler(get_subnet_scanner_callback, pattern=r"^get_subnet_scanner"))
    dispatcher.add_handler(MessageHandler(Filters.document.mime_type("application/json"), receive_config_file))
    
    logger.info("🚀 终极增强版机器人已启动 (缩进已修复)...")
    updater.start_polling()
    updater.idle()
    logger.info("机器人已关闭。")

if __name__ == "__main__":
    main()
