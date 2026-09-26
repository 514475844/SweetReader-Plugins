# -*- coding: utf-8 -*-
"""cool18 抓取插件 - 实现层（由壳 cool18_grabber.py 按 mtime 热加载）。
本文件名以 _ 开头，不会被插件扫描器直接加载。
更新本文件后无需重启容器：刷新插件页即生效。
"""
import os
import re
import json
import time
import html as htmllib
import traceback
import urllib.request
import urllib.parse
from collections import OrderedDict
from datetime import datetime

from flask import current_app

from app.models import Book, Category, db
from app.plugins import registry

# ---------------- 繁->简转换（OpenCC 内嵌表） ----------------
_T2S_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_cool18_t2s.py')
try:
    _t2s = {}
    exec(compile(open(_T2S_PATH, encoding='utf-8').read(), _T2S_PATH, 'exec'), _t2s)
    T2S_CHARS = _t2s['T2S_CHARS']
    T2S_PHRASES = _t2s['T2S_PHRASES']
    _TBL = str.maketrans(T2S_CHARS)
except Exception:
    T2S_CHARS, T2S_PHRASES, _TBL = {}, {}, str.maketrans({})


def t2s(s):
    """繁体->简体（先短语后单字）"""
    if not s:
        return s
    for k, v in T2S_PHRASES.items():
        if k in s:
            s = s.replace(k, v)
    return s.translate(_TBL)


# 转载/分享类前缀（避免被当成书名）
_PREFIX_RE = re.compile(
    r"^(转载|轉載|转贴|轉貼|分享|推荐|推薦|搬运|搬運|添加|求文|找文|寻文|尋文|转让|好文|收藏|转)\s*"
    r"[:：,，、\-—_\s]*\s*(一篇|几篇|几篇好文|一个好文)?\s*[:：,，、\-—_\s]*")

# ============================================================
# 抓取核心（自包含，无第三方依赖）
# ============================================================
BASE = "https://www.cool18.com"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

