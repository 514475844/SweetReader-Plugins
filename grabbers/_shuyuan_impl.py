# -*- coding: utf-8 -*-
"""Legado 书源导入器 - 实现层（壳按 mtime 热加载）。
精简规则引擎：支持 {{key}}、class/tag/id 选择器(含索引与!排除)、CSS 选择器、
@text/@html/@href/@src/@content/@all、##正则替换、POST 搜索。
不支持 <js>/@js:（标记为不支持）。
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
from datetime import datetime

from flask import current_app
from flask_login import current_user

from app.models import Book, Category, db
from app.plugins import registry

UA = ("Mozilla/5.0 (Linux; Android 12; Yolo) AppleWebKit/603.1.30 "
      "(KHTML, like Gecko) Version/4.0 Chrome/100.0.2987.108 Mobile Safari/537.36")
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


AD_PAT = re.compile(
    r"(一秒记住|笔趣阁|www\.[a-z0-9]+\.(?:com|net|cc|info|org|la)|https?://|请记住|首发|无弹窗|"
    r"天才一秒|值得铭记|最新章节|求收藏|求推荐|求月票|手机阅读|^[-=*_·——\s]+$)", re.I)


def _cfg():
    return registry.get_settings('shuyuan_importer')


import http.cookiejar
_COOKIE_JAR = http.cookiejar.CookieJar()


def _opener():
    px = (_cfg().get('proxy_url') or '').strip()
    h = urllib.request.ProxyHandler({'http': px, 'https': px} if px else {})
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(_COOKIE_JAR), h,
        urllib.request.HTTPSHandler(context=_SSL))


def _headers(source):
    h = {"User-Agent": UA, "Accept": "text/html,*/*;q=0.8", "Accept-Language": "zh-CN,zh;q=0.9"}
    try:
        extra = source.get('header')
        if isinstance(extra, str) and extra.strip().startswith('{'):
            h.update(json.loads(extra))
    except Exception:
        pass
    return h


def _fetch(url, source=None, post=None, timeout=25):
    data = None
    method = 'GET'
    if post:
        method = 'POST'
        data = urllib.parse.urlencode(post).encode()
    h = _headers(source or {})
    if post and source and source.get('bookSourceUrl'):
        h['Referer'] = source.get('bookSourceUrl')
        h['Content-Type'] = 'application/x-www-form-urlencoded'
    req = urllib.request.Request(url, headers=h, data=data, method=method)
    raw = _opener().open(req, timeout=timeout).read()
    m = re.search(rb'charset=["\']?([\w-]+)', raw[:2000])
    enc = m.group(1).decode().lower() if m else 'utf-8'
    try:
        return raw.decode(enc)
    except UnicodeDecodeError:
        return raw.decode(enc, 'ignore')


# ---------------- 精简 Legado 规则引擎 ----------------

def _css_or_tokens(sel):
    s = sel.strip()
    low = s.lower()
    if low.startswith('@css:'):
        return 'css', s[5:].strip()
    if low.startswith('css:'):
        return 'css', s[4:].strip()
    if s.startswith(('.', '#')) or ' ' in s or '>' in s or '[' in s:
        return 'css', s
    return 'tokens', s


def _css2xpath(sel):
    """极简 CSS->XPath：tag、.class、#id、后代空格、子级 >"""
    # 简化：仅处理空格后代组合与 >
    out = ''
    tokens = re.findall(r'>|[^\s>]+', sel)
    sep = ''
    for tok in tokens:
        if tok == '>':
            sep = '/'
            continue
        xp = _simple2xp(tok)
        out += sep + xp
        sep = '//'
    return out


