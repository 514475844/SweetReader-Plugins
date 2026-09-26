# SweetReader-Plugins

你自写 SweetReader 插件的**独立仓库**。

核心仓库 `SweetReader` 只收录随包发布的内置/官方插件（`src/app/plugins/builtin/`、`src/app/plugins/user/`）。  
你自己开发的插件不应该塞进核心仓库（核心仓库的 `instance/` 是被 gitignore 的持久卷，重部署不丢）。  
所以这个独立仓库就是你的插件“家”：版本管理、备份、分发都从这里走，部署时只把单个 `.py` 文件丢进运行实例即可。

---

## ⚠️ 注意事项（必读）

真实踩坑与抓取插件专属坑都汇总在 **[CAUTIONS.md](CAUTIONS.md)**（分类 orphan、代理、任务类型、蓝图时机、隐私等）。
本仓库现在包含：

- `plugins/`：两个**示例**插件（`hello_world`、`book_stats`），用于学习框架。
- `grabbers/`：你**实际在跑的抓取插件**（从运行实例 `instance/plugins/` 同步），详见 [grabbers/README.md](grabbers/README.md)。
- `CAUTIONS.md`：注意事项 / 已知坑。

---

## 插件是怎么被加载的

SweetReader 的 `PluginRegistry` 会从三个地方发现插件（详见核心仓库 `src/app/plugins/__init__.py`）：

| 来源           | 路径                      | 说明                            |
| ------------ | ----------------------- | ----------------------------- |
| builtin      | `app/plugins/builtin/`  | 随核心代码发布，进仓库                   |
| user（随包）     | `app/plugins/user/`     | 随核心代码发布，进仓库                   |
| **instance** | `instance/plugins/*.py` | **你自己丢的插件，持久卷，重部署不丢，无需改核心代码** |



> 本仓库的插件部署目标就是上表的 **`instance/plugins/`**。

一个插件 = 一个 Python 模块，定义 `PLUGIN = Plugin(...)` 即可被自动发现。  
插件状态（启用/停用/设置）存在运行实例的 `instance/plugins_state.json`，与插件文件分离。

---

## 插件能做什么（`Plugin` 字段速查）

- `homepage`: 在首页「扩展功能」面板露出入口（`PluginEntry(label, url, widget, html_url, order, show_on_homepage)`）
- `blueprint`: 一个 Flask Blueprint → 插件拥有自己的路由 + 页面（自包含页面最推荐用 `render_template_string` 内联 HTML，避免模板目录问题）
- `reader_tools`: 在阅读页工具栏注入按钮（`[{label, url, icon?}]`）
- `admin_nav`: 在管理员侧栏注入链接（`[{label, url}]`）
- `settings_schema`: 声明一组设置字段（text/int/float/bool/select/textarea），框架在「插件管理」页自动渲染图形化表单，无需写前端
- `settings_blueprint`: 通用表单不够用时，自定义设置页 Blueprint
- `enabled_by_default` / `id` / `name` / `description` / `version` / `author`

在插件代码里读自己的设置：

```python
from app.plugins import registry
cfg = registry.get_settings('your_plugin_id')
limit = int(cfg.get('limit', 10))
```

---

## 开发一个插件（最小示例）

见 `plugins/hello_world.py`：

```python
from flask import Blueprint, render_template_string
from flask_login import login_required, current_user
from app.plugins import Plugin, PluginEntry, registry

bp = Blueprint('hello_world', __name__)

@bp.route('/plugin/hello')
@login_required
def hello():
    cfg = registry.get_settings('hello_world')
    return render_template_string('<h2>Hello {{ u }}!</h2>', u=getattr(current_user, 'username', '书友'))

PLUGIN = Plugin(
    id='hello_world',
    name='Hello World',
    description='最小示例',
    homepage=[PluginEntry(label='Hello', url='/plugin/hello', order=90)],
    blueprint=bp,
    settings_schema=[{'key': 'topN', 'label': '示例数值', 'type': 'int', 'default': 5}],
)
```

`plugins/book_stats.py` 是更完整的示例（Blueprint 页面 + 管理员侧栏 + 图形化设置 + 真实查库）。

---

## 部署到运行实例

插件目录在宿主机上挂载自容器，路径是：

```
宿主机：<宿主部署目录>/instance/plugins/<你的插件id>.py
容器：  /app/instance/plugins/<你的插件id>.py
```

步骤：

1. 把 `plugins/<name>.py` 复制到宿主机的 `instance/plugins/` 下（文件名随意，但建议用插件 id）。
2. 重启容器，或在「插件管理」页点「重新扫描」：
   ```bash
   docker restart sweetreader
   ```
3. 到「插件管理」页确认插件出现、启用，配置设置。

> 需要模板文件的高级插件：Blueprint 用 `template_folder='templates'` 时，模板需放到  
> `instance/plugins/templates/<xxx>.html`（与 `.py` 同级），文件名请保持唯一避免互相覆盖。  
> 为简单起见，本仓库示例一律用 `render_template_string` 内联 HTML，单文件即可部署。

---

## 本地 git 约定

- 每个插件一个 `<id>.py`，放在 `plugins/`。
- 不要提交 `__pycache__/`、`*.pyc`（已写进 `.gitignore`）。
- 提交身份用你的 GitHub 登录名 + noreply 邮箱，避免泄露真实邮箱。

---

## 远程部署小贴士（可选）

如果在本机用 `tools/ssh_run.py`（核心仓库里）做自动同步，可写个小脚本把 `plugins/*.py`  
逐个 `scp`/put 到 `<宿主部署目录>/instance/plugins/`，再 restart。本项目不耦合核心仓库，  
保持“源码在此、部署到实例”的清晰边界。
