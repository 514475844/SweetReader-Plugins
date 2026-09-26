# -*- coding: utf-8 -*-
"""SweetReader 插件：飞机文学抓取（dfjstory.com）
壳+热加载架构：路由固定，逻辑在 _wpsites_impl.py（更新无需重启）。
dfjstory.com 有 Cloudflare 盾，需经代理（设置项 proxy_url）。
"""
import os
import threading

from flask import Blueprint, request, jsonify, render_template_string, current_app
from flask_login import login_required, current_user

from app.plugins import Plugin, PluginEntry

PLUGIN_ID = 'dfjstory_grabber'
SITE = 'dfjstory'
_INSTANCE_DIR = os.environ.get('INSTANCE_DIR', '/app/instance')
_IMPL_PATH = os.path.join(_INSTANCE_DIR, 'plugins', '_wpsites_impl.py')
_TPL_PATH = os.path.join(_INSTANCE_DIR, 'plugins', 'templates', 'wpsites.html')

bp = Blueprint(PLUGIN_ID, __name__)
URL_PREFIX = '/' + PLUGIN_ID.replace('_', '-')

JOB = {"running": False, "kind": "", "msg": "", "log": [], "done": 0, "total": 0,
       "started": "", "finished": ""}
_JOB_LOCK = threading.Lock()
_IMPL_CACHE = {"mtime": 0, "mod": None}


def _impl():
    import importlib.util
    mtime = os.path.getmtime(_IMPL_PATH)
    if _IMPL_CACHE["mod"] is None or _IMPL_CACHE["mtime"] != mtime:
        spec = importlib.util.spec_from_file_location('_wpsites_impl_live_dfjstory_grabber', _IMPL_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _IMPL_CACHE["mod"] = mod
        _IMPL_CACHE["mtime"] = mtime
    return _IMPL_CACHE["mod"]


def _admin_required():
    return current_user.is_authenticated and getattr(current_user, 'is_admin', False)


def _render(**ctx):
    with open(_TPL_PATH, 'r', encoding='utf-8') as f:
        return render_template_string(f.read(), **ctx)


@bp.route(URL_PREFIX)
@login_required
def page():
    ctx = _impl().page_data(JOB, PLUGIN_ID, SITE)
    ctx['urls'] = {'start': URL_PREFIX + '/start', 'stop': URL_PREFIX + '/stop',
                   'status': URL_PREFIX + '/status', 'search': URL_PREFIX + '/search'}
    return _render(**ctx)


@bp.route(URL_PREFIX + '/status')
@login_required
def status():
    return jsonify(_impl().status_data(JOB, SITE))


@bp.route(URL_PREFIX + '/start', methods=['POST'])
@login_required
def start():
    if not _admin_required():
        return jsonify({'ok': False, 'msg': '需要管理员权限'}), 403
    kind = request.form.get('kind', 'latest')
    arg = request.form.get('arg', '')
    with _JOB_LOCK:
        if JOB['running']:
            return jsonify({'ok': False, 'msg': '已有任务在运行'})
        app = current_app._get_current_object()

        def runner():
            with app.app_context():
                try:
                    _impl().run_job(JOB, PLUGIN_ID, SITE, kind, arg)
                except Exception as e:
                    JOB['running'] = False
                    JOB['msg'] = '失败：%s' % e

        threading.Thread(target=runner, daemon=True).start()
        return jsonify({'ok': True, 'msg': '已启动'})


@bp.route(URL_PREFIX + '/stop', methods=['POST'])
@login_required
def stop():
    if not _admin_required():
        return jsonify({'ok': False, 'msg': '需要管理员权限'}), 403
    JOB['running'] = False
    JOB['msg'] = '已请求停止'
    return jsonify({'ok': True, 'msg': '正在停止'})


@bp.route(URL_PREFIX + '/search')
@login_required
def search():
    kw = request.args.get('kw', '').strip()
    if not kw:
        return jsonify({'ok': False, 'items': [], 'msg': '关键词为空'})
    try:
        return jsonify({'ok': True, 'items': _impl().search_articles(PLUGIN_ID, SITE, kw)})
    except Exception as e:
        return jsonify({'ok': False, 'items': [], 'msg': str(e)})


PLUGIN = Plugin(
    id=PLUGIN_ID,
    name='飞机文学抓取',
    description='抓取 dfjstory.com 小说：同标题多帖自动合并成书、按站分类归类、去广告、繁转简、搜索下载。需经代理访问。',
    version='1.0.0',
    author='514475844',
    homepage=[],
    admin_nav=[{'label': '飞机文学抓取', 'url': URL_PREFIX}],
    blueprint=bp,
    enabled_by_default=True,
    settings_schema=[
        {'key': 'proxy_url', 'label': '抓取代理地址', 'type': 'text', 'default': '',
         'help': 'dfjstory.com 需借道代理访问（留空直连）'},
        {'key': 'pages', 'label': '抓取列表页数', 'type': 'int', 'default': 5, 'min': 1, 'max': 500},
        {'key': 'cat_pages', 'label': '按分类抓取页数', 'type': 'int', 'default': 20, 'min': 1, 'max': 500},
        {'key': 'delay', 'label': '抓取间隔(秒)', 'type': 'float', 'default': 0.8, 'min': 0.3, 'max': 5},
        {'key': 'min_size', 'label': '最小字数', 'type': 'int', 'default': 1000, 'min': 0},
        {'key': 'j2s', 'label': '繁体自动转简体', 'type': 'bool', 'default': True},
        {'key': 'category_root', 'label': '分类根目录名', 'type': 'text', 'default': 'dfjstory'},
    ],
)