def _simple2xp(tok):
    tag, rest = '*', tok
    m = re.match(r'^([a-zA-Z][\w-]*)(.*)$', tok)
    if m and tok[0] not in '.#':
        tag, rest = m.group(1), m.group(2)
    conds = []
    for m2 in re.finditer(r'([.#])([\w-]+)', rest):
        if m2.group(1) == '.':
            conds.append('contains(concat(" ", normalize-space(@class), " "), " %s ")' % m2.group(2))
        else:
            conds.append('@id="%s"' % m2.group(2))
    base = tag.lower() if tag != '*' else '*'
    return base + ('[%s]' % ' and '.join(conds) if conds else '')


def _sel_xpath(el, sel):
    return el.xpath('.//' + _css2xpath(sel))


def _token_select(el, tokens):
    cur = [el]
    for tok in tokens:
        if not tok:
            continue
        excl = set()
        m = re.match(r'^(tag|class|id|text)\.(.+)$', tok)
        if tok == 'children':
            nxt = []
            for c in cur:
                nxt.extend(c.xpath('./*'))
            cur = nxt
            continue
        if m:
            kind, rest = m.group(1), m.group(2)
            parts = rest.split('.')
            name = parts[0]
            idxs = []
            for p in parts[1:]:
                if re.match(r'^!?-?\d+$', p):
                    if p.startswith('!'):
                        excl.add(int(p[1:]))
                    else:
                        idxs.append(int(p))
            nxt = []
            for c in cur:
                if kind == 'tag':
                    if name == 'all':
                        found = c.xpath('.//*')
                    else:
                        found = c.xpath('.//*[local-name()="%s"]' % name)
                elif kind == 'class':
                    found = c.xpath('.//*[contains(concat(" ", normalize-space(@class), " "), " %s ")]' % name)
                elif kind == 'id':
                    found = c.xpath('.//*[@id="%s"]' % name)
                else:
                    found = [c]
                if idxs:
                    found = [found[i] for i in idxs if i < len(found)]
                if excl:
                    found = [x for i, x in enumerate(found) if i not in excl]
                nxt.extend(found)
            cur = nxt
        else:
            if re.match(r'^!?-?\d+$', tok):
                i = int(tok.lstrip('!'))
                if tok.startswith('!'):
                    cur = [x for j, x in enumerate(cur) if j != i]
                else:
                    cur = [cur[i]] if i < len(cur) else []
    return cur


def _apply_rule(el, rule):
    rule = rule.strip()
    if not rule:
        return ''
    rep = None
    if '##' in rule:
        seg = rule.split('##')
        rule = seg[0]
        if len(seg) >= 2:
            rep = (seg[1], seg[2] if len(seg) > 2 else '')
    parts = [p for p in re.split(r'@', rule) if p != '']
    kind, sel = _css_or_tokens(parts[0])
    if kind == 'css':
        cur = _sel_xpath(el, sel)
    else:
        cur = _token_select(el, parts[0].split('.'))
    attr = None
    for p in parts[1:]:
        pl = p.strip().lower()
        if pl in ('text', 'textnodes', 'owntext', 'all', 'contentnodes'):
            attr = pl
        elif pl in ('href', 'src', 'content', 'value', 'title', 'alt'):
            attr = pl
        else:
            kind2, sel2 = _css_or_tokens(p)
            if kind2 == 'css':
                nxt = []
                for c in cur:
                    nxt.extend(_sel_xpath(c, sel2))
                cur = nxt
            else:
                cur = _token_select(cur, p.split('.'))
    texts = []
    for c in cur:
        if hasattr(c, 'text_content'):
            if attr is None or attr in ('text', 'contentnodes'):
                texts.append(c.text_content())
            elif attr == 'all':
                texts.append(str(c))
            elif attr == 'owntext':
                ot = (c.text or '').strip()
                texts.append(ot if ot else c.text_content())
            else:
                v = c.get(attr)
                texts.append(v if v else '')
        else:
            texts.append(str(c))
    val = '\n'.join([t for t in texts if t])
    if rep:
        try:
            val = re.sub(rep[0], rep[1].replace('$', '\\'), val)
        except Exception:
            pass
    return val


def _rule_get(root, rule):
    if not rule:
        return ''
    res = _apply_rule(root, rule)
    return res.strip() if isinstance(res, str) else str(res)


