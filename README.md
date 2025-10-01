# Benort

Benort 是一套面向演讲与教学场景的 **LaTeX Beamer 幻灯片编辑与语音生成平台**。后端基于 Flask，前端整合 CodeMirror、PDF.js 与 Markdown 渲染，配合 OpenAI 提供的 ChatCompletion/TTS 能力，实现从内容创作、排版优化到语音输出的一体化体验。虽说初衷是为了写ppt，其实你还可以用各种模版写论文嗷，可以试一试。

## 功能亮点

- **双模式编辑器**：左侧编辑区可在 LaTeX 与 Markdown 之间无缝切换，右侧自动同步 PDF 或 Markdown 预览，支持内部滚动，切换时界面保持稳定。
- **自动编译与即时提示**：任何编辑、页面切换或模板变动都会自动触发编译；失败时在页面顶部闪现错误，无需额外弹窗。
- **讲稿与笔记管理**：每页独立维护讲稿与备注，可调用 AI 优化；讲稿可用于 TTS 输出，笔记则可导出 Markdown。
- **音频缓存**：合并讲稿或单页文本生成的 MP3 自动缓存在项目 `build/audio/` 下，重复播放无需再次调用 OpenAI TTS API。
- **智能模板系统**：支持从 `temps/` 目录选择现有模板，并在对话框中额外补充自定义宏包/片段；保存后立即生效并参与后续编译。
- **多项目隔离**：`projects/` 目录下的每个子目录即一个独立项目（含 `build/` 等），附件与资源分别存放在根目录的 `attachments_store/<project>/`、`resources_store/<project>/` 中，可在界面上快速切换。
- **资源与附件管理**：内置管理界面支持上传、引用、删除资源/附件；同时可将引用路径自动规范化到 LaTeX 代码中。

## 安装与运行

> 推荐使用 Python 3.11 及以上版本。

```bash
python -m venv venv
source venv/bin/activate  # Windows 使用 venv\Scripts\activate
pip install --upgrade pip
pip install .              # 根据 pyproject.toml 安装依赖
```

配置 OpenAI API 密钥与界面主题：在项目根目录创建 `.env`，例如：

```
OPENAI_API_KEY=你的OpenAI密钥

# UI 主题配置（可选）
BENORT_COLOR_MODE=dark            # light 或 dark
BENORT_NAVBAR_PRESET=modern       # modern 自定义柔和风格；bootstrap 使用原生 Bootstrap
BENORT_NAVBAR_STYLE=palette       # uniform 固定色；palette 使用多彩色
BENORT_NAVBAR_VARIANT=solid       # solid 实心；outline 描边
# BENORT_NAVBAR_COLOR=primary    # uniform 模式下的主色
# BENORT_NAVBAR_PALETTE=primary,success,warning,danger,info,secondary

说明：`BENORT_NAVBAR_PRESET=modern` 使用优化后的圆角按钮（支持柔和 hover 动效）；设为 `bootstrap` 则完全沿用 Bootstrap 原生色系，方便与既有设计保持一致。页码、项目切换按钮等都会随配置统一更新。
```

启动开发服务器：

```bash
flask --app benort run  # 默认监听 http://localhost:5000
```

生产部署示例：

```bash
gunicorn benort:app
gunicorn -w 4 -b 0.0.0.0:5555 benort:app
```

## 项目目录结构

```
Benort/
├─ benort/              # 应用源码（蓝图、模板、配置等）
├─ projects/            # 多项目存储根目录
│  ├─ default/
│  │  ├─ resources/
│  │  ├─ build/
│  │  └─ project.yaml
│  └─ ...               # 其他项目同样结构
├─ attachments_store/   # 每个项目的本地附件目录
│  ├─ default/
│  └─ ...
├─ resources_store/     # 每个项目的本地资源目录
│  ├─ default/
│  └─ ...
├─ temps/               # 可选的默认模板（YAML）
├─ pyproject.toml       # 项目配置与依赖声明
├─ README.md
└─ ...
```

首次启动时若 `projects/` 为空，系统会自动创建 `default` 项目。模板选择对话框会列出 `temps/` 中的所有 `.yaml` 文件，并允许在文本框内进一步补充自定义内容。

## OSS 同步配置

附件支持按项目同步到阿里云 OSS。在设置以下环境变量后，即可在“附件管理”弹窗中开启同步开关：

- `ALIYUN_OSS_ENDPOINT`：OSS Endpoint，例如 `oss-cn-hangzhou.aliyuncs.com`
- `ALIYUN_OSS_ACCESS_KEY_ID` / `ALIYUN_OSS_ACCESS_KEY_SECRET`
- `ALIYUN_OSS_BUCKET`：目标 Bucket 名称
- `ALIYUN_OSS_PREFIX` *(可选)*：对象键前缀，默认 `attachments`
- `ALIYUN_OSS_PUBLIC_BASE_URL` *(可选)*：用于拼接附件外链的自定义域名
- `LOCAL_ATTACHMENTS_ROOT` *(可选)*：覆盖本地附件根目录，默认 `attachments_store`
- `LOCAL_RESOURCES_ROOT` *(可选)*：覆盖本地资源根目录，默认 `resources_store`

同步开关默认关闭，仅在需要向 OSS 同步时再启用，可有效节省流量成本并兼顾本地部署体验。

### `.env` 示例（阿里云华东 2 上海 + UI 配置）

```bash
# OSS 基本配置
ALIYUN_OSS_ENDPOINT=oss-cn-shanghai.aliyuncs.com
ALIYUN_OSS_ACCESS_KEY_ID=your-access-key-id
ALIYUN_OSS_ACCESS_KEY_SECRET=your-access-key-secret
ALIYUN_OSS_BUCKET=your-bucket-name

# 可选：自定义对象前缀 / 外链域名
ALIYUN_OSS_PREFIX=attachments
#ALIYUN_OSS_PUBLIC_BASE_URL=https://cdn.example.com

# 可选：覆盖默认附件/资源存储位置
#LOCAL_ATTACHMENTS_ROOT=/data/benort/attachments
#LOCAL_RESOURCES_ROOT=/data/benort/resources

# UI 主题（可选）
BENORT_COLOR_MODE=dark      # 或 light
BENORT_NAVBAR_PRESET=modern # 或 bootstrap
BENORT_NAVBAR_STYLE=palette # 或 uniform
BENORT_NAVBAR_VARIANT=solid # 或 outline
```

## 常用命令

- `pip install .`：依据 `pyproject.toml` 安装依赖。
- `flask --app benort run`：开发模式启动服务。
- `gunicorn benort:app`：生产模式启动。
- `python -m compileall benort`：快速检查语法。

## 许可协议

自由修改与扩展。如在使用过程中遇到问题，欢迎提交 Issue 或 Pull Request。
