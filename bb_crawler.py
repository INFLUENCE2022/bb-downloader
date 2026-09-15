# -*- coding: utf-8 -*-
"""BlackBoard 全课程批量下载器。

把 BB 上账号可见的**所有课程**资料，按学期归档到 <项目根>/<年份>年<学期>/<课程>/...，
目录结构镜像 BB 的内容树。

用法：
    python bb_crawler.py --dry-run           # 只列出课程与学期映射，不下载
    python bb_crawler.py                     # 全量下载（跳过已完成课程）
    python bb_crawler.py --course _12345_1   # 只跑一门课（course_id 见 --dry-run 输出）
    python bb_crawler.py --force             # 清空该课目录后重新下载

目标站点：BlackBoard Learn **Classic / Original** 版式（`ultraStatus: "Classic"`）。
换学校只需改 configs/paths.json 的 base_url；学期名映射见 configs/term_map.json。
自建站点的结构差异与自行侦察方法见 docs/blackboard-structure.md。

设计要点（详见 docs/blackboard-structure.md）：
- 课程枚举/内容树走 BB REST 接口（在浏览器内 fetch，复用登录态、绕 CORS）
- 文件真实下载地址需从 displayIndividualContent 包装页里正则抠 /bbcswebdav/...
- 内容区必须遍历 REST 全树：课程左侧菜单可能不显示某些区，纯 DOM 抓取会整区漏掉
- 断点续爬：.crawl_status.json 记课程级状态，文件级靠磁盘存在性判重

依赖：DrissionPage 4.2.x（实测 4.2.0b20；PyPI 无 4.2+ 稳定版，必须精确指定该 beta 版本）
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Windows GBK 终端统一 UTF-8 输出，避免中文乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

from DrissionPage import ChromiumOptions, ChromiumPage
from DrissionPage._functions.tools import port_is_using

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / 'configs' / 'paths.json'
TERM_MAP_PATH = BASE / 'configs' / 'term_map.json'
STATUS_PATH = BASE / '.crawl_status.json'
LOG_DIR = BASE / 'logs'

# BB 内容条目类型（contentHandler.id 去掉前缀）
H_FOLDER = 'resource/x-bb-folder'
H_FILE = 'resource/x-bb-file'
H_DOCUMENT = 'resource/x-bb-document'
H_ASSIGNMENT = 'resource/x-bb-assignment'
H_EXTLINK = 'resource/x-bb-externallink'

# 匹配 /bbcswebdav/pid-123456-dt-content-rid-7890123_1/xid-7890123_1
RE_WEBDav = re.compile(r'/bbcswebdav/pid-(\d+)-dt-content-rid-(\d+)_1/xid-\d+_1')
RE_BAD_NAME = re.compile(r'[\\/:*?"<>|]')

_LOG_FH = None


# ---------------------------------------------------------------- 配置 / 日志

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f'缺少配置文件 {CONFIG_PATH}\n'
            f'请先复制模板：cp configs/paths.example.json configs/paths.json\n'
            f'然后填入你学校的 base_url 和本机浏览器路径。'
        )
    cfg = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    for key in ('browser_path', 'base_url', 'port'):
        if key not in cfg:
            raise SystemExit(f'configs/paths.json 缺少字段: {key}')
    return cfg


def load_term_map() -> dict:
    if not TERM_MAP_PATH.exists():
        return {}
    return json.loads(TERM_MAP_PATH.read_text(encoding='utf-8'))


def log(msg: str) -> None:
    """打印到 stdout，同时追加到本次运行的日志文件。"""
    line = f'{datetime.now():%H:%M:%S} {msg}'
    print(line, flush=True)
    if _LOG_FH:
        _LOG_FH.write(line + '\n')
        _LOG_FH.flush()


def init_log() -> None:
    global _LOG_FH
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f'crawl_{datetime.now():%Y%m%d_%H%M%S}.log'
    _LOG_FH = open(path, 'w', encoding='utf-8')
    log(f'[日志] {path}')


def safe(name: str) -> str:
    """替换 Windows 非法文件名字符，并裁掉结尾的点/空格（Windows 不允许）。"""
    name = RE_BAD_NAME.sub('_', name or '').strip()
    return name.rstrip('. ') or 'unnamed'


# ------------------------------------------------------------------- 浏览器

def kill_profile_instances(profile_path: str) -> None:
    """只杀占用该 profile 的 msedge，不碰日常 Edge。"""
    cmd = (
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{profile_path}*' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    subprocess.run(['powershell', '-Command', cmd], capture_output=True)


def wait_port_free(port: int, timeout: float = 10.0) -> None:
    """轮询直到端口完全释放（杀进程后避免端口竞态）。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not port_is_using('127.0.0.1', port):
            return
        time.sleep(0.5)


