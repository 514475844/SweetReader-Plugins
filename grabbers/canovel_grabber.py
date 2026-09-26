# -*- coding: utf-8 -*-
"""SweetReader 插件：canovel 情色小说抓取器（稳定壳）

canovel.com 为 WordPress 站：一部小说 = 一篇文章 /article/<id>，章节为文章内翻页 /article/<id>/N。
站点有 Cloudflare 盾，需经代理访问（设置项 proxy_url，默认借道 Windows v2rayN）。
逻辑在 _canovel_impl.py（热加载，更新无需重启）；只有壳/元数据变化才需点一次「重新扫描」。
"""
import os
import threading

from flask import Blueprint, request, jsonify, render_template_string, current_app
from flask_login import login_required, current_user

from app.plugins import Plugin, PluginEntry

PLUGIN_ID = 'canovel_grabber'
_INSTANCE_DIR = os.environ.get('INSTANCE_DIR', '/app/instance')
_IMPL_PATH = os.path.join(_INSTANCE_DIR, 'plugins', '_canovel_impl.py')
_TPL_PATH = os.path.join(_INSTANCE_DIR, 'plugins', 'templates', 'canovel.html')

bp = Blueprint(PLUGIN_ID, __name__)

JOB = {"running": False, "kind": "", "msg": "", "log": [], "done": 0, "total": 0,
       "started": "", "finished": ""}
JOB_LOCK = threading.Lock()
_IMPL_CACHE = {"mtime": 0, "mod": None}


def _impl():
    import importlib.util
    mtime = os.path.getmtime(_IMPL_PATH)
    if _IMPL_CACHE["mod"] is None or _IMPL_CACHE["mtime"] != mtime:
        spec = importlib.util.spec_from_file_location('_canovel_impl_live', _IMPL_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _IMPL_CACHE["mod"] = mod
        _IMPL_CACHE["mtime"] = mtime
    return _IMPL_CACHE["mod"]


def _admin_required():
    return current_user.is_authenticated and getattr(current_user, 'is_admin', False)


def _render(**ctx):
    with open(_TPL_PATH, 'r', encoding='utf-8') as f:
        src = f.read()
    return render_template_string(src, **ctx)


@bp.route('/plugin/canovel')
@login_required
def page():
    ctx = _impl().page_data(JOB)
    ctx['urls'] = {'start': '/plugin/canovel/start', 'stop': '/plugin/canovel/stop',
                   'status': '/plugin/canovel/status', 'search': '/plugin/canovel/search'}
    return _render(**ctx)


@bp.route('/plugin/canovel/status')
@login_required
def status():
    return jsonify(_impl().status_data(JOB))


@bp.route('/plugin/canovel/start', methods=['POST'])
@login_required
def start():
    if not _admin_required():
        return jsonify({'ok': False, 'msg': '需要管理员权限'}), 403
    kind = request.form.get('kind', 'latest')
    arg = request.form.get('arg', '')
    with JOB_LOCK:
        if JOB['running']:
            return jsonify({'ok': False, 'msg': '已有任务在运行'})
        app = current_app._get_current_object()

        def runner():
            with app.app_context():
                try:
                    _impl().run_job(JOB, kind, arg)
                except Exception as e:
                    JOB['running'] = False
                    JOB['msg'] = '失败：%s' % e

        threading.Thread(target=runner, daemon=True).start()
        return jsonify({'ok': True, 'msg': '已启动'})


@bp.route('/plugin/canovel/stop', methods=['POST'])
@login_required
def stop():
    if not _admin_required():
        return jsonify({'ok': False, 'msg': '需要管理员权限'}), 403
    JOB['running'] = False
    JOB['msg'] = '已请求停止'
    return jsonify({'ok': True, 'msg': '正在停止'})


@bp.route('/plugin/canovel/search')
@login_required
def search():
    kw = request.args.get('kw', '').strip()
    if not kw:
        return jsonify({'ok': False, 'items': [], 'msg': '关键词为空'})
    try:
        items = _impl().search_novels(kw)
        return jsonify({'ok': True, 'items': items})
    except Exception as e:
        return jsonify({'ok': False, 'items': [], 'msg': str(e)})


PLUGIN = Plugin(
    id=PLUGIN_ID,
    name='canovel 书馆抓取',
    description='抓取 canovel.com 小说：文章分页合并成书、按站分类归类、去广告、繁转简、搜索下载、增量更新。需经代理访问。',
    version='1.0.0',
    author='514475844',
    homepage=[],
    admin_nav=[{'label': 'canovel 抓取', 'url': '/plugin/canovel'}],
    blueprint=bp,
    enabled_by_default=True,
    settings_schema=[
        {'key': 'proxy_url', 'label': '抓取代理地址', 'type': 'text', 'default': 'http://192.168.10.121:10808',
         'help': 'canovel 有 Cloudflare 盾，服务器需借道代理（留空则直连）'},
        {'key': 'pages', 'label': '抓取列表页数', 'type': 'int', 'default': 5, 'min': 1, 'max': 500,
         'help': '「抓取最新」扫描的站点列表页数，每页 10 帖'},
        {'key': 'cat_pages', 'label': '按分类抓取页数', 'type': 'int', 'default': 20, 'min': 1, 'max': 500,
         'help': '「按分类抓取」扫描的分类页数，每页 20 部'},
        {'key': 'delay', 'label': '抓取间隔(秒)', 'type': 'float', 'default': 0.8, 'min': 0.3, 'max': 5},
        {'key': 'max_pages', 'label': '单书最大页数', 'type': 'int', 'default': 200, 'min': 1, 'max': 2000},
        {'key': 'min_size', 'label': '最小字数', 'type': 'int', 'default': 1000, 'min': 0},
        {'key': 'j2s', 'label': '繁体自动转简体', 'type': 'bool', 'default': True},
        {'key': 'category_root', 'label': '分类根目录名', 'type': 'text', 'default': 'canovel'},
    ],
)
