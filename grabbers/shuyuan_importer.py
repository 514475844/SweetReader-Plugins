# -*- coding: utf-8 -*-
"""SweetReader 插件：Legado 书源导入器
粘贴/拉取「阅读」App 书源 JSON（yckceo 等仓库），内置精简规则引擎（非 JS 子集），
支持搜索测试、整本入库、增量更新。含 <js>/@js: 规则的书源不支持（会标记）。
逻辑在 _shuyuan_impl.py（热加载）。
"""
import os
import threading

from flask import Blueprint, request, jsonify, render_template_string, current_app
from flask_login import login_required, current_user

from app.plugins import Plugin, PluginEntry

PLUGIN_ID = 'shuyuan_importer'
_INSTANCE_DIR = os.environ.get('INSTANCE_DIR', '/app/instance')
_IMPL_PATH = os.path.join(_INSTANCE_DIR, 'plugins', '_shuyuan_impl.py')
_TPL_PATH = os.path.join(_INSTANCE_DIR, 'plugins', 'templates', 'shuyuan.html')

bp = Blueprint(PLUGIN_ID, __name__)

JOB = {"running": False, "kind": "", "msg": "", "log": [], "done": 0, "total": 0,
       "started": "", "finished": ""}
_JOB_LOCK = threading.Lock()
_IMPL_CACHE = {"mtime": 0, "mod": None}


def _impl():
    import importlib.util
    mtime = os.path.getmtime(_IMPL_PATH)
    if _IMPL_CACHE["mod"] is None or _IMPL_CACHE["mtime"] != mtime:
        spec = importlib.util.spec_from_file_location('_shuyuan_impl_live', _IMPL_PATH)
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


@bp.route('/plugin/shuyuan')
@login_required
def page():
    ctx = _impl().page_data(JOB)
    ctx['urls'] = {'start': '/plugin/shuyuan/start', 'stop': '/plugin/shuyuan/stop',
                   'status': '/plugin/shuyuan/status', 'search': '/plugin/shuyuan/search',
                   'add': '/plugin/shuyuan/add', 'fetch': '/plugin/shuyuan/fetch',
                   'del': '/plugin/shuyuan/del', 'test': '/plugin/shuyuan/test'}
    return _render(**ctx)


@bp.route('/plugin/shuyuan/status')
@login_required
def status():
    return jsonify(_impl().status_data(JOB))


@bp.route('/plugin/shuyuan/add', methods=['POST'])
@login_required
def add():
    if not _admin_required():
        return jsonify({'ok': False, 'msg': '需要管理员权限'}), 403
    js = request.form.get('json', '')
    ok, msg, n = _impl().add_sources(js)
    return jsonify({'ok': ok, 'msg': msg, 'count': n})


@bp.route('/plugin/shuyuan/fetch', methods=['POST'])
@login_required
def fetch():
    if not _admin_required():
        return jsonify({'ok': False, 'msg': '需要管理员权限'}), 403
    sid = request.form.get('sid', '').strip()
    url = request.form.get('url', '').strip()
    ok, msg, n = _impl().fetch_sources(sid, url)
    return jsonify({'ok': ok, 'msg': msg, 'count': n})


@bp.route('/plugin/shuyuan/del', methods=['POST'])
@login_required
def delete():
    if not _admin_required():
        return jsonify({'ok': False, 'msg': '需要管理员权限'}), 403
    url = request.form.get('url', '')
    _impl().del_source(url)
    return jsonify({'ok': True, 'msg': '已删除'})


@bp.route('/plugin/shuyuan/search')
@login_required
def search():
    url = request.args.get('url', '').strip()
    kw = request.args.get('kw', '').strip()
    if not url or not kw:
        return jsonify({'ok': False, 'items': [], 'msg': '参数缺失'})
    try:
        items = _impl().search_source(JOB, url, kw)
        return jsonify({'ok': True, 'items': items})
    except Exception as e:
        return jsonify({'ok': False, 'items': [], 'msg': str(e)[:120]})


@bp.route('/plugin/shuyuan/start', methods=['POST'])
@login_required
def start():
    if not _admin_required():
        return jsonify({'ok': False, 'msg': '需要管理员权限'}), 403
    kind = request.form.get('kind', 'book')
    arg = request.form.get('arg', '')
    src = request.form.get('src', '')
    with _JOB_LOCK:
        if JOB['running']:
            return jsonify({'ok': False, 'msg': '已有任务在运行'})
        app = current_app._get_current_object()

        def runner():
            with app.app_context():
                try:
                    _impl().run_job(JOB, kind, src, arg)
                except Exception as e:
                    JOB['running'] = False
                    JOB['msg'] = '失败：%s' % e

        threading.Thread(target=runner, daemon=True).start()
        return jsonify({'ok': True, 'msg': '已启动'})


@bp.route('/plugin/shuyuan/stop', methods=['POST'])
@login_required
def stop():
    if not _admin_required():
        return jsonify({'ok': False, 'msg': '需要管理员权限'}), 403
    JOB['running'] = False
    JOB['msg'] = '已请求停止'
    return jsonify({'ok': True, 'msg': '正在停止'})


PLUGIN = Plugin(
    id=PLUGIN_ID,
    name='Legado 书源导入器',
    description='导入「阅读」App 书源 JSON（yckceo 等仓库 id 或直接粘贴），按书源规则搜索并整本入库。仅支持无 JS 规则的书源。',
    version='1.0.0',
    author='514475844',
    homepage=[],
    admin_nav=[{'label': '书源导入器', 'url': '/plugin/shuyuan'}],
    blueprint=bp,
    enabled_by_default=True,
    settings_schema=[
        {'key': 'delay', 'label': '抓取间隔(秒)', 'type': 'float', 'default': 0.5, 'min': 0.2, 'max': 5},
        {'key': 'min_size', 'label': '最小字数', 'type': 'int', 'default': 1000, 'min': 0},
        {'key': 'j2s', 'label': '繁体自动转简体', 'type': 'bool', 'default': True},
        {'key': 'category_root', 'label': '分类根目录名', 'type': 'text', 'default': '书源'},
        {'key': 'proxy_url', 'label': '代理地址(可选)', 'type': 'text', 'default': '',
         'help': '默认直连；被墙的源自行填写代理'},
    ],
)
