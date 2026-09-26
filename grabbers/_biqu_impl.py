# -*- coding: utf-8 -*-
"""笔趣阁系书源抓取 - 共享实现层（各站壳按 mtime 热加载）。
模型：书 = /book|/<id>/，目录页列章节，章节页有正文容器（各站适配）。
任务：by_cat（分类页翻页找书→整本入库）、search、update（目录比对追加新章）。
"""
import os
import re
import json
import time
import html as htmllib
import base64
import traceback
import urllib.request
import urllib.parse
import ssl
from datetime import datetime

from flask import current_app
from flask_login import current_user

from app.models import Book, Category, db
from app.plugins import registry

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE

AD_PAT = re.compile(
    r"(一秒记住|笔趣阁|www\.|https?://|请记住本书|首发|最新章节请|无弹窗| popups |"
    r"手机阅读|m\.[a-z]+\.| genius |天才一秒|值得铭记|收藏|推荐票|月票求|求收藏|求推荐|求月票|"
    r"^[-=*_·——\s]+$|&nbsp;)", re.I)


def t2s_keep(s):
    return s


def _pid(PLUGIN_ID):
    return PLUGIN_ID.replace('_grabber', '')


def _adapter(PLUGIN_ID):
    return ADAPTERS.get(_pid(PLUGIN_ID), {})


def _base(PLUGIN_ID):
    return _adapter(PLUGIN_ID).get('base', '')


def _cfg(PLUGIN_ID):
    return registry.get_settings(PLUGIN_ID)


def _opener(PLUGIN_ID):
    px = (_cfg(PLUGIN_ID).get('proxy_url') or '').strip()
    h = urllib.request.ProxyHandler({'http': px, 'https': px} if px else {})
    return urllib.request.build_opener(h, urllib.request.HTTPSHandler(context=_SSL))