def launch(cfg: dict) -> ChromiumPage:
    """杀干净 profile 实例，等端口释放，冷启动 Edge 并连接。"""
    profile = (BASE / cfg['profile_path']).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    kill_profile_instances(str(profile))
    wait_port_free(cfg['port'])
    co = ChromiumOptions()
    co.set_browser_path(cfg['browser_path'])
    co.set_user_data_path(str(profile))
    co.set_local_port(cfg['port'])
    co.disable_pdf_preview()  # 4.2+ API：PDF 直接下载不预览
    return ChromiumPage(co)


# --------------------------------------------------------------- BB REST 调用

def api_fetch(page: ChromiumPage, path: str, timeout: float = 40.0) -> dict | None:
    """在浏览器内异步 fetch 指定路径，轮询取回 {status, text}。

    用「发起 fetch → 结果写进 window 变量 → 轮询读回」的模式，
    因为 DrissionPage 的 run_js 不 await Promise，而同步 XHR 已被 Chrome 禁用。
    """
    js = (
        'window.__bb_res = undefined;'
        f'fetch({json.dumps(path)}, {{credentials: "include"}})'
        '.then(function(r){ return r.text().then(function(t){'
        '  window.__bb_res = {status: r.status, text: t}; }); })'
        '.catch(function(e){ window.__bb_res = {status: -1, text: String(e)}; });'
    )
    try:
        page.run_js(js)
    except Exception as e:
        log(f'    [warn] 注入 fetch 失败: {e}')
        return None

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            res = page.run_js('return window.__bb_res === undefined ? null : window.__bb_res')
        except Exception:
            res = None
        if res:
            return res
        time.sleep(0.15)
    log(f'    [warn] API 超时: {path[:90]}')
    return None


def api_json(page: ChromiumPage, path: str) -> dict | None:
    """调 REST 接口并解析 JSON。"""
    res = api_fetch(page, path)
    if not res:
        return None
    if res.get('status') != 200:
        return None
    try:
        return json.loads(res['text'])
    except Exception:
        return None


def api_text(page: ChromiumPage, path: str) -> str | None:
    """取原始文本（用于抠 HTML 里的下载链接）。"""
    res = api_fetch(page, path)
    if not res or res.get('status') != 200:
        return None
    return res['text']


# ------------------------------------------------------------------- 登录态

def is_logged_in(page: ChromiumPage) -> bool:
    """调 /users/me：200 即登录有效（比数链接数可靠）。"""
    res = api_fetch(page, '/learn/api/public/v1/users/me', timeout=15)
    return bool(res and res.get('status') == 200)


def ensure_logged_in(page: ChromiumPage, base_url: str, timeout: float = 240) -> None:
    """打开 BB 首页；未登录则点 LOGIN 触发 ADFS SSO，等人手动登录完成。"""
    page.get(base_url + '/')
    time.sleep(3)
    if is_logged_in(page):
        log('[登录] 已登录（profile 登录态持久化生效）')
        return
    log(f'[登录] 未登录 —— 请在打开的浏览器窗口里完成 {base_url} 的登录，最多等 '
        f'{int(timeout)} 秒')
    for sel in ("css:input[name='login']", 'css:a[href*="login"]'):
        btn = page.ele(sel, timeout=3)
        if btn:
            try:
                btn.click()
            except Exception:
                pass
            break
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(5)
        if is_logged_in(page):
            log('[登录] 登录成功')
            return
    raise RuntimeError('登录超时，请检查浏览器窗口')


# --------------------------------------------------------------- 课程 / 学期