def _rule_list(root, rule):
    if not rule:
        return []
    kind, sel = _css_or_tokens(rule)
    if kind == 'css':
        return _sel_xpath(root, sel)
    return _token_select(root, rule.split('.'))



def _url_fill(source, url, key=''):
    """searchUrl 填充：{{key}} 替换；{{...JS...}} 含 source.getKey 时回退 base；POST 选项"""
    opts = {}
    u = url
    m = re.match(r'^(.*?),\s*(\{.*\"method\".*\})\s*$', u, re.S)
    if m:
        u, opt = m.group(1), m.group(2)
        try:
            opts = json.loads(opt.replace('{{key}}', urllib.parse.quote(key)))
        except Exception:
            opts = {}
    base = (source.get('bookSourceUrl') or '').rstrip('/')
    def repl(m):
        inner = m.group(1)
        if 'key' in inner and 'source' not in inner and 'String' not in inner:
            return urllib.parse.quote(key)
        if 'getKey' in inner or 'getVariable' in inner:
            return base
        return ''
    u = re.sub(r'\{\{(.*?)\}\}', repl, u)
    if '{{' in u:
        u = re.sub(r'\{\{.*?\}\}', '', u)
    u = u.replace('{{key}}', urllib.parse.quote(key))
    if u.startswith('/'):
        u = base + u
    return u, opts


def _js_free(source):
    blob = json.dumps({k: source.get(k, '') for k in
                       ('ruleSearch', 'ruleBookInfo', 'ruleToc', 'ruleContent', 'searchUrl')},
                      ensure_ascii=False)
    return ('<js>' not in blob and '@js:' not in blob and 'java.' not in blob
            and '{{' not in json.dumps(source.get('ruleSearch', {}), ensure_ascii=False)
            and '{{' not in json.dumps(source.get('ruleToc', {}), ensure_ascii=False))


# ---------------- 书源仓库 ----------------

def _sources():
    p = os.path.join(current_app.instance_path, 'shuyuan_sources.json')
    try:
        with open(p, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_sources(d):
    p = os.path.join(current_app.instance_path, 'shuyuan_sources.json')
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False)


def add_sources(js):
    try:
        data = json.loads(js)
        if isinstance(data, dict):
            data = [data]
        n, skipped = 0, 0
        d = _sources()
        for s in data:
            url = s.get('bookSourceUrl')
            if not url:
                continue
            s['_js_free'] = _js_free(s)
            if not s['_js_free']:
                skipped += 1
            d[url] = s
            n += 1
        _save_sources(d)
        return True, '导入 %d 个书源（其中 %d 个含JS规则将不可用）' % (n, skipped), n
    except Exception as e:
        return False, 'JSON 解析失败：%s' % str(e)[:80], 0


def fetch_sources(sid, url):
    try:
        if sid:
            api = 'https://www.yckceo.com/yuedu/shuyuan/json/id/%s.html' % sid
            raw = _fetch(api)
        elif url:
            raw = _fetch(url)
        else:
            return False, '未提供 id 或 url', 0
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        return add_sources(json.dumps(data, ensure_ascii=False))
    except Exception as e:
        return False, '拉取失败：%s' % str(e)[:80], 0


def del_source(url):
    d = _sources()
    d.pop(url, None)
    _save_sources(d)


