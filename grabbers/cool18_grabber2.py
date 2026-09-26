# -*- coding: utf-8 -*-
"""SweetReader 插件：cool18 禁忌书屋抓取器（稳定壳）

设计：本文件只包含固定的 Blueprint 路由，真正逻辑在 _cool18_impl.py。
_ 前缀文件不会被插件扫描器加载；壳每次请求按 mtime 热加载实现文件，
因此更新 _cool18_impl.py 后无需重启容器、无需重新扫描，刷新页面即生效。
只有本壳文件或插件元数据（名称/设置表单）变化时，才需在「插件管理」点一次重新扫描。
"""
import os
import threading

from flask import Blueprint, request, jsonify, render_template_string, current_app
from flask_login import login_required, current_user

from app.plugins import Plugin, PluginEntry

PLUGIN_ID = 'cool18_grabber2'
_INSTANCE_DIR = os.environ.get('INSTANCE_DIR', '/app/instance')
_IMPL_PATH = os.path.join(_INSTANCE_DIR, 'plugins', '_cool18_impl.py')
_TPL_PATH = os.path.join(_INSTANCE_DIR, 'plugins', 'templates', 'cool18.html')

bp = Blueprint(PLUGIN_ID, __name__)

# 稳定的任务状态（不随实现文件热加载而丢失）
JOB = {"running": False, "kind": "", "msg": "", "log": [], "done": 0, "total": 0,
       "started": "", "finished": ""}
JOB_LOCK = threading.Lock()
_IMPL_CACHE = {"mtime": 0, "mod": None}


def _impl():
    """按 mtime 热加载实现模块"""
    import importlib.util
    mtime = os.path.getmtime(_IMPL_PATH)
    if _IMPL_CACHE["mod"] is None or _IMPL_CACHE["mtime"] != mtime:
        spec = importlib.util.spec_from_file_location('_cool18_impl_live2', _IMPL_PATH)
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


@bp.route('/plugin/cool182')
@login_required
def page():
    ctx = _impl().page_data(JOB)
    ctx['urls'] = {'start':'/plugin/cool182/start','stop':'/plugin/cool182/stop','status':'/plugin/cool182/status','search':'/plugin/cool182/search'}
    return _render(**ctx)


@bp.route('/plugin/cool182/status')
@login_required
def status():
    return jsonify(_impl().status_data(JOB))


@bp.route('/plugin/cool182/start', methods=['POST'])
@login_required
def start():
    if not _admin_required():
        return jsonify({'ok': False, 'msg': '需要管理员权限'}), 403
    kind = request.form.get('kind', 'latest')
    tids = request.form.get('tids', '')
    ok, msg = _start_job(kind, tids)
    return jsonify({'ok': ok, 'msg': msg})


@bp.route('/plugin/cool182/stop', methods=['POST'])
@login_required
def stop():
    if not _admin_required():
        return jsonify({'ok': False, 'msg': '需要管理员权限'}), 403
    JOB['running'] = False
    JOB['msg'] = '已请求停止'
    return jsonify({'ok': True, 'msg': '正在停止（当前帖抓完后退出）'})


@bp.route('/plugin/cool182/search')
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


def _start_job(kind, tids=''):
    with JOB_LOCK:
        if JOB['running']:
            return False, '已有任务在运行'
        app = current_app._get_current_object()

        def runner():
            with app.app_context():
                try:
                    _impl().run_job(JOB, kind, tids)
                except Exception as e:  # 保险
                    JOB['running'] = False
                    JOB['msg'] = '失败：%s' % e

        th = threading.Thread(target=runner, daemon=True)
        th.start()
        return True, '已启动'


PLUGIN = Plugin(
    id=PLUGIN_ID,
    name='cool18 书屋抓取（新版）',
    description='抓取 cool18 禁忌书屋小说：自动合并连载、按站内标签分类、去广告去重、搜索下载、增量更新。',
    version='1.2.1',
    author='514475844',
    homepage=[],
    admin_nav=[{'label': 'cool18 抓取', 'url': '/plugin/cool182'}],
    blueprint=bp,
    enabled_by_default=True,
    settings_schema=[
        {'key': 'pages', 'label': '抓取列表页数', 'type': 'int', 'default': 1, 'min': 1, 'max': 1000,
         'help': '每页约 100 帖；页数越多回填越深，耗时按页数增长'},
        {'key': 'aifilter', 'label': '过滤 AI 贴', 'type': 'bool', 'default': True},
        {'key': 'delay', 'label': '抓取间隔(秒)', 'type': 'float', 'default': 0.6, 'min': 0.2, 'max': 5},
        {'key': 'max_parts', 'label': '单书最大分帖数', 'type': 'int', 'default': 100, 'min': 5, 'max': 300},
        {'key': 'min_size', 'label': '最小字数', 'type': 'int', 'default': 2000, 'min': 0,
         'help': '正文字数低于此值的短帖不入库'},
        {'key': 'j2s', 'label': '繁体自动转简体', 'type': 'bool', 'default': True,
         'help': '入库正文与书名自动繁转简（内嵌 OpenCC 词表）'},
        {'key': 'cat_pages', 'label': '按分类抓取页数', 'type': 'int', 'default': 50, 'min': 1, 'max': 1000,
         'help': '「按分类抓取」时扫描的列表页数，每页约 100 帖'},
        {'key': 'category_root', 'label': '分类根目录名', 'type': 'text', 'default': 'cool18'},
    ],
)
