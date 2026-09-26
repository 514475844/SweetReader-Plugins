# -*- coding: utf-8 -*-
"""canovel 抓取插件 - 实现层（由壳 canovel_grabber.py 按 mtime 热加载）。
站结构（WordPress/hueman 主题）：
- 一部小说 = 一篇文章 https://canovel.com/article/<id>
- 章节分页 https://canovel.com/article/<id>/2 /3 ...
- 列表：首页 /page/N（每页10）；分类 /article/category/<slug>[/page/N]（每页20）；搜索 /?s=kw
- 文章页含 rel="category tag">分类名、post-byline 作者、entry themeform 正文
"""
import os
import re
import json
import time
import html as htmllib
import traceback
import urllib.request
import urllib.parse
import ssl
from collections import OrderedDict
from datetime import datetime

from flask import current_app
from flask_login import current_user

from app.models import Book, Category, db
from app.plugins import registry

BASE = "https://canovel.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE

# 繁->简（复用 cool18 插件的内嵌表）
_T2S_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_cool18_t2s.py')
try:
    _t2s = {}
    exec(compile(open(_T2S_PATH, encoding='utf-8').read(), _T2S_PATH, 'exec'), _t2s)
    _TBL = str.maketrans(_t2s['T2S_CHARS'])
    _PHR = _t2s['T2S_PHRASES']
except Exception:
    _TBL, _PHR = str.maketrans({}), {}


def t2s(s):
    if not s:
        return s
    for k, v in _PHR.items():
        if k in s:
            s = s.replace(k, v)
    return s.translate(_TBL)


_AD_PAT = re.compile(
    r"(广告|廣告|ADS|adsby|JuicyAds|juicyads|继续阅读|繼續閱讀|请点击|點擊進入|永久网址|永久網址|"
    r"最新地址|飞机|電報|TG群| reciprocal|广告位|招商|"
    r"\.(?:com|net|cc|me|xyz|top|vip|info|org)\b|www\.|https?://|"
    r"^[-=*_·——\s]+$)", re.I)


def _cfg():
    return registry.get_settings('canovel_grabber')


def _opener():
    px = (_cfg().get('proxy_url') or '').strip()
    h = urllib.request.ProxyHandler({'http': px, 'https': px} if px else {})
    return urllib.request.build_opener(h, urllib.request.HTTPSHandler(context=_SSL))