def search_source(JOB, url, kw):
    src = _sources().get(url)
    if not src:
        raise RuntimeError('书源不存在')
    if not src.get('_js_free', True):
        raise RuntimeError('该源含 JS 规则，不支持')
    su = src.get('searchUrl') or (src.get('bookSourceUrl') + '/search.php?searchkey={{key}}')
    u, opts = _url_fill(src, su, kw)
    if opts.get('method', '').upper() == 'POST':
        # 杰奇CMS等要求先访问首页种 Cookie
        try:
            _fetch(src.get('bookSourceUrl'), source=src)
        except Exception:
            pass
        body = opts.get('body', '')
        post = dict(urllib.parse.parse_qsl(body)) if '=' in body else {'key': kw}
        html = _fetch(u, source=src, post=post)
    else:
        html = _fetch(u, source=src)
    rs = src.get('ruleSearch', {})
    import lxml.html
    root = lxml.html.fromstring(html)
    items = []
    for el in _rule_list(root, rs.get('bookList', ''))[:30]:
        name = _rule_get(el, rs.get('name', ''))
        burl = _rule_get(el, rs.get('bookUrl', ''))
        if not name or not burl:
            continue
        if burl.startswith('/'):
            burl = urllib.parse.urljoin(src.get('bookSourceUrl'), burl)
        items.append({'name': t2s(name), 'author': t2s(_rule_get(el, rs.get('author', ''))),
                      'bookUrl': burl, 'cat': t2s(_rule_get(el, rs.get('kind', '')))})
    return items


# ============================================================
# 入库
# ============================================================

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


def _upsert_book(title, author, cat_name, text, tags):
    cfg = _cfg()
    root_name = (cfg.get('category_root') or '书源').strip() or '书源'
    books_dir = current_app.config.get('BOOKS_DIR', '/app/books')
    cat = _get_category(cat_name, root_name)
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
    book.tags = tags
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


def _import_book(JOB, source, book_url, st, cat_hint=''):
    cfg = _cfg()
    alive = lambda: JOB['running']
    bi = source.get('ruleBookInfo', {})
    tc = source.get('ruleToc', {})
    rc = source.get('ruleContent', {})
    page = _fetch(book_url, source=source)
    import lxml.html
    root = lxml.html.fromstring(page)
    title = _rule_get(root, bi.get('name', '')) or '未命名'
    author = _rule_get(root, bi.get('author', ''))
    cat = _rule_get(root, bi.get('kind', '')) or cat_hint or '未分类'
    toc_url = _rule_get(root, bi.get('tocUrl', ''))
    if toc_url and toc_url != book_url:
        if toc_url.startswith('/'):
            toc_url = urllib.parse.urljoin(source.get('bookSourceUrl'), toc_url)
        page = _fetch(toc_url, source=source)
        root = lxml.html.fromstring(page)
    # 目录
    chaps = []
    for el in _rule_list(root, tc.get('chapterList', ''))[:3000]:
        cn = _rule_get(el, tc.get('chapterName', 'tag.a@text') or 'tag.a@text')
        cu = _rule_get(el, tc.get('chapterUrl', 'tag.a@href') or 'tag.a@href')
        if cu:
            if cu.startswith('/'):
                cu = urllib.parse.urljoin(source.get('bookSourceUrl'), cu)
            chaps.append({'t': cn, 'u': cu})
    if not chaps:
        _jlog(JOB, '  《%s》目录为空' % title[:20])
        return False
    buf = ['《%s》' % title, ('作者：%s' % author) if author else '', '']
    n = 0
    repl = None
    crule = rc.get('content', '')
    if '##' in crule:
        seg = crule.split('##')
        crule = seg[0]
        if len(seg) >= 2:
            repl = (seg[1], seg[2] if len(seg) > 2 else '')
    for i, c in enumerate(chaps):
        if not alive():
            break
        try:
            cpage = _fetch(c['u'], source=source)
            croot = lxml.html.fromstring(cpage)
            txt = _apply_rule(croot, crule) if crule else ''
            if repl:
                try:
                    txt = re.sub(repl[0], repl[1].replace('$', '\\\\'), txt)
                except Exception:
                    pass
            txt = _clean(txt)
            if txt and len(txt) > 20:
                buf.append(c['t'] or ('第%d章' % (i + 1)))
                buf.append(txt)
                buf.append('')
                n += 1
            if (i + 1) % 20 == 0:
                _jlog(JOB, '  《%s》%d/%d 章' % (title[:18], i + 1, len(chaps)))
            time.sleep(float(cfg.get('delay', 0.5) or 0.5))
        except Exception as e:
            _jlog(JOB, '  章「%s」失败：%s' % (c['t'][:14], str(e)[:40]))
    full_text = '\n'.join(buf)
    if j2s_on():
        full_text = t2s(full_text)
        title = t2s(title)
    min_size = int(cfg.get('min_size', 1000) or 0)
    if n == 0 or len(re.sub(r'\s+', '', full_text)) < min_size:
        _jlog(JOB, '  《%s》有效章节不足，跳过' % title[:20])
        return False
    src_name = source.get('bookSourceName') or '书源'
    book, created = _upsert_book(title, author, cat_hint or cat or src_name, full_text,
                                 'shuyuan:' + src_name)
    _jlog(JOB, '  √ %s book#%s《%s》%d 章 %d 字' %
          ('入库' if created else '更新', book.id, title[:20], n, len(full_text)))
    st['books'][book_url] = {'book_id': book.id, 'title': title, 'cat': cat,
                             'chapters': len(chaps)}
    save_state(st)
    return True


