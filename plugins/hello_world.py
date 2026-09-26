"""示例插件：Hello World（最小可部署插件）。

部署：把本文件复制到宿主机
    /mnt/sata1-1/SweetReader/instance/plugins/hello_world.py
（该目录挂载到容器 /app/instance/plugins/）
然后 docker restart sweetreader（或在插件管理页点「重新扫描」）。
插件即在首页「扩展功能」面板与管理员侧栏出现入口。

特点：
- 单文件即可部署（页面用 render_template_string 内联 HTML，无需模板目录）。
- 演示 homepage 入口 + admin_nav + 图形化设置(settings_schema)。
"""
from flask import Blueprint, render_template_string
from flask_login import login_required, current_user
from app.plugins import Plugin, PluginEntry, registry

bp = Blueprint('hello_world', __name__)

_PAGE = """
<div class="sr-plugin-card">
  <h2>Hello, {{ u }} 👋</h2>
  <p>这是你的第一个 SweetReader 插件页面。</p>
  <p>当前设置 <code>topN = {{ top_n }}</code>（在「插件管理」页可改）。</p>
  <p style="color:#9a8a8a;font-size:13px;">插件 id：<code>hello_world</code></p>
</div>
"""


@bp.route('/plugin/hello')
@login_required
def hello():
    cfg = registry.get_settings('hello_world')
    top_n = int(cfg.get('topN', 5))
    return render_template_string(_PAGE, u=getattr(current_user, 'username', '书友'), top_n=top_n)


PLUGIN = Plugin(
    id='hello_world',
    name='Hello World',
    description='插件框架最小示例：首页入口 + 图形化设置 + 一个页面。',
    version='1.0.0',
    author='514475844',
    homepage=[PluginEntry(label='Hello', url='/plugin/hello', order=90, show_on_homepage=True)],
    admin_nav=[{'label': 'Hello', 'url': '/plugin/hello'}],
    blueprint=bp,
    enabled_by_default=True,
    settings_schema=[
        {'key': 'topN', 'label': '示例数值', 'type': 'int', 'default': 5,
         'min': 1, 'max': 100, 'help': '仅作演示，可在插件管理页修改'},
    ],
)