def _fetch(url, timeout=30, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Accept": "text/html,*/*;q=0.8",
                                                       "Accept-Language": "zh-CN,zh;q=0.9"})
            raw = _opener().open(req, timeout=timeout).read()
            if b"Just a moment" in raw[:600] or b"cf-chl" in raw[:1200]:
                raise RuntimeError('cloudflare-challenge')
            return raw.decode('utf-8', 'ignore')
        except Exception as e:
            last = e
            time.sleep(2.0 * (i + 1))
    raise last


def _entry_html(body):
    """文章页 -> 正文 entry 区 HTML（不含评论区/侧栏）"""
    m = re.search(r'<div class="entry themeform"[^>]*>(.*?)(?:<footer|<div class="post-tags|'
                  r'<div class="related|</article>)', body, re.S)
    if m:
        return m.group(1)
    m = re.search(r'<div class="entry[^"]*"[^>]*>(.*?)</article>', body, re.S)
    return m.group(1) if m else ''


def _clean(html):
    t = re.sub(r'<script.*?</script>', '', html, flags=re.S | re.I)
    t = re.sub(r'<style.*?</style>', '', t, flags=re.S | re.I)
    t = re.sub(r'<ins[^>]*>.*?</ins>', '', t, flags=re.S | re.I)
    t = re.sub(r'<br\s*/?>', '\n', t, flags=re.I)
    t = re.sub(r'</p\s*>', '\n', t, flags=re.I)
    t = re.sub(r'<[^>]+>', '', t)
    for k, v in {"&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'"}.items():
        t = t.replace(k, v)
    t = htmllib.unescape(t)
    out, blank = [], 0
    for ln in t.split('\n'):
        ln = ln.strip()
        if not ln:
            blank += 1
            if blank > 1:
                continue
            out.append('')
            continue
        blank = 0
        if _AD_PAT.search(ln) and len(ln) < 160:
            continue
        out.append(ln)
    return '\n'.join(out).strip()


def _title_of(body):
    m = re.search(r'<h1[^>]*class="[^"]*post-title[^"]*"[^>]*>(.*?)</h1>', body, re.S) \
        or re.search(r'<h1[^>]*>(.*?)</h1>', body, re.S)
    return re.sub(r'<[^>]+>', '', m.group(1)).strip() if m else ''


def _author_of(body):
    m = re.search(r'rel="author"[^>]*>([^<]+)<', body)
    return m.group(1).strip() if m else ''


def _cat_of(body):
    m = re.search(r'rel="category tag"[^>]*>([^<]+)<', body)
    return m.group(1).strip() if m else ''


def _maxpage_of(body, aid):
    mx = 1
    for m in re.finditer(r'href="%s/article/%s/(\d+)"' % (re.escape(BASE), aid), body):
        mx = max(mx, int(m.group(1)))
    return mx


def fetch_article_pages(aid, max_pages, log=None, alive=None):
    """抓文章所有分页，返回 (title, author, cat, [text...])"""
    first = _fetch('%s/article/%s' % (BASE, aid))
    title, author, cat = _title_of(first), _author_of(first), _cat_of(first)
    mx = min(_maxpage_of(first, aid), max_pages)
    parts = [_clean(_entry_html(first))]
    if log:
        log('  《%s》共 %d 页' % (title[:24], mx))
    for p in range(2, mx + 1):
        if alive is not None and not alive():
            break
        try:
            body = _fetch('%s/article/%s/%d' % (BASE, aid, p))
            parts.append(_clean(_entry_html(body)))
            time.sleep(float(_cfg().get('delay', 0.8) or 0.8))
        except Exception as e:
            if log:
                log('  第 %d 页抓取失败：%s' % (p, e))
    return title, author, cat, [t for t in parts if t]


def list_latest(pages=5):
    """首页 /page/N（每页 10 篇）"""
    out, seen = [], set()
    for p in range(1, pages + 1):
        url = BASE + '/' if p == 1 else '%s/page/%d' % (BASE, p)
        try:
            body = _fetch(url)
        except Exception:
            break
        for m in re.finditer(r'<h2[^>]*class="[^"]*post-title[^"]*"[^>]*>\s*<a[^>]*href="%s/article/(\d+)[^"]*"[^>]*>(.*?)</a>'
                             % re.escape(BASE), body, re.S):
            aid, txt = m.group(1), re.sub(r'<[^>]+>', '', m.group(2)).strip()
            if aid not in seen and txt:
                seen.add(aid)
                out.append({'aid': aid, 'title': txt})
        time.sleep(0.5)
    return out


CATS = ['都市小品', '人妻熟女', '強暴虐待', '亂倫小說', '校園師生', '古典武俠', '同志小說', '名人明星', '外國翻譯', '其他']


def list_by_cat(cat, pages=20, log=None, alive=None):
    """分类页 /article/category/<slug>[/page/N]，每页 20 篇"""
    slug = urllib.parse.quote(cat)
    out, seen = [], set()
    for p in range(1, pages + 1):
        if alive is not None and not alive():
            break
        url = '%s/article/category/%s' % (BASE, slug) if p == 1 \
            else '%s/article/category/%s/page/%d' % (BASE, slug, p)
        try:
            body = _fetch(url)
        except Exception as e:
            if log:
                log('分类第 %d 页失败：%s' % (p, e))
            break
        n0 = len(out)
        for m in re.finditer(r'<h2[^>]*class="[^"]*post-title[^"]*"[^>]*>\s*<a[^>]*href="%s/article/(\d+)[^"]*"[^>]*>(.*?)</a>'
                             % re.escape(BASE), body, re.S):
            aid, txt = m.group(1), re.sub(r'<[^>]+>', '', m.group(2)).strip()
            if aid not in seen and txt:
                seen.add(aid)
                out.append({'aid': aid, 'title': txt})
        if log:
            log('分类「%s」第 %d 页：+%d 部' % (cat, p, len(out) - n0))
        if 'class="next"' not in body and 'rel="next"' not in body:
            break
        time.sleep(0.5)
    return out


def search_novels(kw, limit=50):
    body = _fetch(BASE + '/?s=' + urllib.parse.quote(kw))
    items, seen = [], set()
    for m in re.finditer(r'<h2[^>]*class="[^"]*post-title[^"]*"[^>]*>\s*<a[^>]*href="%s/article/(\d+)[^"]*"[^>]*>(.*?)</a>'
                         % re.escape(BASE), body, re.S):
        aid, txt = m.group(1), re.sub(r'<[^>]+>', '', m.group(2)).strip()
        if aid not in seen and txt:
            seen.add(aid)
            items.append({'aid': aid, 'title': t2s(txt)})
            if len(items) >= limit:
                break
    try:
        path = _cache_path()
        cache = {}
        try:
            cache = json.load(open(path, encoding='utf-8'))
        except Exception:
            cache = {}
        for it in items:
            cache[it['aid']] = it['title']
        json.dump(cache, open(path, 'w', encoding='utf-8'), ensure_ascii=False)
    except Exception:
        pass
    return items


# ============================================================
# 状态与入库
# ============================================================

def _state_path():
    return os.path.join(current_app.instance_path, 'canovel_state.json')


def load_state():
    try:
        with open(_state_path(), 'r', encoding='utf-8') as f:
            st = json.load(f)
            st.setdefault('books', {})
            st.setdefault('imported_ids', [])
            return st
    except Exception:
        return {'books': {}, 'imported_ids': []}


def save_state(st):
    with open(_state_path(), 'w', encoding='utf-8') as f:
        json.dump(st, f, ensure_ascii=False)


def _cache_path():
    return os.path.join(current_app.instance_path, 'canovel_search.json')


def _safe_name(s):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', (s or '').strip())[:80] or '未命名'


def _initial_of(title):
    try:
        from pypinyin import lazy_pinyin
        c = lazy_pinyin(title[:1])[0][:1].upper()
        return c if c.isalpha() else 'Other'
    except Exception:
        return 'Other'


def _get_category(name, root_name):
    name = name or '未分类'
    root = Category.query.filter(Category.path == root_name).first()
    if not root:
        root = Category(name=root_name, path=root_name, level=0, book_count=0)
        db.session.add(root)
        db.session.flush()
    full = '%s/%s' % (root_name, name)
    cat = Category.query.filter(Category.path == full).first()
    if not cat:
        cat = Category(name=name, path=full, parent_id=root.id, level=1, book_count=0)
        db.session.add(cat)
        db.session.flush()
    return cat


def _upsert_book(title, author, cat_name, text):
    root_name = (_cfg().get('category_root') or 'canovel').strip() or 'canovel'
    books_dir = current_app.config.get('BOOKS_DIR', '/app/books')
    cat = _get_category(cat_name, root_name)
    folder = os.path.join(books_dir, _safe_name(root_name), _safe_name(cat_name))
    os.makedirs(folder, exist_ok=True)
    fn = '《%s》%s.txt' % (_safe_name(title), ('-' + _safe_name(author)) if author else '')
    full = os.path.join(folder, fn)
    with open(full, 'w', encoding='utf-8') as f:
        f.write(text)
    rel = os.path.relpath(full, books_dir)
    book = Book.query.filter_by(filename=fn).first()
    created = book is None
    if not book:
        book = Book(filename=fn)
        db.session.add(book)
    book.title = title
    book.author = author or '佚名'
    book.file_type = 'txt'
    book.file_size = len(text.encode('utf-8'))
    book.relative_path = rel
    book.category_id = cat.id
    book.tags = 'canovel'
    book.modified_time = datetime.now()
    book.upload_date = book.upload_date or datetime.now()
    book.metadata_parsed = True
    book.initial = _initial_of(title)
    db.session.flush()
    if created:
        cat.book_count = (cat.book_count or 0) + 1
    db.session.commit()
    return book, created, full


# ============================================================
# 任务
# ============================================================

def _jlog(JOB, msg):
    JOB['log'].append('[%s] %s' % (datetime.now().strftime('%H:%M:%S'), msg))
    JOB['log'] = JOB['log'][-300:]


def _import_one(JOB, aid, cat_hint, st, imported):
    cfg = _cfg()
    alive = lambda: JOB['running']
    try:
        title, author, cat, parts = fetch_article_pages(
            aid, int(cfg.get('max_pages', 200) or 200),
            log=lambda m: _jlog(JOB, m), alive=alive)
    except Exception as e:
        _jlog(JOB, '  《aid %s》抓取失败：%s' % (aid, e))
        return False
    if bool(cfg.get('j2s', True)):
        title, author, cat = t2s(title), t2s(author), t2s(cat)
    if not title:
        title = 'canovel-' + str(aid)
    if cat_hint:
        cat = cat_hint
    text = '《%s》\n作者：%s\n\n' % (title, author or '佚名') + \
        '\n\n'.join('────── 第 %d 部分 ──────\n%s' % (i + 1, p) for i, p in enumerate(parts))
    if bool(cfg.get('j2s', True)):
        text = t2s(text)
    plain = re.sub(r'\s+', '', text)
    min_size = int(cfg.get('min_size', 1000) or 0)
    if len(plain) < min_size:
        _jlog(JOB, '  《%s》字数过少(%d)，跳过' % (title[:20], len(plain)))
        return False
    book, created, full = _upsert_book(title, author, cat or '未分类', text)
    _jlog(JOB, '  √ %s book#%s《%s》%d 字 %d 部分' %
          ('入库' if created else '更新', book.id, title[:20], len(text), len(parts)))
    st['books'][str(aid)] = {'book_id': book.id, 'title': title, 'author': author,
                             'cat': cat or '未分类', 'parts': len(parts)}
    st['imported_ids'] = sorted(set(st.get('imported_ids', [])) | {str(aid)})
    save_state(st)
    return True


def run_job(JOB, kind, arg=''):
    cfg = _cfg()
    JOB.update(kind=kind, running=True, msg='运行中', done=0, total=0,
               started=datetime.now().strftime('%m-%d %H:%M:%S'), finished='')
    st = load_state()
    imported = set(st.get('imported_ids', []))
    alive = lambda: JOB['running']
    root = (_cfg().get('category_root') or 'canovel').strip() or 'canovel'
    try:
        if kind == 'by_cat':
            cat = arg.strip()
            if not cat:
                JOB['msg'] = '未指定分类'
                return
            # 站内分类为繁体，允许用户输简体
            cat_site = cat
            pages_c = min(500, max(1, int(cfg.get('cat_pages', 20) or 20)))
            _jlog(JOB, '按分类抓取：「%s」，分类页 %d 页' % (cat, pages_c))
            items = list_by_cat(cat_site, pages=pages_c,
                                log=lambda m: _jlog(JOB, m), alive=alive)
            todo = [it for it in items if it['aid'] not in imported]
            _jlog(JOB, '「%s」共 %d 部，待导入 %d 部' % (cat, len(items), len(todo)))
            JOB['total'] = len(todo)
            for idx, it in enumerate(todo):
                if not alive():
                    break
                JOB['done'] = idx
                _import_one(JOB, it['aid'], cat, st, imported)
            JOB['done'] = len(todo)
        elif kind == 'search':
            want = [a for a in (arg or '').split(',') if a.strip()]
            cache = {}
            try:
                cache = json.load(open(_cache_path(), encoding='utf-8'))
            except Exception:
                pass
            _jlog(JOB, '搜索下载：%d 篇' % len(want))
            JOB['total'] = len(want)
            for idx, aid in enumerate(want):
                if not alive():
                    break
                JOB['done'] = idx
                _import_one(JOB, aid.strip(), '', st, imported)
            JOB['done'] = len(want)
        elif kind == 'update':
            known = st['books']
            _jlog(JOB, '增量更新：已入库 %d 部，检查新页…' % len(known))
            JOB['total'] = len(known)
            updates = 0
            for idx, (aid, meta) in enumerate(known.items()):
                JOB['done'] = idx
                if not alive():
                    break
                try:
                    first = _fetch('%s/article/%s' % (BASE, aid))
                    mx = min(_maxpage_of(first, aid), int(cfg.get('max_pages', 200) or 200))
                    old = int(meta.get('parts', 1))
                    if mx <= old:
                        continue
                    _jlog(JOB, '《%s》+%d 新页' % (meta['title'][:20], mx - old))
                    new_parts = []
                    for p in range(old + 1, mx + 1):
                        if not alive():
                            break
                        body = _fetch('%s/article/%s/%d' % (BASE, aid, p))
                        new_parts.append(_clean(_entry_html(body)))
                        time.sleep(float(cfg.get('delay', 0.8) or 0.8))
                    new_parts = [p for p in new_parts if p]
                    if not new_parts:
                        meta['parts'] = mx
                        save_state(st)
                        continue
                    book = Book.query.get(meta.get('book_id'))
                    full = os.path.join(current_app.config.get('BOOKS_DIR', '/app/books'),
                                        book.relative_path) if book else None
                    if book and full and os.path.isfile(full):
                        with open(full, 'a', encoding='utf-8') as f:
                            for j, p in enumerate(new_parts):
                                f.write('\n\n────── 第 %d 部分 ──────\n%s' % (old + j + 1, p))
                        book.file_size = os.path.getsize(full)
                        book.modified_time = datetime.now()
                        db.session.commit()
                    meta['parts'] = mx
                    save_state(st)
                    updates += 1
                    _jlog(JOB, '  √ 已更新《%s》' % meta['title'][:20])
                except Exception as e:
                    _jlog(JOB, '  aid %s 检查失败：%s' % (aid, e))
            _jlog(JOB, '更新完成：%d 部有新内容' % updates)
        else:  # latest
            pages = min(500, max(1, int(cfg.get('pages', 5) or 5)))
            _jlog(JOB, '抓取最新：站点列表 %d 页' % pages)
            items = list_latest(pages)
            todo = [it for it in items if it['aid'] not in imported]
            _jlog(JOB, '共 %d 篇，待导入 %d 篇' % (len(items), len(todo)))
            JOB['total'] = len(todo)
            for idx, it in enumerate(todo):
                if not alive():
                    break
                JOB['done'] = idx
                _import_one(JOB, it['aid'], '', st, imported)
            JOB['done'] = len(todo)
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
    st = load_state()
    books = sorted(st.get('books', {}).values(), key=lambda m: -(m.get('book_id') or 0))
    bmap = {}
    for m in books:
        b = Book.query.get(m.get('book_id'))
        if b:
            bmap[m['book_id']] = b
    return {'stories': books, 'books': bmap, 'job': JOB, 'cfg': _cfg(),
            'imported_count': len(st.get('books', {})),
            'imported_tids': len(st.get('imported_ids', [])),
            'cats': CATS,
            'is_admin': bool(current_user.is_authenticated and getattr(current_user, 'is_admin', False))}


def status_data(JOB):
    st = load_state()
    return {'job': {k: JOB[k] for k in ('running', 'kind', 'msg', 'log', 'done', 'total', 'started', 'finished')},
            'imported_count': len(st.get('books', {})),
            'imported_tids': len(st.get('imported_ids', []))}