def j2s_on():
    return bool(_cfg().get('j2s', True))


def _clean(html):
    t = re.sub(r'<script.*?</script>', '', html, flags=re.S | re.I)
    t = re.sub(r'<style.*?</style>', '', t, flags=re.S | re.I)
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
        if AD_PAT.search(ln) and len(ln) < 100:
            continue
        out.append(ln)
    return '\n'.join(out).strip()


def _state_path():
    return os.path.join(current_app.instance_path, 'shuyuan_state.json')


def load_state():
    try:
        with open(_state_path(), 'r', encoding='utf-8') as f:
            st = json.load(f)
            st.setdefault('books', {})
            return st
    except Exception:
        return {'books': {}}


def save_state(st):
    with open(_state_path(), 'w', encoding='utf-8') as f:
        json.dump(st, f, ensure_ascii=False)


def run_job(JOB, kind, src_url, arg):
    cfg = _cfg()
    JOB.update(kind=kind, running=True, msg='运行中', done=0, total=0,
               started=datetime.now().strftime('%m-%d %H:%M:%S'), finished='')
    st = load_state()
    alive = lambda: JOB['running']
    src = _sources().get(src_url)
    if not src:
        JOB['msg'] = '书源不存在'
        return
    try:
        if kind == 'book':
            _jlog(JOB, '导入《%s》…' % arg[:40])
            JOB['total'] = 1
            _import_book(JOB, src, arg, st)
            JOB['done'] = 1
        JOB['msg'] = '完成' if JOB['running'] else '已停止'
    except Exception as e:
        JOB['msg'] = '失败：%s' % e
        _jlog(JOB, '任务失败：%s' % e)
        _jlog(JOB, traceback.format_exc()[-400:])
    finally:
        JOB['running'] = False
        JOB['finished'] = datetime.now().strftime('%m-%d %H:%M:%S')
        try:
            db.session.remove()
        except Exception:
            pass


def page_data(JOB):
    st = load_state()
    books = sorted(st.get('books', {}).values(), key=lambda m: -(m.get('book_id') or 0))
    bmap = {}
    for m in books:
        b = Book.query.get(m.get('book_id'))
        if b:
            bmap[m['book_id']] = b
    srcs = []
    for url, s in _sources().items():
        srcs.append({'url': url, 'name': s.get('bookSourceName') or url,
                     'js_free': s.get('_js_free', True)})
    return {'stories': books, 'books': bmap, 'job': JOB, 'cfg': _cfg(),
            'sources': srcs,
            'is_admin': bool(current_user.is_authenticated and getattr(current_user, 'is_admin', False))}


def status_data(JOB):
    st = load_state()
    return {'job': {k: JOB[k] for k in ('running', 'kind', 'msg', 'log', 'done', 'total', 'started', 'finished')},
            'imported_count': len(st.get('books', {}))}