def list_courses(page: ChromiumPage) -> list[dict]:
    """枚举账号下全部课程（REST，自动分页）。"""
    out, offset = [], 0
    while True:
        data = api_json(page,
                        f'/learn/api/public/v1/users/me/courses'
                        f'?expand=term,course&limit=200&offset={offset}')
        if not data:
            break
        out.extend(data.get('results') or [])
        paging = data.get('paging') or {}
        if not paging.get('nextPage'):
            break
        offset += 200
    return out


def list_terms(page: ChromiumPage) -> dict:
    """termId -> {name, start, end}。"""
    data = api_json(page, '/learn/api/public/v1/terms?limit=200')
    table = {}
    for t in (data or {}).get('results') or []:
        dur = (t.get('availability') or {}).get('duration') or {}
        table[t['id']] = {'name': t.get('name') or '',
                          'start': dur.get('start'), 'end': dur.get('end')}
    return table


def term_to_folder(term_id: str, term_name: str, term_map: dict) -> str:
    """学期 → 中文文件夹名。

    识别格式 YY + T + 0 + [PG|UG]（如 9910PG = AY2099-00 Term 1）：
      T=1 秋季 → 20YY年下学期；T=2 春季 → 20(YY+1)年上学期；T=5 夏季 → 20(YY+1)年夏季学期
    命中 term_map 的手工覆盖则优先用覆盖值；无法识别返回 '未分类'。
    """
    over = term_map.get('by_term_id', {})
    if term_id in over:
        return over[term_id]
    over2 = term_map.get('by_term_name', {})
    if term_name in over2:
        return over2[term_name]

    m = re.match(r'^\s*(\d{2})\s*(\d)\s*0', term_name or '')  # 容忍 "2020 PG" 里的空格
    if not m:
        return '未分类'
    yy, t = int(m.group(1)), int(m.group(2))
    year = 2000 + yy
    if t == 1:
        return f'{year}年下学期'
    if t == 2:
        return f'{year + 1}年上学期'
    if t == 5:
        return f'{year + 1}年夏季学期'
    return '未分类'


# ------------------------------------------------------------------- 内容树

def walk_course_tree(page: ChromiumPage, course_id: str) -> list[dict]:
    """递归取回整门课的内容树，返回扁平化的 [(相对路径, 条目)]。"""
    base = f'/learn/api/public/v1/courses/{course_id}/contents'
    flat: list[dict] = []

    def recurse(parent_id: str | None, path: list[str], depth: int = 0) -> None:
        url = (f'{base}/{parent_id}/children?limit=200' if parent_id
               else f'{base}?limit=200')
        data = api_json(page, url)
        if not data:
            log(f'    [warn] 取内容失败: {"/".join(path) or "(根)"}')
            return
        for item in data.get('results') or []:
            title = (item.get('title') or '').strip()
            handler = ((item.get('contentHandler') or {}).get('id')) or ''
            entry = {'item': item, 'path': path, 'title': title,
                     'handler': handler, 'id': item.get('id')}
            flat.append(entry)
            if item.get('hasChildren') and depth < 12:
                recurse(item.get('id'), path + [title], depth + 1)

    recurse(None, [])
    return flat


def resolve_download_urls(page: ChromiumPage, base_url: str, course_id: str,
                          content_id: str) -> list[str]:
    """从 displayIndividualContent 包装页里抠出该条目的 /bbcswebdav 下载地址。

    只保留 pid 与条目 id 数字段一致的链接，避免误收同页其他附件。
    """
    html = api_text(page, f'/webapps/blackboard/execute/displayIndividualContent'
                          f'?course_id={course_id}&content_id={content_id}')
    if not html:
        return []
    want = content_id.strip('_').split('_')[0]
    urls, seen = [], set()
    for pid, rid in RE_WEBDav.findall(html):
        if pid != want:
            continue
        full = f'/bbcswebdav/pid-{pid}-dt-content-rid-{rid}_1/xid-{rid}_1'
        if full not in seen:
            seen.add(full)
            urls.append(base_url + full)
    return urls