def _fetch(PLUGIN_ID, url, referer=None, timeout=20, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            h = {"User-Agent": UA, "Accept": "text/html,*/*;q=0.8"}
            ref = referer or _adapter(PLUGIN_ID).get('referer')
            if ref:
                h['Referer'] = ref
            raw = _opener(PLUGIN_ID).open(urllib.request.Request(url, headers=h), timeout=timeout).read()
            enc = _adapter(PLUGIN_ID).get('enc', 'utf-8')
            if enc == 'auto':
                m = re.search(rb'charset=["\']?([\w-]+)', raw[:2000])
                enc = m.group(1).decode().lower() if m else 'utf-8'
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                return raw.decode(enc, 'ignore')
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


# ---------------- 各站适配 ----------------

def _shuhaige_cats(PLUGIN_ID):
    # 全站书库分页：/shuku/0_0_0_<页>.html
    return [{'path': 'shuku', 'name': '全站书库'}]


def _shuhaige_books(PLUGIN_ID, path, page):
    body = _fetch(PLUGIN_ID, 'https://www.shuhaige.net/shuku/0_0_0_%d.html' % page if page > 1
                  else 'https://www.shuhaige.net/shuku/')
    out = []
    for m in re.finditer(r'<a[^>]*href="/(\d+)/"[^>]*(?:title="([^"]{1,60})")?[^>]*>(.*?)</a>', body, re.S):
        bid, tit, inner = m.group(1), m.group(2), re.sub(r'<[^>]+>', '', m.group(3)).strip()
        out.append({'book': '/%s/' % bid, 'title': tit or inner})
    seen, dedup = set(), []
    for x in out:
        if x['book'] not in seen and x['title']:
            seen.add(x['book'])
            dedup.append(x)
    return dedup


def _shuhaige_toc(PLUGIN_ID, book):
    body = _fetch(PLUGIN_ID, 'https://www.shuhaige.net' + book, referer='https://www.shuhaige.net/')
    title = re.search(r'<h1[^>]*>(.*?)</h1>', body, re.S)
    author = re.search(r'作者[：:]\s*(?:</?[a-z]+>|\s)*([^\s<]{1,20})', body)
    if not author:
        author = re.search(r'作者[：:]\s*([^\s<]{1,20})', re.sub(r'<[^>]+>', '', body))
    chaps, seen = [], set()
    for m in re.finditer(r'href="(%s\d+\.html)"[^>]*>([^<]{2,60})<' % re.escape(book), body):
        u, t = m.group(1), m.group(2).strip()
        if u not in seen:
            seen.add(u)
            chaps.append({'url': u, 'title': t})
    return ((title.group(1).strip() if title else book.strip('/')),
            (author.group(1).strip() if author else ''), chaps)


def _shuhaige_chapter(PLUGIN_ID, url):
    body = _fetch(PLUGIN_ID, 'https://www.shuhaige.net' + url)
    m = re.search(r'id="content"[^>]*>(.*?)</div>', body, re.S)
    if not m:
        return ''
    return _clean(m.group(1))


def _biquge365_cats(PLUGIN_ID):
    body = _fetch(PLUGIN_ID, 'http://www.biquge365.net/')
    cats, seen = [], set()
    for m in re.finditer(r'href="(/sort/(\d+)_1/)"[^>]*>([^<]{2,10})<', body):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            cats.append({'path': m.group(1).strip('/'), 'name': m.group(3).strip()})
    return cats


def _biquge365_books(PLUGIN_ID, path, page):
    # path 形如 sort/1
    n = path.split('/')[-1]
    body = _fetch(PLUGIN_ID, 'http://www.biquge365.net/sort/%s_%d/' % (n, page))
    out, seen = [], set()
    for m in re.finditer(r'href="(/book/(\d+)/)"[^>]*>\s*(?:<img[^>]*alt="([^"]*)")?[^>]*>\s*(?:<h[23][^>]*>)?([^<]{2,40})', body):
        u, tit = m.group(1), (m.group(3) or m.group(4) or '').strip()
        if u not in seen and tit:
            seen.add(u)
            out.append({'book': u, 'title': tit})
    return out


def _biquge365_toc(PLUGIN_ID, book):
    body = _fetch(PLUGIN_ID, 'http://www.biquge365.net/newbook' + book[:-1] + '/' if book.endswith('/') else '', )
    if not body or 'chapter' not in body:
        body = _fetch(PLUGIN_ID, 'http://www.biquge365.net/newbook%s' % book)
    t = re.search(r'<h1[^>]*>(.*?)</h1>', body, re.S)
    title = re.sub(r'<[^>]+>', '', t.group(1)).strip() if t else ''
    author = re.search(r'作者[：:]\s*([^\s<]{1,20})', body)
    chaps, seen = [], set()
    for m in re.finditer(r'href="(/chapter/\d+/\d+\.html)"[^>]*>([^<]{1,60})<', body):
        u, tt = m.group(1), m.group(2).strip()
        if u not in seen and tt != '开始阅读':
            seen.add(u)
            chaps.append({'url': u, 'title': tt})
    return title, (author.group(1).strip() if author else ''), chaps


def _biquge365_chapter(PLUGIN_ID, url):
    body = _fetch(PLUGIN_ID, 'http://www.biquge365.net' + url,
                  referer='http://www.biquge365.net/')
    m = re.search(r'class="txt"[^>]*>(.*?)</div>', body, re.S)
    return _clean(m.group(1)) if m else ''


def _ranwen8_cats(PLUGIN_ID):
    body = _fetch(PLUGIN_ID, 'http://www.ranwen8.cc/')
    cats, seen = [], set()
    for m in re.finditer(r'href="(/fenlei/(\d+)_1/)"[^>]*>([^<]{2,10})<', body):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            cats.append({'path': m.group(1).strip('/'), 'name': m.group(3).strip()})
    return cats


def _ranwen8_books(PLUGIN_ID, path, page):
    n = path.split('/')[-1]
    body = _fetch(PLUGIN_ID, 'http://www.ranwen8.cc/fenlei/%s_%d/' % (n, page))
    out, seen = [], set()
    for m in re.finditer(r'href="(/book/(\d+)/)"[^>]*>\s*(?:<img[^>]*alt="([^"]*)")?[^>]*>\s*(?:<h[23][^>]*>)?([^<]{2,40})', body):
        u, tit = m.group(1), (m.group(3) or m.group(4) or '').strip()
        if u not in seen and tit:
            seen.add(u)
            out.append({'book': u, 'title': tit})
    return out


def _ranwen8_toc(PLUGIN_ID, book):
    body = _fetch(PLUGIN_ID, 'http://www.ranwen8.cc' + book)
    t = re.search(r'<h1[^>]*>(.*?)</h1>', body, re.S)
    author = re.search(r'作者[：:]\s*([^\s<]{1,20})', re.sub(r'<[^>]+>', ' ', body))
    chaps, seen = [], set()
    for m in re.finditer(r'href="(%s\d+\.html)"[^>]*>([^<]{2,60})<' % re.escape(book), body):
        u, tt = m.group(1), m.group(2).strip()
        if u not in seen:
            seen.add(u)
            chaps.append({'url': u, 'title': tt})
    # 目录页显示为最新在前时整体反转（按站点原序保留两种皆可，此处反转使旧章在前）
    chaps = list(reversed(chaps))
    return (t.group(1).strip() if t else book.strip('/')), (author.group(1).strip() if author else ''), chaps


def _ranwen8_chapter(PLUGIN_ID, url):
    body = _fetch(PLUGIN_ID, 'http://www.ranwen8.cc' + url)
    chunks = re.findall(r"qsbs\.bb\('([A-Za-z0-9+/=]+)'", body)
    txt = ''
    for c in chunks:
        try:
            txt += base64.b64decode(c).decode('utf-8', 'ignore')
        except Exception:
            pass
    if not txt:
        m = re.search(r'class="contentbox[^"]*"[^>]*>(.*?)</div>', body, re.S)
        txt = m.group(1) if m else ''
    return _clean(txt)


ADAPTERS = {
    'shuhaige': {
        'base': 'https://www.shuhaige.net', 'name': '书海阁小说网',
        'cats': _shuhaige_cats, 'books': _shuhaige_books,
        'toc': _shuhaige_toc, 'chapter': _shuhaige_chapter,
        'book_re': re.compile(r'^/\d+/$'),
    },
    'biquge365': {
        'base': 'http://www.biquge365.net', 'name': '笔趣阁365',
        'cats': _biquge365_cats, 'books': _biquge365_books,
        'toc': _biquge365_toc, 'chapter': _biquge365_chapter,
        'book_re': re.compile(r'^/book/\d+/$'),
    },
    'ranwen8': {
        'base': 'http://www.ranwen8.cc', 'name': '笑文小说网',
        'cats': _ranwen8_cats, 'books': _ranwen8_books,
        'toc': _ranwen8_toc, 'chapter': _ranwen8_chapter,
        'book_re': re.compile(r'^/book/\d+/$'),
    },
}


def _clean(html):
    t = re.sub(r'<script.*?</script>', '', html, flags=re.S | re.I)
    t = re.sub(r'<style.*?</style>', '', t, flags=re.S | re.I)
    t = re.sub(r'<ins[^>]*>.*?</ins>', '', t, flags=re.S | re.I)
    t = re.sub(r'<br\s*/?>', '\n', t, flags=re.I)
    t = re.sub(r'</p\s*>', '\n', t, flags=re.I)
    t = re.sub(r'<[^>]+>', '', t)
    for k, v in {"&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
                 "&#39;": "'", "&gt;": ">", "&ldquo;": '"', "&rdquo;": '"',
                 "&mdash;": "—", "&hellip;": "…"}.items():
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
        if AD_PAT.search(ln) and len(ln) < 100:
            continue
        out.append(ln)
    return '\n'.join(out).strip()


def search_books(PLUGIN_ID, kw, limit=30):
    a = _adapter(PLUGIN_ID)
    base = a['base']
    try:
        if _pid(PLUGIN_ID) == 'biquge365':
            body = _fetch(PLUGIN_ID, base + '/s.php?s=' + urllib.parse.quote(kw))
        elif _pid(PLUGIN_ID) == 'ranwen8':
            body = _fetch(PLUGIN_ID, base + '/search.html?s=' + urllib.parse.quote(kw))
        else:
            body = _fetch(PLUGIN_ID, base + '/search.php?searchkey=' + urllib.parse.quote(kw))
    except Exception:
        return []
    out, seen = [], set()
    for m in re.finditer(r'href="((?:/book/\d+/|/\d+/))"[^>]*>\s*(?:<[^>]+>\s*)*([^<]{2,40})', body):
        u, tit = m.group(1), m.group(2).strip()
        if a['book_re'].match(u) and u not in seen and tit:
            seen.add(u)
            out.append({'book': u, 'title': tit})
            if len(out) >= limit:
                break
    return out


def _state_path(site):
    return os.path.join(current_app.instance_path, 'biqu_%s_state.json' % site)


def load_state(site):
    try:
        with open(_state_path(site), 'r', encoding='utf-8') as f:
            st = json.load(f)
            st.setdefault('books', {})
            st.setdefault('imported', {})
            return st
    except Exception:
        return {'books': {}, 'imported': {}}


def save_state(site, st):
    with open(_state_path(site), 'w', encoding='utf-8') as f:
        json.dump(st, f, ensure_ascii=False)


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


def _jlog(JOB, msg):
    JOB['log'].append('[%s] %s' % (datetime.now().strftime('%H:%M:%S'), msg))
    JOB['log'] = JOB['log'][-300:]


def _import_book(JOB, PLUGIN_ID, site, item, st, cat_hint=''):
    a = _adapter(PLUGIN_ID)
    cfg = _cfg(PLUGIN_ID)
    alive = lambda: JOB['running']
    try:
        title, author, chaps = a['toc'](PLUGIN_ID, item['book'])
    except Exception as e:
        _jlog(JOB, '  《%s》目录失败：%s' % (item['title'][:20], str(e)[:50]))
        return False
    if not chaps:
        _jlog(JOB, '  《%s》目录为空，跳过' % item['title'][:20])
        return False
    title = t2s_keep(title or item['title'])
    buf = ['《%s》' % title, ('作者：%s' % author) if author else '', '']
    n = 0
    for i, c in enumerate(chaps):
        if not alive():
            break
        try:
            txt = a['chapter'](PLUGIN_ID, c['url'])
            if txt and len(txt) > 30:
                buf.append(c['title'])
                buf.append(txt)
                buf.append('')
                n += 1
            if (i + 1) % 20 == 0:
                _jlog(JOB, '  《%s》%d/%d 章' % (title[:18], i + 1, len(chaps)))
            time.sleep(float(cfg.get('delay', 0.5) or 0.5))
        except Exception as e:
            _jlog(JOB, '  章「%s」失败：%s' % (c['title'][:16], str(e)[:40]))
    text = '\n'.join(buf)
    min_size = int(cfg.get('min_size', 1000) or 0)
    if n == 0 or len(re.sub(r'\s+', '', text)) < min_size:
        _jlog(JOB, '  《%s》有效章节不足，跳过' % title[:20])
        return False
    # 入库
    root_name = (cfg.get('category_root') or _pid(PLUGIN_ID)).strip() or _pid(PLUGIN_ID)
    books_dir = current_app.config.get('BOOKS_DIR', '/app/books')
    cat = _get_category(cat_hint or (a['name']), root_name)
    folder = os.path.join(books_dir, _safe_name(root_name), _safe_name(cat.name))
    os.makedirs(folder, exist_ok=True)
    fn = '《%s》%s.txt' % (_safe_name(title), ('-' + _safe_name(author)) if author else '')
    full = os.path.join(folder, fn)
    with open(full, 'w', encoding='utf-8') as f:
        f.write(text)
    book = Book.query.filter_by(filename=fn).first()
    created = book is None
    if not book:
        book = Book(filename=fn)
        db.session.add(book)
    book.title = title
    book.author = author or '佚名'
    book.file_type = 'txt'
    book.file_size = len(text.encode('utf-8'))
    book.relative_path = os.path.relpath(full, books_dir)
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
    _jlog(JOB, '  √ %s book#%s《%s》%d 章 %d 字' %
          ('入库' if created else '更新', book.id, title[:20], n, len(text)))
    st['books'][item['book']] = {'book_id': book.id, 'title': title,
                                 'cat': cat.name, 'chapters': len(chaps)}
    st['imported'][item['book']] = len(chaps)
    save_state(site, st)
    return True


def run_job(JOB, PLUGIN_ID, site, kind, arg=''):
    cfg = _cfg(PLUGIN_ID)
    JOB.update(kind=kind, running=True, msg='运行中', done=0, total=0,
               started=datetime.now().strftime('%m-%d %H:%M:%S'), finished='')
    st = load_state(site)
    alive = lambda: JOB['running']
    a = _adapter(PLUGIN_ID)
    try:
        if kind == 'by_cat':
            cat_path = (arg or '').strip().strip('/')
            if not cat_path:
                JOB['msg'] = '未指定分类'
                return
            pages_n = min(200, max(1, int(cfg.get('cat_pages', 5) or 5)))
            _jlog(JOB, '按分类抓取：%s，%d 页' % (cat_path, pages_n))
            books, seen = [], set()
            for p in range(1, pages_n + 1):
                if not alive():
                    break
                try:
                    its = a['books'](PLUGIN_ID, cat_path, p)
                except Exception as e:
                    _jlog(JOB, '第 %d 页失败：%s' % (p, str(e)[:60]))
                    break
                add = [x for x in its if x['book'] not in seen
                       and x['book'] not in st.get('imported', {})]
                for x in add:
                    seen.add(x['book'])
                books += add
                _jlog(JOB, '第 %d 页：+%d 本（累计 %d）' % (p, len(add), len(books)))
                time.sleep(0.4)
                if not add:
                    break
            _jlog(JOB, '待导入 %d 本' % len(books))
            JOB['total'] = len(books)
            for idx, item in enumerate(books):
                if not alive():
                    break
                JOB['done'] = idx
                _jlog(JOB, '导入《%s》…' % item['title'][:24])
                _import_book(JOB, PLUGIN_ID, site, item, st, cat_hint=arg)
            JOB['done'] = len(books)
        elif kind == 'search':
            kw = (arg or '').strip()
            if not kw:
                JOB['msg'] = '未指定关键词'
                return
            books = search_books(PLUGIN_ID, kw)
            _jlog(JOB, '搜索「%s」：%d 本' % (kw, len(books)))
            JOB['total'] = len(books)
            for idx, item in enumerate(books):
                if not alive():
                    break
                JOB['done'] = idx
                _import_book(JOB, PLUGIN_ID, site, item, st)
            JOB['done'] = len(books)
        elif kind == 'update':
            known = st.get('books', {})
            _jlog(JOB, '增量更新：%d 本' % len(known))
            JOB['total'] = len(known)
            upd = 0
            for idx, (bpath, meta) in enumerate(list(known.items())):
                JOB['done'] = idx
                if not alive():
                    break
                try:
                    title, author, chaps = a['toc'](PLUGIN_ID, bpath)
                    old = int(meta.get('chapters', 0))
                    if len(chaps) <= old:
                        continue
                    _jlog(JOB, '《%s》+%d 新章' % (meta['title'][:18], len(chaps) - old))
                    # 只补新章：重新拼全本（简单可靠）
                    _import_book(JOB, PLUGIN_ID, site,
                                 {'book': bpath, 'title': meta['title']}, st)
                    upd += 1
                except Exception as e:
                    _jlog(JOB, '  %s 检查失败：%s' % (meta['title'][:16], str(e)[:40]))
            _jlog(JOB, '更新完成：%d 本' % upd)
        else:
            JOB['msg'] = '未知任务类型'
            return
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
        cats = _adapter(PLUGIN_ID)['cats'](PLUGIN_ID)
    except Exception:
        cats = []
    return {'stories': books, 'books': bmap, 'job': JOB, 'cfg': _cfg(PLUGIN_ID),
            'imported_count': len(st.get('books', {})),
            'cats': cats, 'site': site,
            'is_admin': bool(current_user.is_authenticated and getattr(current_user, 'is_admin', False))}


def status_data(JOB, site):
    st = load_state(site)
    return {'job': {k: JOB[k] for k in ('running', 'kind', 'msg', 'log', 'done', 'total', 'started', 'finished')},
            'imported_count': len(st.get('books', {}))}
