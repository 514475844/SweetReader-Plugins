# -*- coding: utf-8 -*-
"""WordPress 情色小说站群抓取 - 共享实现层（各站壳文件按 mtime 热加载）。
模型：文章 = /20xx/xx/xx/<slug>/[<page>/]（WordPress 日期链接，可有内部分页）；
列表 /page/N；分类 /category/<slug> 或站点短路径（首页菜单解析）；搜索 /?s=kw。
合并：同归一标题的多篇文章合并为一本书（按日期+路径排序）。
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

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE

_T2S_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_cool18_t2s.py')
try:
    _t2s_d = {}
    exec(compile(open(_T2S_PATH, encoding='utf-8').read(), _T2S_PATH, 'exec'), _t2s_d)
    _TBL = str.maketrans(_t2s_d['T2S_CHARS'])
    _PHR = _t2s_d['T2S_PHRASES']
except Exception:
    _TBL, _PHR = str.maketrans({}), {}


def t2s(s):
    if not s:
        return s
    for k, v in _PHR.items():
        if k in s:
            s = s.replace(k, v)
    return s.translate(_TBL)


SITES = {
    'sosing':      'https://sosing.com',
    'novel1000':   'https://1000novel.com',
    'springnovel': 'https://springnovel.com',
    'dfjstory':    'https://dfjstory.com',
    'hhhbook':     'https://hhhbook.com',
    'xxxnovel':    'https://xxxnovel.com',
    'aaanovel':    'https://aaanovel.com',
}

_DATE_PATH = re.compile(r'^/20\d\d/\d\d/\d\d/.+?/$')
_PAGE_SUFFIX = re.compile(r'/(\d+)/?$')
_AD_PAT = re.compile(
    r"(广告|廣告|adsby|juicyads|继续阅读|繼續閱讀|请点击|點擊進入|永久网址|永久網址|最新地址|"
    r"電報|TG群|飞机|直播|裸聊|免費A片|在线观看|線上觀看|"
    r"\.(?:com|net|cc|me|xyz|top|vip|info|org)\b|www\.|https?://|"
    r"^[-=*_·——\s]+$)", re.I)
_GROUP_STRIP = re.compile(
    u'[（(]\\s*\\d+\\s*[）)]|[（(]\\s*[上中下完結]\\s*[）)]|[（(]\\s*\\d+\\s*[-－~至]\\s*\\d+\\s*[）)]')


def _pid(PLUGIN_ID):
    return PLUGIN_ID.replace('_grabber', '')


def _base(site):
    return SITES.get(site, '')


def _cfg(PLUGIN_ID):
    return registry.get_settings(PLUGIN_ID)


def _opener(PLUGIN_ID):
    px = (_cfg(PLUGIN_ID).get('proxy_url') or '').strip()
    h = urllib.request.ProxyHandler({'http': px, 'https': px} if px else {})
    return urllib.request.build_opener(h, urllib.request.HTTPSHandler(context=_SSL))


def _fetch(PLUGIN_ID, url, timeout=30, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Accept": "text/html,*/*;q=0.8",
                                                       "Accept-Language": "zh-CN,zh;q=0.9"})
            raw = _opener(PLUGIN_ID).open(req, timeout=timeout).read()
            if b"Just a moment" in raw[:600] or b"cf-chl" in raw[:1200]:
                raise RuntimeError('cloudflare-challenge')
            return raw.decode('utf-8', 'ignore')
        except Exception as e:
            last = e
            time.sleep(2.0 * (i + 1))
    raise last


def _abs(site, url):
    if url.startswith('http'):
        return url
    return _base(site) + ('' if url.startswith('/') else '/') + url


def _canonical(path):
    p = path.rstrip('/') + '/'
    m = _PAGE_SUFFIX.search(p)
    if m and m.group(1) != '1':
        page = int(m.group(1))
        can = p[:m.start()] + '/'
        return can, page
    return p, 1


def _extract_posts(body):
    out, seen = [], set()
    for m in re.finditer(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', body, re.S):
        href, txt = m.group(1), re.sub(r'<[^>]+>', '', m.group(2)).strip()
        p = urllib.parse.urlparse(href).path
        if '/page/' in p or '/tag/' in p or not _DATE_PATH.match(p.rstrip('/') + '/'):
            continue
        can, page = _canonical(p)
        if can in seen or not txt or len(txt) < 2 or re.match(r'^\d+$', txt):
            continue
        seen.add(can)
        out.append({'aid': can, 'page': page, 'title': htmllib.unescape(txt)})
    return out


def _container_html(body):
    m = re.search(r'<div[^>]*class="(?:[^"]*\b)?(?:entry-content|entry themeform|post-content|'
                  r'article-content|single-content|td-post-content)[^"]*"[^>]*>(.*)', body, re.S | re.I)
    if not m:
        m = re.search(r'<article[^>]*>(.*)</article>', body, re.S | re.I)
    if not m:
        return ''
    seg = m.group(1)
    end = len(seg)
    for marker in ('post-tags', 'related-posts', 'entry-footer', 'id="comments"', 'id="respond"',
                   '<footer', 'class="navigation', 'class="page-nav', 'class="nav-links"',
                   'class="post-avant"', 'class="post-apres"', '</article>', 'class="post-entries"'):
        i = seg.find(marker)
        if 0 <= i < end:
            end = i
    return seg[:end]


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
    m = re.search(r'<h1[^>]*class="[^"]*entry-title[^"]*"[^>]*>(.*?)</h1>', body, re.S) \
        or re.search(r'<h1[^>]*>(.*?)</h1>', body, re.S)
    return re.sub(r'<[^>]+>', '', m.group(1)).strip() if m else ''


def _author_of(body):
    m = re.search(r'rel="author"[^>]*>([^<]+)<', body)
    return m.group(1).strip() if m else ''


def _cat_of(body):
    m = re.search(r'rel="category tag"[^>]*>([^<]+)<', body)
    return m.group(1).strip() if m else ''


def _group_key(title):
    s = t2s(htmllib.unescape(title or ''))
    s = _GROUP_STRIP.sub('', s)
    s = re.sub(r'[\s　\-—_·.,，。:：！!？?\[\]【】（）()「」『』~～]+', '', s).lower()
    return s


def _fetch_post(PLUGIN_ID, site, aid, max_pages=100, log=None, alive=None):
    base = _base(site)
    body = _fetch(PLUGIN_ID, base + aid)
    title, author, cat = _title_of(body), _author_of(body), _cat_of(body)
    parts = [_clean(_container_html(body))]
    mx = 1
    for m in re.finditer(r'href="(%s[^"]*?)/(\d+)/?"' % re.escape(base + aid.rstrip('/')), body):
        mx = max(mx, int(m.group(2)))
    mx = min(mx, max_pages)
    for p in range(2, mx + 1):
        if alive is not None and not alive():
            break
        try:
            b2 = _fetch(PLUGIN_ID, '%s%s%d/' % (base, aid.rstrip('/'), p))
            parts.append(_clean(_container_html(b2)))
            time.sleep(float(_cfg(PLUGIN_ID).get('delay', 0.8) or 0.8))
        except Exception as e:
            if log:
                log('  第 %d 页失败：%s' % (p, str(e)[:50]))
    return title, author, cat, [t for t in parts if t]


def list_cats(PLUGIN_ID, site, validate=False):
    body = _fetch(PLUGIN_ID, _base(site) + '/')
    cats, seen = [], set()

    def add(path, name):
        p = path.strip('/')
        if not p or p in seen or p.count('/') > 1:
            return
        seen.add(p)
        cats.append({'path': p, 'name': t2s(name.strip())})

    cat_links = re.findall(r'<a[^>]*href="([^"]*/category/[^"]*)"[^>]*>([^<]{2,16})</a>', body)
    if cat_links:
        for href, txt in cat_links:
            p = urllib.parse.urlparse(href).path.strip('/')
            if p and not p.startswith('20') and '/page/' not in p:
                add(p, txt)
    else:
        for m in re.finditer(r'<a[^>]*href="([^"]+)"[^>]*>([^<]{2,16})</a>', body):
            href, txt = m.group(1), m.group(2).strip()
            p = urllib.parse.urlparse(href).path.strip('/')
            if not p or re.match(r'^20\d\d', p) or p in ('feed', 'about', 'adultsites', 'wp-json',
                                                         'sexnovel', 'livechat.html') \
                    or p.startswith('tag') or '.' in p:
                continue
            if re.search(u'[\u4e00-\u9fff]', txt):
                add(p, txt)
    return cats


def list_page_articles(PLUGIN_ID, site, list_path, page=1):
    base = _base(site)
    if page == 1:
        url = base + '/' if not list_path else '%s/%s/' % (base, list_path.strip('/'))
    else:
        url = '%s/%s/page/%d/' % (base, list_path.strip('/'), page) if list_path \
            else '%s/page/%d/' % (base, page)
    body = _fetch(PLUGIN_ID, url)
    return _extract_posts(body)


def search_articles(PLUGIN_ID, site, kw, limit=50):
    body = _fetch(PLUGIN_ID, '%s/?s=%s' % (_base(site), urllib.parse.quote(kw)))
    return _extract_posts(body)[:limit]


def _state_path(site):
    return os.path.join(current_app.instance_path, 'wp_%s_state.json' % site)


def load_state(site):
    try:
        with open(_state_path(site), 'r', encoding='utf-8') as f:
            st = json.load(f)
            st.setdefault('books', {})
            st.setdefault('imported_ids', [])
            return st
    except Exception:
        return {'books': {}, 'imported_ids': []}


def save_state(site, st):
    with open(_state_path(site), 'w', encoding='utf-8') as f:
        json.dump(st, f, ensure_ascii=False)


def _safe_name(s):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', (s or '').strip())[:80] or '未分类'


def _initial_of(title):
    try:
        from pypinyin import lazy_pinyin
        c = lazy_pinyin(title[:1])[0][:1].upper()
        return c if c.isalpha() else 'Other'
    except Exception:
        return 'Other'


def _get_category(name, root_name):
    name = t2s(name or '未分类')
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


def _upsert_book(PLUGIN_ID, title, author, cat_name, text):
    cfg = _cfg(PLUGIN_ID)
    root_name = (cfg.get('category_root') or _pid(PLUGIN_ID)).strip() or _pid(PLUGIN_ID)
    books_dir = current_app.config.get('BOOKS_DIR', '/app/books')
    cat = _get_category(cat_name, root_name)
    folder = os.path.join(books_dir, _safe_name(root_name), _safe_name(cat.name))
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
    book.tags = _pid(PLUGIN_ID)
    book.modified_time = datetime.now()
    book.upload_date = book.upload_date or datetime.now()
    book.metadata_parsed = True
    book.initial = _initial_of(title)
    db.session.flush()
    if created:
        cat.book_count = (cat.book_count or 0) + 1
    db.session.commit()
    return book, created


def _jlog(JOB, msg):
    JOB['log'].append('[%s] %s' % (datetime.now().strftime('%H:%M:%S'), msg))
    JOB['log'] = JOB['log'][-300:]


def _process(JOB, PLUGIN_ID, site, batch, st, imported, alive, cfg, cat_hint=''):
    j2s = bool(cfg.get('j2s', True))
    fetched = []
    for i, it in enumerate(batch):
        if not alive():
            break
        try:
            title, author, cat, parts = _fetch_post(
                PLUGIN_ID, site, it['aid'],
                max_pages=int(cfg.get('max_pages', 100) or 100),
                log=lambda m: _jlog(JOB, m), alive=alive)
            if cat_hint:
                cat = cat_hint
            if parts:
                fetched.append({'aid': it['aid'], 'title': title or it['title'],
                                'author': author, 'cat': cat, 'text': '\n\n'.join(parts)})
            if (i + 1) % 5 == 0:
                _jlog(JOB, '  已抓 %d/%d 篇' % (i + 1, len(batch)))
        except Exception as e:
            _jlog(JOB, '  《%s》抓取失败：%s' % (it['title'][:20], str(e)[:50]))
    fetched = [x for x in fetched if re.sub(r'\s+', '', x['text'])]
    groups = OrderedDict()
    for x in fetched:
        k = _group_key(x['title']) or ('aid' + re.sub(r'\W+', '', x['aid']))
        g = groups.setdefault(k, {'title': x['title'], 'author': '', 'cat': '', 'parts': []})
        if not g['author']:
            g['author'] = x['author']
        if not g['cat']:
            g['cat'] = x['cat']
        g['parts'].append(x)
    for k, g in groups.items():
        if not alive():
            break
        title = t2s(g['title']) if j2s else g['title']
        author = t2s(g['author']) if j2s else g['author']
        cat_name = t2s(g['cat'] or '未分类') if j2s else (g['cat'] or '未分类')
        text = '《%s》\n作者：%s\n\n' % (title, author or '佚名') + \
            '\n\n'.join('────── 第 %d 部分 ──────\n%s' % (i + 1, p['text'])
                        for i, p in enumerate(g['parts']))
        min_size = int(cfg.get('min_size', 1000) or 0)
        if len(re.sub(r'\s+', '', text)) < min_size:
            _jlog(JOB, '  《%s》字数过少，跳过' % title[:20])
            continue
        book, created = _upsert_book(PLUGIN_ID, title, author, cat_name, text)
        _jlog(JOB, '  √ %s book#%s《%s》%d 字 %d 部分' %
              ('入库' if created else '更新', book.id, title[:20], len(text), len(g['parts'])))
        for x in g['parts']:
            imported.add(x['aid'])
            st['books'][x['aid']] = {'book_id': book.id, 'title': title,
                                     'author': author, 'cat': cat_name,
                                     'key': k, 'parts': len(g['parts'])}
        save_state(site, st)


def run_job(JOB, PLUGIN_ID, site, kind, arg=''):
    cfg = _cfg(PLUGIN_ID)
    JOB.update(kind=kind, running=True, msg='运行中', done=0, total=0,
               started=datetime.now().strftime('%m-%d %H:%M:%S'), finished='')
    st = load_state(site)
    imported = set(st.get('imported_ids', []))
    alive = lambda: JOB['running']
    try:
        if kind == 'by_cat':
            cat_path = (arg or '').strip().strip('/')
            if not cat_path:
                JOB['msg'] = '未指定分类'
                return
            pages_n = min(500, max(1, int(cfg.get('cat_pages', 20) or 20)))
            _jlog(JOB, '按分类抓取：%s，%d 页' % (cat_path, pages_n))
        elif kind == 'latest':
            pages_n = min(500, max(1, int(cfg.get('pages', 5) or 5)))
            _jlog(JOB, '抓取最新：%d 页' % pages_n)
        elif kind == 'search':
            kw = (arg or '').strip()
            if not kw:
                JOB['msg'] = '未指定关键词'
                return
            _jlog(JOB, '搜索「%s」…' % kw)
            items = [it for it in search_articles(PLUGIN_ID, site, kw)
                     if it['aid'] not in imported]
            _jlog(JOB, '搜索到 %d 篇新文章' % len(items))
            JOB['total'] = len(items)
            _process(JOB, PLUGIN_ID, site, items, st, imported, alive, cfg)
            JOB['done'] = len(items)
            JOB['msg'] = '完成' if JOB['running'] else '已停止'
            return
        else:
            JOB['msg'] = '未知任务类型'
            return
        list_path = '' if kind == 'latest' else cat_path
        items, seen = [], set()
        for p in range(1, pages_n + 1):
            if not alive():
                break
            try:
                its = list_page_articles(PLUGIN_ID, site, list_path, p)
            except Exception as e:
                _jlog(JOB, '第 %d 页失败：%s' % (p, str(e)[:60]))
                break
            add = [it for it in its if it['aid'] not in seen]
            for it in add:
                seen.add(it['aid'])
            items += add
            _jlog(JOB, '列表第 %d 页：+%d 篇（累计 %d）' % (p, len(add), len(items)))
            time.sleep(0.5)
            if not add:
                break
        new_items = [it for it in items if it['aid'] not in imported]
        _jlog(JOB, '共 %d 篇，待处理 %d 篇' % (len(items), len(new_items)))
        JOB['total'] = len(new_items)
        CHUNK = 10
        for i in range(0, len(new_items), CHUNK):
            if not alive():
                break
            _process(JOB, PLUGIN_ID, site, new_items[i:i + CHUNK], st, imported,
                     alive, cfg, cat_hint=cat_path if kind == 'by_cat' else '')
            JOB['done'] = min(i + CHUNK, len(new_items))
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


def page_data(JOB, PLUGIN_ID, site):
    st = load_state(site)
    books = sorted(st.get('books', {}).values(), key=lambda m: -(m.get('book_id') or 0))
    bmap = {}
    for m in books:
        b = Book.query.get(m.get('book_id'))
        if b:
            bmap[m['book_id']] = b
    try:
        cats = list_cats(PLUGIN_ID, site)
    except Exception:
        cats = []
    return {'stories': books, 'books': bmap, 'job': JOB, 'cfg': _cfg(PLUGIN_ID),
            'imported_count': len(st.get('books', {})),
            'imported_tids': len(st.get('imported_ids', [])),
            'cats': cats, 'site': site,
            'is_admin': bool(current_user.is_authenticated and getattr(current_user, 'is_admin', False))}


def status_data(JOB, site):
    st = load_state(site)
    return {'job': {k: JOB[k] for k in ('running', 'kind', 'msg', 'log', 'done', 'total', 'started', 'finished')},
            'imported_count': len(st.get('books', {})),
            'imported_tids': len(st.get('imported_ids', []))}
