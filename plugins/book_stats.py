"""示例插件：书库统计（较完整示例）。

演示插件框架的能力：
- Blueprint 自定义页面（/plugin/stats），用 render_template_string 内联 HTML（单文件可部署）。
- 首页「扩展功能」入口 + 管理员侧栏入口。
- 图形化设置(settings_schema)：用户在「插件管理」页即可配置，无需写前端。

与核心仓库 src/app/plugins/user/book_stats.py 逻辑一致，但这里改为内联 HTML，
保证“复制单个 .py 到 instance/plugins/ 即可用”，不必再搬运 templates 目录。
"""
from flask import Blueprint, render_template_string
from flask_login import login_required
from app.models import Book, Category, db
from app.plugins import Plugin, PluginEntry, registry

bp = Blueprint('book_stats', __name__)

_ROW = '<tr><td>{k}</td><td>{v}</td></tr>'
_PAGE = """
<div class="sr-plugin-card">
  <h2>书库统计</h2>
  <p>共 <b>{{ total }}</b> 本书。</p>
  {% if show_format %}
  <h3>格式分布</h3>
  <table class="sr-stats">
    {% for t, c in by_format %}<tr><td>{{ t }}</td><td>{{ c }}</td></tr>{% endfor %}
  </table>
  {% endif %}
  {% if show_recent %}
  <h3>最近入库（前 {{ top_n }} 个分类）</h3>
  <table class="sr-stats">
    {% for c in top_cats %}<tr><td>{{ c.name }}</td><td>{{ c.book_count }}</td></tr>{% endfor %}
  </table>
  {% endif %}
</div>
<style>.sr-stats td{padding:2px 10px 2px 0;border-bottom:1px solid #eee}</style>
"""


@bp.route('/plugin/stats')
@login_required
def stats_page():
    cfg = registry.get_settings('book_stats')
    top_n = int(cfg.get('topN', 8))
    show_format = bool(cfg.get('show_format', True))
    show_recent = bool(cfg.get('show_recent', True))

    total = Book.query.count()
    by_format = []
    if show_format:
        by_format = [(t or '未知', c) for t, c in
                     (db.session.query(Book.file_type, db.func.count(Book.id))
                      .group_by(Book.file_type)
                      .order_by(db.func.count(Book.id).desc()).all())]
    top_cats = []
    if show_recent:
        top_cats = (Category.query.filter(Category.book_count > 0)
                    .order_by(Category.book_count.desc()).limit(top_n).all())
    return render_template_string(_PAGE, total=total, by_format=by_format,
                                  top_cats=top_cats, show_format=show_format,
                                  show_recent=show_recent, top_n=top_n)


PLUGIN = Plugin(
    id='book_stats',
    name='书库统计',
    description='查看书库总量、格式分布、热门分类。',
    version='1.1.0',
    author='514475844',
    homepage=[PluginEntry(label='书库统计', url='/plugin/stats', order=50, show_on_homepage=False)],
    admin_nav=[{'label': '书库统计', 'url': '/plugin/stats'}],
    reader_tools=[],
    blueprint=bp,
    enabled_by_default=True,
    settings_schema=[
        {'key': 'topN', 'label': '热门分类数量', 'type': 'int', 'default': 8,
         'min': 1, 'max': 50, 'help': '统计页展示前 N 个分类'},
        {'key': 'show_format', 'label': '显示格式分布', 'type': 'bool', 'default': True,
         'help': '是否展示 TXT/EPUB/DOC 等格式占比'},
        {'key': 'show_recent', 'label': '显示热门分类', 'type': 'bool', 'default': True,
         'help': '是否展示按书量排序的 Top 分类'},
    ],
)
