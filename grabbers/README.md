# 抓取插件（grabbers）

本目录是你**实际在跑的「书源抓取」插件**，已从运行实例 `instance/plugins/` 同步过来。
它们把各小说站的文章抓成 SweetReader 的书，并按站建分类。

> 这些是「真插件」，示例学习用的最小插件在仓库根的 `plugins/` 目录。

---

## 文件结构

- `<site>_grabber.py`：**壳文件**（固定路由 + `Plugin` 声明）。路由前缀 = 插件 id 的下划线转连字符。
- `_<family>_impl.py`：**共享实现层**，按 mtime 热加载（改逻辑无需重启）。
  - `_wpsites_impl.py`：WordPress 情色小说站群（aaanovel / dfjstory / hhhbook / novel1000 / sosing / springnovel / xxxnovel）
  - `_biqu_impl.py`：笔趣阁类（biquge365）
  - `_canovel_impl.py` / `_cool18_impl.py` / `_cool18_t2s.py`：各自独立逻辑（`_cool18_t2s.py` 负责繁→简）
- `templates/*.html`：各站管理页面（biqu.html / canovel.html / cool18.html / wpsites.html）。

---

## 站点清单（来自壳文件头注释）

| 插件 id | 站 | 家族 / impl | 支持的 `kind` | 备注 |
| --- | --- | --- | --- | --- |
| `aaanovel_grabber` | aaanovel.com | wpsites | latest / by_cat / search | **需代理**（Cloudflare 盾） |
| `dfjstory_grabber` | dfjstory | wpsites | latest / by_cat / search | |
| `hhhbook_grabber` | hhhbook | wpsites | latest / by_cat / search | |
| `novel1000_grabber` | novel1000 | wpsites | latest / by_cat / search | |
| `sosing_grabber` | sosing | wpsites | latest / by_cat / search | |
| `springnovel_grabber` | springnovel | wpsites | latest / by_cat / search | |
| `xxxnovel_grabber` | xxxnovel | wpsites | latest / by_cat / search | |
| `biquge365_grabber` | biquge365 | biqu | by_cat / search / **update** | **不支持 `latest`** |
| `canovel_grabber` | canovel | canovel | （见 `_canovel_impl.py`） | |
| `cool18_grabber` | cool18 | cool18 | （见 `_cool18_impl.py`） | |
| `cool18_grabber2` | cool18（第二源） | cool18 | | 共用 `_cool18_impl.py` |
| `ranwen8_grabber` | ranwen8 | | | |
| `shuhaige_grabber` | shuhaige | | | |

各站具体域名 / 列表页数 / 最小字数等参数见对应壳文件的 `settings_schema` 与头注释。

---

## 已知坑（详见根目录 `CAUTIONS.md`）

1. **站点根分类 `parent_id=NULL`**（`_wpsites_impl.py:309`、`_biqu_impl.py:338`）→ 侧栏不可见。
   线上数据已修复（16 个孤儿重新挂到 `books`），**但源码待修**：按 `CAUTIONS.md` 第 1 条改成 `parent_id=books.id`。
2. **aaanovel 需代理**（`proxy_url` 默认空 = 直连；填了代理但容器连不到就 `No route to host`，框架会自动降级直连）。
3. **biquge365 不支持 `kind=latest`**，用 `by_cat` / `search` / `update`。
4. 抓取线程跑在容器里，关 Windows 不影响（除代理站点）；`docker restart` 会中断未完成的抓取。

---

## 运行

管理员在「插件管理」页进入各插件页面点「开始」，或：
```
POST /<plugin-id>/start   表单: kind=latest (或 by_cat/search/update)  arg=<分类路径或关键词>
```
例如 aaanovel：`POST /aaanovel-grabber/start`，表单 `kind=latest`。
biquge365：`POST /biquge365-grabber/start`，表单 `kind=by_cat&arg=<分类路径>`。

状态：`GET /<plugin-id>/status`（返回 `job.running` / `job.msg` / 日志）。停止：`POST /<plugin-id>/stop`。