_PRE_RE = re.compile(r"<pre[^>]*>(.*?)</pre>", re.S | re.I)
_A_RE = re.compile(r"<a\s[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.S | re.I)
_TAG_SPAN_RE = re.compile(r"<span[^>]*class='list-type-show'[^>]*>(.*?)</span>", re.S)
_TITLE_RE = re.compile(r"[【〔\[［]([^】〕\]］]+)[】〕\]］]")
_RANGE_RE = re.compile(r"[（(]\s*(\d+)\s*[-－~至到]\s*(\d+)\s*[）)]")
_SINGLE_RE = re.compile(r"[（(]\s*(\d+)\s*[）)]")
_AUTHOR_RE = re.compile(r"(?:原作者|作\s*者|作)\s*[:：]?\s*(\S{1,20})")
_WS_TBL = dict.fromkeys(map(ord, "　 \t\r\n"), None)
_FULL2HALF = {0xFF10 + i: str(i) for i in range(10)}

_AD_PAT = re.compile(
    r"(发表于|字数[：:]|本帖最后|版权|转载请注明|更多精彩|请点击|免费观看|永久域名|最新地址|"
    r"网址[：:]?|论坛地址|备用域名|广告位|招商|友情链接|合作站点|推广|"
    r"QQ[：:]?\s*\d|微信[：:]?\s*\w+|扫码|二维码|"
    r"\.(?:com|net|cc|me|xyz|top|vip|info|org)\b|www\.|https?://|"
    r"^[-=*_·——\s]+$)", re.I)
_AD_FULL = re.compile(r"(福[利利][社院]|楼[凤凤]|^【广告】|点此进入|收藏本站)", re.I)


def _fetch(url, timeout=30, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            raw = urllib.request.urlopen(req, timeout=timeout).read()
            for enc in ("utf-8", "big5", "gb18030"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", "ignore")
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def _parse_page_data(body):
    m = re.search(r"const\s+_PageData\s*=", body)
    if m:
        j = body.index("[", m.end())
        data, _ = json.JSONDecoder().raw_decode(body[j:])
        return data
    try:
        data = json.loads(body)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def list_threads(pages=1, aifilter=True, log=None, alive=None):
    body = _fetch(BASE + "/bbs4/index.php")
    data = _parse_page_data(body)
    threads = [d for d in data if str(d.get("rootid", "0")) == "0" and d.get("tid")]
    if log:
        log("列表第 1 页：%d 帖" % len(threads))
    for p in range(2, pages + 1):
        if alive is not None and not alive():
            break
        if not threads:
            break
        mtid = min(int(t["tid"]) for t in threads)
        try:
            body = _fetch(BASE + "/bbs4/index.php?app=forum&act=ajax&mtid=%d&aifilter=%d"
                          % (mtid, 1 if aifilter else 0))
            more = _parse_page_data(body)
            new_roots = [d for d in more if str(d.get("rootid", "0")) == "0" and d.get("tid")]
            known = {t["tid"] for t in threads}
            add = [t for t in new_roots if t["tid"] not in known]
            if log:
                log("列表第 %d 页：+%d 帖" % (p, len(add)))
            if not add:
                break
            threads += add
            time.sleep(0.5)
        except Exception as e:
            if log:
                log("第 %d 页抓取失败：%s" % (p, e))
            break
    return threads


def parse_subject(raw):
    s = htmllib.unescape(raw or "")
    # 繁体先转简体，保证书名/作者/分类解析一致
    s = t2s(s)
    cat = ""
    m = _TAG_SPAN_RE.search(s)
    if m:
        cat = re.sub(r"<[^>]+>", "", m.group(1)).strip("『』")
        s = _TAG_SPAN_RE.sub("", s)
    s = re.sub(r"<[^>]+>", "", s).strip().translate(_FULL2HALF)
    # 去掉转载/分享类前缀，避免被当成书名
    while True:
        s2 = _PREFIX_RE.sub("", s)
        if s2 == s:
            break
        s = s2.strip()
    ch_start = ch_end = None
    m = _RANGE_RE.search(s)
    if m:
        ch_start, ch_end = int(m.group(1)), int(m.group(2))
    else:
        m = _SINGLE_RE.search(s)
        if m:
            ch_start = ch_end = int(m.group(1))
    if m:
        s = s[:m.start()] + s[m.end():]
    author = ""
    ma = _AUTHOR_RE.search(s)
    if ma:
        author = re.sub(r"\[[^\]]*$", "", ma.group(1)).strip("[]（）()：: ，,")
        s = s[:ma.start()] + s[ma.end():]
    mt = _TITLE_RE.search(s)
    title = mt.group(1).strip() if mt else s.strip(" 【】［］[]（）()　")
    key = re.sub(r"[\s　]+", "", title).lower().translate(_WS_TBL)
    if author:
        key += "|" + re.sub(r"[\s　]+", "", author)
    return {"title": title, "author": author, "cat": cat,
            "ch_start": ch_start, "ch_end": ch_end, "key": key}


def _related_tids(pre_html, self_tid):
    tids = set()
    for m in _A_RE.finditer(pre_html):
        href, txt = m.group(1), re.sub(r"<[^>]+>", "", m.group(2))
        if "threadview" not in href:
            continue
        tm = re.search(r"tid=(\d+)", href)
        if tm and (_TITLE_RE.search(txt) or _RANGE_RE.search(txt.translate(_FULL2HALF))
                   or _SINGLE_RE.search(txt.translate(_FULL2HALF))):
            tids.add(tm.group(1))
    tids.discard(str(self_tid))
    return tids


def clean_content(pre_html):
    t = re.sub(r"<br\s*/?>", "\n", pre_html, flags=re.I)
    t = re.sub(r"</p\s*>", "\n", t, flags=re.I)
    t = _A_RE.sub(lambda m: "\x00" if "threadview" in m.group(1) else m.group(2), t)
    t = re.sub(r"<[^>]+>", "", t)
    for k, v in {"&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'"}.items():
        t = t.replace(k, v)
    t = htmllib.unescape(t)
    out, blank = [], 0
    for ln in t.split("\n"):
        ln = ln.replace("\x00", "").strip()
        if not ln:
            blank += 1
            if blank > 1:
                continue
            out.append("")
            continue
        blank = 0
        if _AD_PAT.search(ln) and len(ln) < 120:
            continue
        if _AD_FULL.search(ln):
            continue
        out.append(ln)
    return "\n".join(out).strip()


def fetch_thread(tid):
    url = "%s/bbs4/index.php?app=forum&act=threadview&tid=%s" % (BASE, tid)
    body = _fetch(url)
    m = re.search(r"<title>(.*?)</title>", body, re.S)
    subject = m.group(1).split(" - ")[0].strip() if m else ""
    mp = _PRE_RE.search(body)
    pre = mp.group(1) if mp else ""
    return {"tid": str(tid), "subject": subject, "pre_html": pre,
            "text": clean_content(pre), "related": _related_tids(pre, tid)}


def collect_story_tids(seed_tids, known=(), rounds=2, log=None, alive=None):
    all_tids = OrderedDict((str(t), True) for t in list(seed_tids) + list(known))
    frontier = [t for t in all_tids if t not in known]
    for r in range(rounds):
        nxt = []
        for tid in frontier:
            if alive is not None and not alive():
                return list(all_tids)
            try:
                th = fetch_thread(tid)
                for rel in th["related"]:
                    if rel not in all_tids:
                        all_tids[rel] = True
                        nxt.append(rel)
                time.sleep(0.6)
            except Exception as e:
                if log:
                    log("  帖 %s 关联发现失败：%s" % (tid, e))
        if log and nxt:
            log("  关联发现第 %d 轮：+%d 帖" % (r + 1, len(nxt)))
        frontier = nxt
        if not frontier:
            break
    return list(all_tids)


def assemble_parts(tids, delay=0.6, max_parts=60, log=None, alive=None):
    parts = []
    for i, tid in enumerate(tids):
        if alive is not None and not alive():
            break
        try:
            th = fetch_thread(tid)
        except Exception as e:
            if log:
                log("  帖 %s 抓取失败：%s" % (tid, e))
            continue
        info = parse_subject(th["subject"])
        parts.append({"tid": tid, "subject": th["subject"], "text": th["text"],
                      "no": info["ch_start"]})
        if log and (i + 1) % 10 == 0:
            log("  已抓 %d/%d 帖" % (i + 1, len(tids)))
        time.sleep(delay)
        if len(parts) >= max_parts:
            break
    parts.sort(key=lambda p: (p["no"] if p["no"] is not None else 10 ** 9, int(p["tid"])))
    seen, dedup = set(), []
    for p in parts:
        sig = re.sub(r"\s+", "", p["text"])[:400]
        if not sig or sig in seen:
            continue
        seen.add(sig)
        dedup.append(p)
    return dedup


# ---------------- 站内搜索 ----------------

def list_by_cat(cat, pages=20, log=None, alive=None):
    """走站点分类专用搜索接口（type=分类），rel=next 翻页，返回 [{tid,subject}]"""
    q = urllib.parse.urlencode({'action': 'search', 'act': 'threadsearch', 'app': 'forum',
                                'type': cat, 'submit': '查询'})
    url = "%s/bbs4/index.php?%s" % (BASE, q)
    out, seen = [], set()
    page = 1
    while url and page <= pages:
        if alive is not None and not alive():
            break
        body = _fetch(url)
        n0 = len(out)
        for m in _A_RE.finditer(body):
            href, txt = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if 'threadview' not in href or not txt:
                continue
            tm = re.search(r"tid=(\d+)", href)
            if not tm or tm.group(1) in seen:
                continue
            seen.add(tm.group(1))
            out.append({'tid': tm.group(1), 'subject': txt})
        if log:
            log('分类「%s」第 %d 页：+%d 帖' % (cat, page, len(out) - n0))
        nm = re.search(r'rel="next"[^>]*?href="([^"]+)"', body) or \
             re.search(r'rel=[\'"]next[\'"][^>]*?href=[\'"]([^\'"]+)[\'"]', body) or \
             re.search(r'href="([^"]+)"[^>]*?rel="next"', body)
        if nm:
            nxt = htmllib.unescape(nm.group(1))
            url = nxt if nxt.startswith('http') else (BASE + '/bbs4/' + nxt.lstrip('/'))
            page += 1
            time.sleep(0.5)
        else:
            url = None
    return out


def search_novels(kw, limit=60):
    q = urllib.parse.urlencode({'action': 'search', 'bbsdr': 'bbs4', 'act': 'threadsearch',
                                'app': 'forum', 'keywords': kw})
    body = _fetch("%s/bbs4/index.php?%s" % (BASE, q))
    items, seen = [], set()
    for m in _A_RE.finditer(body):
        href, txt = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if "threadview" not in href or not txt:
            continue
        tm = re.search(r"tid=(\d+)", href)
        if not tm or tm.group(1) in seen:
            continue
        seen.add(tm.group(1))
        info = parse_subject(txt)
        items.append({'tid': tm.group(1), 'subject': txt, 'title': info['title'],
                      'author': info['author'], 'cat': info['cat'],
                      'ch': info['ch_start']})
        if len(items) >= limit:
            break
    # 持久化搜索结果，供下载任务取标题（合并分组的依据）
    try:
        path = _search_cache_path()
        cache = {}
        try:
            cache = json.load(open(path, encoding='utf-8'))
        except Exception:
            cache = {}
        for it in items:
            cache[it['tid']] = it['subject']
        json.dump(cache, open(path, 'w', encoding='utf-8'), ensure_ascii=False)
    except Exception:
        pass
    return items


# ============================================================
# 状态持久化与入库
# ============================================================

def _state_path():
    return os.path.join(current_app.instance_path, "cool18_state.json")


def load_state():
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            st = json.load(f)
            st.setdefault("stories", {})
            st.setdefault("imported_tids", [])
            return st
    except Exception:
        return {"stories": {}, "imported_tids": []}


def save_state(st):
    with open(_state_path(), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


def _search_cache_path():
    return os.path.join(current_app.instance_path, "cool18_search.json")


def root_cat_name():
    cfg = registry.get_settings('cool18_grabber')
    return (cfg.get('category_root') or 'cool18').strip() or 'cool18'


def _get_category(name, root_name):
    root = Category.query.filter(Category.path == root_name).first()
    if not root:
        root = Category(name=root_name, path=root_name, level=0, book_count=0)
        db.session.add(root)
        db.session.flush()
    if not name or name == root_name:
        return root
    full = "%s/%s" % (root_name, name)
    cat = Category.query.filter(Category.path == full).first()
    if not cat:
        cat = Category(name=name, path=full, parent_id=root.id, level=1, book_count=0)
        db.session.add(cat)
        db.session.flush()
    return cat


def _initial_of(title):
    try:
        from pypinyin import lazy_pinyin
        c = lazy_pinyin(title[:1])[0][:1].upper()
        return c if c.isalpha() else 'Other'
    except Exception:
        c = title[:1].upper()
        return c if c.isalpha() else 'Other'


def _safe_name(s):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', (s or '').strip())[:80] or '未命名'


def _books_dir():
    return current_app.config.get('BOOKS_DIR', '/app/books')


def _write_book_file(story, text, cat_name):
    folder = os.path.join(_books_dir(), _safe_name(root_cat_name()), _safe_name(cat_name or '未分类'))
    os.makedirs(folder, exist_ok=True)
    fn = '《%s》%s.txt' % (_safe_name(story['title']), ('-' + _safe_name(story['author'])) if story['author'] else '')
    full = os.path.join(folder, fn)
    with open(full, 'w', encoding='utf-8') as f:
        f.write(text)
    return full, os.path.relpath(full, _books_dir())


def _upsert_book(story, text, cat, rel_path, full_path):
    book = Book.query.filter_by(filename=os.path.basename(full_path)).first()
    created = book is None
    if not book:
        book = Book(filename=os.path.basename(full_path))
        db.session.add(book)
    book.title = story['title']
    book.author = story['author'] or '佚名'
    book.description = (story.get('summary') or '')[:500]
    book.file_type = 'txt'
    book.file_size = len(text.encode('utf-8'))
    book.relative_path = rel_path
    book.category_id = cat.id
    book.tags = 'cool18'
    book.modified_time = datetime.now()
    book.upload_date = book.upload_date or datetime.now()
    book.metadata_parsed = True
    book.initial = _initial_of(story['title'])
    db.session.flush()
    if created:
        cat.book_count = (cat.book_count or 0) + 1
    db.session.commit()
    return book, created


def _build_text(story, parts):
    buf = ['《%s》' % story['title']]
    if story.get('author'):
        buf.append('作者：%s' % story['author'])
    buf.append('')
    for p in parts:
        buf.append('=' * 12 + ' ' + (p['subject'] or '') + ' ' + '=' * 12)
        buf.append(p['text'])
        buf.append('')
    return '\n'.join(buf)


# ============================================================
# 后台任务
# ============================================================

def _jlog(JOB, msg):
    stamp = datetime.now().strftime('%H:%M:%S')
    JOB['log'].append('[%s] %s' % (stamp, msg))
    JOB['log'] = JOB['log'][-300:]


def _import_story(JOB, s, st, cfg, imported_tids, root):
    """导入单个作品（s: {key,title,author,cat,tids})；成功返回 True"""
    delay = float(cfg.get('delay', 0.6) or 0.6)
    max_parts = int(cfg.get('max_parts', 60) or 60)
    min_size = int(cfg.get('min_size', 2000) or 0)
    _jlog(JOB, '导入《%s》(作者:%s 分类:%s)…' % (s['title'][:26], s['author'] or '?', s['cat'] or '未分类'))
    seed = [t for t in s['tids'] if t not in imported_tids] or s['tids']
    tids = collect_story_tids(seed, rounds=2, log=lambda m: _jlog(JOB, m),
                              alive=lambda: JOB['running'])
    # 站内搜索补全：把搜索到的同书分帖也并进来（解决分帖互链不全导致的缺章）
    try:
        extra = []
        known = set(tids)
        tnorm = re.sub(r'[\s　]+', '', s['title']).lower()
        for it in search_novels(s['title'], limit=40):
            i2 = parse_subject(it['subject'])
            same_title = re.sub(r'[\s　]+', '', i2['title']).lower() == tnorm
            if not same_title:
                continue
            same_author = (not i2['author'] or not s['author'] or i2['author'] == s['author'])
            if same_author and it['tid'] not in known:
                known.add(it['tid'])
                extra.append(it['tid'])
        if extra:
            _jlog(JOB, '  搜索补全：+%d 个分帖' % len(extra))
            tids = collect_story_tids(extra, known=tids, rounds=1,
                                      log=lambda m: _jlog(JOB, m), alive=lambda: JOB['running'])
    except Exception:
        pass
    parts = assemble_parts(tids, delay=delay, max_parts=max_parts,
                           log=lambda m: _jlog(JOB, m), alive=lambda: JOB['running'])
    text = _build_text(s, parts)
    if bool(cfg.get('j2s', True)):
        text = t2s(text)
    plain = re.sub(r'\s+', '', text)
    if len(plain) < min_size:
        _jlog(JOB, '  字数过少(%d)，跳过' % len(plain))
        return False
    cat = _get_category(s['cat'] or '', root)
    full, rel = _write_book_file(s, text, s['cat'] or '')
    book, created = _upsert_book(s, text, cat, rel, full)
    _jlog(JOB, '  √ %s book#%s：%d 字，%d 帖 → %s' %
          ('入库' if created else '更新', book.id, len(text), len(parts), rel))
    st['stories'][s['key']] = {'book_id': book.id, 'title': s['title'],
                               'tids': [p['tid'] for p in parts],
                               'max_no': max([p['no'] or 0 for p in parts]),
                               'cat': s['cat'] or ''}
    st['imported_tids'] = sorted(set(st.get('imported_tids', [])) |
                                 {p['tid'] for p in parts} | set(s['tids']))
    save_state(st)
    return True


def run_job(JOB, kind, tids_csv=''):
    cfg = registry.get_settings('cool18_grabber')
    pages = max(1, int(cfg.get('pages', 1) or 1))
    aifilter = bool(cfg.get('aifilter', True))
    root = root_cat_name()
    JOB.update(kind=kind, running=True, msg='运行中', done=0, total=0,
               started=datetime.now().strftime('%m-%d %H:%M:%S'), finished='')
    st = load_state()
    imported_tids = set(st.get('imported_tids', []))
    alive = lambda: JOB['running']
    try:
        if kind == 'search':
            # 搜索下载：tids_csv 为逗号分隔 tid，标题从缓存/正文帖补齐
            want = [t for t in tids_csv.split(',') if t.strip()]
            cache = {}
            try:
                cache = {it['tid']: it['subject']
                         for it in json.load(open(_search_cache_path(), encoding='utf-8'))}
            except Exception:
                cache = {}
            stories = OrderedDict()
            for tid in want:
                subj = cache.get(tid, '')
                if not subj:
                    # 无缓存时补抓帖子标题（保证书名/作者/分类正确）
                    try:
                        subj = fetch_thread(tid)['subject']
                        time.sleep(0.5)
                    except Exception:
                        pass
                info = parse_subject(subj)
                k = info['key'] or ('tid%s' % tid)
                s = stories.setdefault(k, {'key': k, 'title': info['title'] or ('tid' + tid),
                                           'author': info['author'], 'cat': info['cat'],
                                           'tids': []})
                s['tids'].append(tid)
            todo = list(stories.values())
            _jlog(JOB, '搜索下载：%d 部作品' % len(todo))
            JOB['total'] = len(todo)
            for idx, s in enumerate(todo):
                if not alive():
                    break
                JOB['done'] = idx
                _import_story(JOB, s, st, cfg, imported_tids, root)
            JOB['done'] = len(todo)
        elif kind == 'by_cat':
            cat = ''
            for tok in (tids_csv or '').split(','):
                tok = tok.strip()
                if tok.startswith('cat='):
                    cat = tok.split('=', 1)[1].strip()
            pages_c = min(1000, max(1, int(cfg.get('cat_pages', 20) or 20)))
            if not cat:
                _jlog(JOB, '未指定分类')
                JOB['msg'] = '未指定分类'
                JOB['running'] = False
                JOB['finished'] = datetime.now().strftime('%m-%d %H:%M:%S')
                return
            _jlog(JOB, '按分类抓取：「%s」，分类页 %d 页' % (cat, pages_c))
            cthreads = list_by_cat(cat, pages=pages_c, log=lambda m: _jlog(JOB, m), alive=alive)
            stories = OrderedDict()
            for t in cthreads:
                info = parse_subject(t.get('subject', ''))
                k = info['key'] or ('tid%s' % t['tid'])
                s = stories.setdefault(k, {'key': k, 'title': info['title'] or ('tid' + t['tid']),
                                           'author': info['author'], 'cat': cat,
                                           'tids': []})
                s['tids'].append(str(t['tid']))
            todo = [s for s in stories.values() if s['key'] not in st['stories']]
            _jlog(JOB, '「%s」分类共 %d 部，待导入 %d 部' % (cat, len(stories), len(todo)))
            JOB['total'] = len(todo)
            for idx, s in enumerate(todo):
                if not alive():
                    break
                JOB['done'] = idx
                _import_story(JOB, s, st, cfg, imported_tids, root)
            JOB['done'] = len(todo)
        else:
            threads = list_threads(pages=pages, aifilter=aifilter,
                                   log=lambda m: _jlog(JOB, m), alive=alive)
            stories = OrderedDict()
            for t in threads:
                info = parse_subject(t.get('subject', ''))
                if not info['key']:
                    continue
                s = stories.setdefault(info['key'], {'key': info['key'], 'title': info['title'],
                                                     'author': info['author'], 'cat': info['cat'],
                                                     'tids': []})
                s['tids'].append(str(t['tid']))
            if kind == 'latest':
                todo = [s for s in stories.values() if s['key'] not in st['stories']]
                _jlog(JOB, '新作品 %d 部（列表共 %d 部）' % (len(todo), len(stories)))
                JOB['total'] = len(todo)
                for idx, s in enumerate(todo):
                    if not alive():
                        break
                    JOB['done'] = idx
                    _import_story(JOB, s, st, cfg, imported_tids, root)
                JOB['done'] = len(todo)
            else:  # update
                known = st['stories']
                _jlog(JOB, '已入库作品 %d 部，检查更新…' % len(known))
                JOB['total'] = len(known)
                updates = 0
                for idx, (key, meta) in enumerate(known.items()):
                    JOB['done'] = idx
                    if not alive():
                        break
                    new_tids = [t['tid'] for t in threads
                                if t['tid'] not in set(meta.get('tids', []))
                                and parse_subject(t.get('subject', ''))['key'] == key]
                    if not new_tids:
                        continue
                    _jlog(JOB, '更新《%s》：+%d 新帖' % (meta['title'][:26], len(new_tids)))
                    tids = collect_story_tids(new_tids, known=meta.get('tids', []),
                                              rounds=1, log=lambda m: _jlog(JOB, m), alive=alive)
                    new_parts = [p for p in assemble_parts(tids, delay=float(cfg.get('delay', 0.6) or 0.6),
                                                           max_parts=int(cfg.get('max_parts', 60) or 60),
                                                           alive=alive)
                                 if p['tid'] not in set(meta.get('tids', []))]
                    if not new_parts:
                        meta['tids'] = sorted(set(meta['tids']) | set(new_tids), key=int)
                        st['imported_tids'] = sorted(set(st.get('imported_tids', [])) | set(new_tids))
                        save_state(st)
                        continue
                    book = Book.query.get(meta.get('book_id'))
                    full_path = os.path.join(_books_dir(), book.relative_path) if book else None
                    max_known_no = meta.get('max_no', 0) or 0
                    if full_path and os.path.isfile(full_path) and all(
                            (p['no'] or 10 ** 9) > max_known_no for p in new_parts):
                        with open(full_path, 'a', encoding='utf-8') as f:
                            for p in new_parts:
                                f.write('\n' + '=' * 12 + ' ' + (p['subject'] or '') + ' ' + '=' * 12 + '\n')
                                f.write(p['text'] + '\n')
                        all_parts_meta = meta.get('tids', []) + [p['tid'] for p in new_parts]
                        meta['max_no'] = max([max_known_no] + [p['no'] or 0 for p in new_parts])
                    else:
                        _jlog(JOB, '  章节需重排，重抓全书…')
                        tids_all = collect_story_tids(meta.get('tids', [])[:1], known=[],
                                                      rounds=2, log=lambda m: _jlog(JOB, m), alive=alive)
                        all_tids = sorted(set(meta.get('tids', [])) | set(new_tids) | set(tids_all), key=int)
                        parts = assemble_parts(all_tids, delay=float(cfg.get('delay', 0.6) or 0.6),
                                               max_parts=int(cfg.get('max_parts', 60) or 60) + 60,
                                               log=lambda m: _jlog(JOB, m), alive=alive)
                        s = {'title': meta['title'], 'author': '', 'summary': ''}
                        text = _build_text(s, parts)
                        with open(full_path, 'w', encoding='utf-8') as f:
                            f.write(text)
                        all_parts_meta = [p['tid'] for p in parts]
                        meta['max_no'] = max([p['no'] or 0 for p in parts])
                    if book:
                        book.file_size = os.path.getsize(full_path)
                        book.modified_time = datetime.now()
                        db.session.commit()
                    meta['tids'] = sorted(set(all_parts_meta) | set(new_tids), key=int)
                    st['imported_tids'] = sorted(set(st.get('imported_tids', [])) |
                                                 {p['tid'] for p in new_parts} | set(new_tids))
                    save_state(st)
                    updates += 1
                    _jlog(JOB, '  √ 已更新《%s》' % meta['title'][:26])
                _jlog(JOB, '更新完成：%d 部作品有新内容' % updates)
        JOB['msg'] = '完成' if JOB['running'] else '已停止'
    except Exception as e:
        JOB['msg'] = '失败：%s' % e
        _jlog(JOB, '任务失败：%s' % e)
        _jlog(JOB, traceback.format_exc()[-500:])
    finally:
        JOB['running'] = False
        JOB['finished'] = datetime.now().strftime('%m-%d %H:%M:%S')
        try:
            db.session.remove()
        except Exception:
            pass


# ============================================================
# 页面数据
# ============================================================

def page_data(JOB):
    from flask_login import current_user
    st = load_state()
    stories = sorted(st.get('stories', {}).values(), key=lambda m: -(m.get('book_id') or 0))
    books = {}
    for m in stories:
        b = Book.query.get(m.get('book_id'))
        if b:
            books[m['book_id']] = b
    cfg = registry.get_settings('cool18_grabber')
    return {'stories': stories, 'books': books, 'job': JOB, 'cfg': cfg,
            'imported_count': len(st.get('stories', {})),
            'imported_tids': len(st.get('imported_tids', [])),
            'is_admin': bool(current_user.is_authenticated and getattr(current_user, 'is_admin', False))}


def status_data(JOB):
    st = load_state()
    return {'job': {k: JOB[k] for k in ('running', 'kind', 'msg', 'log', 'done', 'total', 'started', 'finished')},
            'imported_count': len(st.get('stories', {})),
            'imported_tids': len(st.get('imported_tids', []))}