def item_page_text(page: ChromiumPage, course_id: str, content_id: str) -> str:
    """取条目页正文（作业说明等），剥掉 JS 噪音。"""
    html = api_text(page, f'/webapps/blackboard/execute/displayIndividualContent'
                         f'?course_id={course_id}&content_id={content_id}')
    if not html:
        return ''
    # 去掉 <script>/<style>，其余标签剥离
    html = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', html, flags=re.S | re.I)
    m = re.search(r'<div[^>]*id="content"[^>]*>(.*?)</div>\s*(?:<div|</div)', html,
                  flags=re.S | re.I)
    body = m.group(1) if m else html
    text = re.sub(r'<[^>]+>', ' ', body)
    text = (text.replace('&nbsp;', ' ').replace('&amp;', '&')
                .replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"'))
    return re.sub(r'[ \t]{2,}', ' ', re.sub(r'\n\s*\n+', '\n', text)).strip()


# ------------------------------------------------------------------- 下载

def download_file(page: ChromiumPage, url: str, dest_dir: Path, want_name: str | None,
                  force: bool = False) -> Path | None:
    """用浏览器下载（绕过 PDF 预览）。

    命名策略：浏览器会按服务端 Content-Disposition 存成真实文件名，这对
    document/assignment 这类 REST 不给 fileName 的条目是唯一可靠来源；
    仅当 REST 明确给了 fileName（file 类型）时才改名对齐。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    if want_name and not force:
        target = dest_dir / safe(want_name)
        if target.exists() and target.stat().st_size > 0:
            log(f'      [跳过] 已存在 {target.name}')
            return target
    try:
        mission = page.download.by_browser(url, save_path=str(dest_dir))
        final = mission.wait(show=False, timeout=90)
    except Exception as e:
        log(f'      [失败] 下载异常: {e}')
        return None
    if not final:
        log(f'      [失败] 未下载: {url[-60:]}')
        return None
    got = Path(final)
    if not got.exists() or got.stat().st_size == 0:
        log(f'      [失败] 空文件: {got.name}')
        return None
    if want_name:
        target = dest_dir / safe(want_name)
        if got != target:
            if target.exists():
                target.unlink()
            got.rename(target)
            got = target
    log(f'      [完成] {got.name} ({got.stat().st_size / 1024:.0f} KB)')
    return got


# --------------------------------------------------------------- 单门课下载

def download_course(page: ChromiumPage, cfg: dict, course: dict, term_folder: str,
                    force: bool = False, dry_run: bool = False) -> dict:
    """下载一门课，返回统计信息。"""
    course_id = course['courseId']
    name = (course.get('course') or {}).get('name') or course_id
    dest_root = (BASE / cfg['download_root']).resolve() / term_folder / safe(name)

    # --force 必须先清空本课目录：否则浏览器下载遇到同名会加 _1/_2 后缀，留下重复副本。
    # 只允许删 download_root 之下的目录，防误删。
    if force and dest_root.exists():
        root = (BASE / cfg['download_root']).resolve()
        if root in dest_root.parents:
            log(f'     [--force] 清空旧目录重下: {dest_root.name}')
            shutil.rmtree(dest_root, ignore_errors=True)
        else:
            log(f'     [warn] 拒绝清空 {dest_root}（不在下载根目录下）')

    stat = {'course_id': course_id, 'name': name, 'term_folder': term_folder,
            'files': 0, 'bytes': 0, 'unknown': [], 'assignments': []}

    log(f'  ── {name}  [{course_id}]')
    flat = walk_course_tree(page, course_id)
    log(f'     内容树: {len(flat)} 个条目')

    for entry in flat:
        handler, title, cid = entry['handler'], entry['title'], entry['id']
        rel = [safe(p) for p in entry['path']]
        dest = dest_root
        for part in rel:
            dest = dest / part

        if handler == H_FOLDER:
            if not dry_run:
                dest.mkdir(parents=True, exist_ok=True)
            continue

        if handler == H_EXTLINK:
            # 外链只记录，不下载（如 zoom 会议室链接）
            if not dry_run:
                dest.mkdir(parents=True, exist_ok=True)
                with open(dest / '_外链.md', 'a', encoding='utf-8') as f:
                    url = (entry['item'].get('contentHandler') or {}).get('url') or ''
                    f.write(f'- {title}: {url}\n')
            continue

        if handler in (H_FILE, H_DOCUMENT, H_ASSIGNMENT):
            if dry_run:
                log(f'     [dry] {"/".join(rel)}/{title} ({handler.split("-")[-1]})')
                stat['files'] += 1
                continue
            urls = resolve_download_urls(page, cfg['base_url'], course_id, cid)
            if not urls:
                if handler == H_ASSIGNMENT:
                    # 作业没挂附件是正常情况，计进作业清单，不算「未识别」
                    body = item_page_text(page, course_id, cid)
                    stat['assignments'].append({'title': title, 'path': '/'.join(rel),
                                                'attachments': 0})
                    if len(body) > 80 and not dry_run:
                        dest.mkdir(parents=True, exist_ok=True)
                        (dest / f'{safe(title)}_作业要求.md').write_text(
                            f'# {title}\n\n{body}\n', encoding='utf-8')
                else:
                    stat['unknown'].append(f'{"/".join(rel)}/{title} ({handler}) 无下载链接')
                continue
            fname = ((entry['item'].get('contentHandler') or {})
                     .get('file') or {}).get('fileName')
            if handler == H_ASSIGNMENT:
                stat['assignments'].append({'title': title, 'path': '/'.join(rel),
                                            'attachments': len(urls)})
                # 作业要求正文（多数课程为空，仅记录有内容的）
                body = item_page_text(page, course_id, cid)
                if len(body) > 80:
                    dest.mkdir(parents=True, exist_ok=True)
                    (dest / f'{safe(title)}_作业要求.md').write_text(
                        f'# {title}\n\n{body}\n', encoding='utf-8')
            for i, url in enumerate(urls):
                # 只有 REST 明确给了 fileName 才改名，否则保留浏览器存的真名
                want = fname if (fname and i == 0) else None
                got = download_file(page, url, dest, want, force=force)
                if got:
                    stat['files'] += 1
                    stat['bytes'] += got.stat().st_size
            continue

        # 其他类型：不静默丢弃，记进清单
        stat['unknown'].append(f'{"/".join(rel)}/{title} ({handler or "无类型"})')

    # 作业清单索引
    if stat['assignments'] and not dry_run:
        lines = [f'# {name} — 作业清单', '',
                 f'> 抓取时间 {datetime.now():%Y-%m-%d %H:%M}', '']
        for a in stat['assignments']:
            lines.append(f'- **{a["title"]}** — `{a["path"]}/`，附件 {a["attachments"]} 个')
        idx_dir = dest_root
        idx_dir.mkdir(parents=True, exist_ok=True)
        (idx_dir / '_作业清单.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')

    # 未识别条目清单（这次没有就删掉上次留下的，避免陈旧告警）
    if not dry_run:
        dest_root.mkdir(parents=True, exist_ok=True)
        flag = dest_root / '_未识别条目.md'
        if stat['unknown']:
            body = '\n'.join(f'- {u}' for u in stat['unknown'])
            flag.write_text(f'# 未识别/未下载条目\n\n以下条目类型未处理，请人工确认：\n\n{body}\n',
                            encoding='utf-8')
        elif flag.exists():
            flag.unlink()

    extra = f', 未识别 {len(stat["unknown"])} 条' if stat['unknown'] else ''
    log(f'     完成: {stat["files"]} 文件, {stat["bytes"] / 1048576:.1f} MB{extra}')
    return stat


# ------------------------------------------------------------------- 状态

def load_status() -> dict:
    if STATUS_PATH.exists():
        try:
            return json.loads(STATUS_PATH.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {'per_course': {}}


def save_status(status: dict, courses_total: int) -> None:
    """落盘状态。完成数只统计本次计划内的课程（--course 单跑时不会被历史成绩干扰）。"""
    scope = status.get('_all_ids') or list(status['per_course'])
    done = [i for i in scope if status['per_course'].get(i, {}).get('ok')]
    names = status.get('_names') or {}
    status.update({
        'finished_at': f'{datetime.now():%Y-%m-%d %H:%M:%S}',
        'courses_total': courses_total,
        'courses_done': len(done),
        'courses_remaining': [names.get(i, i) for i in scope if i not in done],
    })
    STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2),
                           encoding='utf-8')


# ------------------------------------------------------------------- 主流程

def main() -> None:
    ap = argparse.ArgumentParser(description='BlackBoard 全课程批量下载器')
    ap.add_argument('--dry-run', action='store_true', help='只列课程与学期映射，不下载')
    ap.add_argument('--course', action='append', default=None,
                    help='只跑指定 course_id（可重复）')
    ap.add_argument('--force', action='store_true',
                    help='清空该课目录后重新下载（避免同名文件残留成重复副本）')
    args = ap.parse_args()

    cfg = load_config()
    term_map = load_term_map()
    init_log()

    page = None
    status = load_status()
    try:
        page = launch(cfg)
        log(f'[浏览器] 已启动 (port {cfg["port"]})')
        ensure_logged_in(page, cfg['base_url'], cfg.get('login_timeout', 240))

        terms = list_terms(page)
        courses = list_courses(page)
        log(f'[课程] 共 {len(courses)} 门')

        # 组装计划
        plan = []
        for c in courses:
            cid = c['courseId']
            cname = (c.get('course') or {}).get('name') or cid
            tid = (c.get('course') or {}).get('termId') or ''
            tname = (terms.get(tid) or {}).get('name') or ''
            folder = term_to_folder(tid, tname, term_map)
            plan.append({'courseId': cid, 'name': cname, 'term_id': tid,
                         'term_name': tname, 'folder': folder, 'raw': c})

        plan_all = plan  # 全量清单：状态文件始终按全量记录，不受 --course 过滤影响
        if args.course:
            plan = [p for p in plan_all if p['courseId'] in set(args.course)]

        # 学期映射总览（dry-run 时尤其重要）
        log('')
        log('=== 课程 → 学期映射 ===')
        for p in sorted(plan, key=lambda x: (x['folder'], x['name'])):
            cur = '✓' if (status['per_course'].get(p['courseId'], {}).get('ok')
                          and not args.force) else ' '
            log(f'  [{cur}] {p["folder"]} / {p["name"]}   (term={p["term_name"] or "?"})')
        log('')

        if args.dry_run:
            log('[dry-run] 不下载任何文件，结束')
            return

        status['_all_ids'] = [p['courseId'] for p in plan_all]
        status['_names'] = {p['courseId']: p['name'] for p in plan_all}

        for p in plan:
            prev = status['per_course'].get(p['courseId'])
            if prev and prev.get('ok') and not args.force:
                log(f'  ── {p["name"]}  已完成，跳过（--force 可重跑）')
                continue
            if not is_logged_in(page):
                log('[登录] 登录态失效，等待重新登录...')
                ensure_logged_in(page, cfg['base_url'], cfg.get('login_timeout', 240))
            try:
                stat = download_course(page, cfg, p['raw'], p['folder'],
                                       force=args.force)
                stat['ok'] = True
                stat['term_name'] = p['term_name']
                status['per_course'][p['courseId']] = stat
            except Exception as e:
                log(f'  !! {p["name"]} 失败: {e}')
                status['per_course'][p['courseId']] = {
                    'ok': False, 'name': p['name'], 'error': str(e)}
            save_status(status, len(plan_all))

        status['status'] = 'all_done'
        save_status(status, len(plan_all))
        done_here = sum(1 for p in plan
                        if status['per_course'].get(p['courseId'], {}).get('ok'))
        log('')
        log(f'=== 本次完成 {done_here}/{len(plan)} 门 | 累计 '
            f'{status["courses_done"]}/{status["courses_total"]} ===')
    except KeyboardInterrupt:
        status['status'] = 'interrupted'
        save_status(status, status.get('courses_total', 0))
        log('\n[中断] 已保存进度，重跑将从断点继续')
    except Exception as e:
        status['status'] = 'error'
        save_status(status, status.get('courses_total', 0))
        log(f'\n[错误] {e}')
        raise
    finally:
        if page:
            try:
                page.disconnect()
            except Exception:
                pass
        if _LOG_FH:
            _LOG_FH.close()


if __name__ == '__main__':
    main()
