# 注意事项 / 已知坑（CAUTIONS）

本文件汇总 SweetReader 插件开发与运行中的真实踩坑（含抓取插件）。每次踩坑后在此更新。
如果你写了「建分类 / 抓书」类的插件，第 1、2、3、4 条直接决定你的目录能不能在首页侧栏看到。

---

## 1. 插件建的分类：`parent_id` 绝不能留 NULL（最严重）

**现象**：你「弄了很多插件」建出来的目录在首页侧栏看不到。

**根因**：抓取插件的「站点根分类」在创建时没设 `parent_id`，成了 `books`（id=1）的
**兄弟**而不是**子节点**。首页侧栏只渲染 `books` 的子分类，所以这些目录整棵进不了侧栏，
连带下面的几千本书也一起消失。

**代码位置（本仓库 `grabbers/`）**：
- `_wpsites_impl.py` 第 309 行：`root = Category(name=root_name, path=root_name, level=0, book_count=0)` —— 缺 `parent_id`。
- `_biqu_impl.py` 第 338 行：同样缺 `parent_id`。

**正确写法**：
```python
books_root = Category.query.filter_by(name='books').first()  # id=1
root = Category(name=root_name, path=root_name,
                parent_id=books_root.id, level=1, book_count=0)
```
子分类 `parent_id=root.id` 是对的，只有「根」要改。改完用 `level=1`（它在 `books` 下一层）。

**线上已做数据修复**（实例库）：16 个孤儿分类已重新挂到 `books` 下（`parent_id=1`，`level` 子树校正），
重启后侧栏可见。但**插件源码没改**——下次跑抓取仍会建孤儿。在插件代码里按上面改掉即可根治。

---

## 2. `book_count` 是「直接计数」，文件夹分类会显示 0

**现象**：顶层分类 `book_count=0` 会被侧栏「噪声过滤」当成垃圾分类隐藏。

**规则**：`book_count` 只数「直接挂在该分类下的书」，不含子孙。站点根分类（书都在子分类里）直接计数为 0。

**已修复（框架侧）**：侧栏噪声过滤已加 `has_children` 例外——**有子分类的真文件夹即使直接计数为 0 也视为有效**，
不再整棵藏。插件若自建分类并维护 `book_count`，记得在建书后回写它（或直接计数），否则角标会显示错乱的 0。

---

## 3. 抓取任务类型（`kind`）各家族不一样

触发方式：管理员登录后 `POST /<plugin-id>/start`，表单字段 `kind=...`、`arg=...`。
`<plugin-id>` 是插件 id 里的下划线转连字符，例如 `aaanovel_grabber` → `/aaanovel-grabber/start`。
（少数老壳文件把路由写死成 `/plugin/<site>/start`，要看具体壳文件头。）

各家族支持的 `kind`：

| 家族（实现层）            | 支持 `kind`                | 不支持         |
| -------------------- | ------------------------ | ----------- |
| wpsites（`_wpsites_impl.py`） | `latest` / `by_cat` / `search` | —           |
| biqu（`_biqu_impl.py`）       | `by_cat` / `search` / `update`    | **`latest`** |

**坑**：`biquge365` 传 `kind=latest` 会返回「未知任务类型」。正确用法：`by_cat`（传分类路径）+ `arg`、
或 `search`（传关键词）+ `arg`、或 `update`。wpsites 家族才支持 `latest`。

---

## 4. 代理依赖（Cloudflare 盾站点）

`aaanovel.com` 有 Cloudflare 盾，必须走代理。设置项 `proxy_url` 默认
`http://192.168.10.121:10808`（你 Windows 上的 v2rayN）。

**坑**：容器跑在 OpenWrt 上，**连不上你 Windows 主机的这个代理** → 报 `No route to host`，该站抓取全失败。
要么在 OpenWrt / 容器侧也起一个可达的代理并把 `proxy_url` 改过去，要么保证那台 Windows 一直开机且 v2rayN 在跑。

---

## 5. 抓取线程跑在容器里，不依赖 Windows

`/start` 在 live 容器主进程里起一个 `daemon=True` 的线程持续抓取。所以**关掉 Windows 不影响已在跑的抓取线程**
（除了第 4 条的代理站点会卡住）。`docker restart` 会中断未完成的抓取。

---

## 6. 插件蓝图注册时机

`/api/plugins/scan` 在请求时 `registry.reload()` + `register_blueprint`，但 Flask 在「处理完首个请求后」
禁止再注册蓝图，会报：
```
register_blueprint can no longer be called on the application. It has already handled its first request
```
框架在工厂启动时已自动注册发现的插件蓝图，正常用没问题。**不要在你的路由处理器里动态 `register_blueprint`。**

---

## 7. 壳 + 热加载架构（抓取插件）

每个抓取插件 = 一个「壳」`.py`（路由固定）+ 一个 `_*_impl.py`（逻辑，按 mtime 热加载，改逻辑无需重启）
+ `templates/<x>.html`（页面）。更新逻辑只改 impl 文件即可；改壳 / template 需要 `docker restart` 或插件页「重新扫描」。

---

## 8. 部署与刷新

见 `README.md`。要点：复制到宿主机 `/mnt/sata1-1/SweetReader/instance/plugins/`
（挂容器 `/app/instance/plugins/`），重启或重新扫描。改完**强刷浏览器（Ctrl+F5）**拉取新资源。

---

## 9. 隐私提醒（与本仓库相关）

- 本仓库已设为 **public**。抓取插件里有站点域名、以及默认代理地址 `192.168.10.121:10808`（局域网 IP，非公网）。
  若不想公开，去 GitHub 仓库 Settings 改回 private，或把代理地址改成占位符再提交。
- 核心仓库的 `sync_push` 脱敏闸门会自动删除路径 / 内网 IP / 设备名等敏感特征，**但本独立仓库不走那套**，
  提交前自己留意别带进真实密钥 / token。
