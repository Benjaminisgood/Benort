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

### Markdown 标题跳转小贴士

Markdown 预览面板内的链接支持快速跳转到标题位置，既可以定位当前页，也可以跨页跳转：

- **当前页标题**：`[跳转到标题](#目标标题)`（标准 Markdown 语法）。
- **按页码跨页**（页码会随拖拽重排变化）：`[跳转到第 3 页的标题](page:3#目标标题)` 或 `[跳转](p5#目标标题)`。
- **按 pageId 精准跨页**（推荐，拖拽排序后仍然有效）：复制目标页的 `pageId`，然后使用  
  - URL 查询格式：`[跳转到目标页](?pageId=39792fadac1f45799a4eea827f1323be#目标标题)`  
  - 简写格式：`[跳转到目标页](pageId:39792fadac1f45799a4eea827f1323be#目标标题)`

链接点击后会自动切换到对应页面、滚动到标题位置并短暂高亮，便于确认跳转是否成功。

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

开启同步后，对象会以“项目”为单位写入 OSS：

- `<前缀>/<项目名>/attachments/...`
- `<前缀>/<项目名>/resources/...`
- `<前缀>/<项目名>/.yaml/project.yaml`

其中 `<前缀>` 来自 `ALIYUN_OSS_PREFIX`（若未设置，则使用默认值）。

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


## bug与改进方向
现在通过”页面拖拽排序“的弹窗进行排序时，对resources:的同步更新支持有问题，排序的更改会导致不属于该资源的页面页被加上该资源,帮我进行修正。
此外，对于资源和附件的管理，当一个文件被多个页面用到，应当实现：删除一个页面的关联不会彻底删除文件，只有当删除最后一处关联，即此时没有其他页面相关时，才会删除源文件（包括同步在oss里的）。附件的话也是，只不过可能要稍微复杂一些，因为它涉及到两种链接，需要进一步在整个yaml内容中寻找。最好能自动检查是否有附件是没有被引用的可以提醒用户删除。

在markdown版本的的插入里，加上一个模版类，其第一个是blog front matter模版，如下只是一个例子，你需要换成模版：
---
cover: https://ars.els-cdn.com/content/image/1-s2.0-S100184172300829X-gr4.jpg
date: '2025-10-08'
status: draft
summary: Deep Chinese analysis of the 2024 Chinese Chemical Letters paper “Producing circularly polarized luminescence by radiative energy transfer from achiral metal–organic cage to chiral organic molecules,” explaining the novel radiative energy transfer (RET)-based CPL mechanism between achiral Zn₈L₆ cages and chiral BINOL–BODIPY molecules, along with white-light CPL generation.
tags:
- CPL
- MOC
- supramolecular
- energy transfer
- photophysics
- chiral materials
title: "Radiative Energy Transfer in Achiral–Chiral Systems: 中文详解《Producing Circularly Polarized Luminescence by Radiative Energy Transfer from Achiral Metal–Organic Cage to Chiral Organic Molecules》"
---
除此blog front matter模版以外，你可以加上更多的模版，比如日记、记账、笔记等等

如果本地有这个附件和资源，默认先从本地打开，没用的话再走oss，节省流量。

新增支持当前项目和全项目两种检索方式，都是针对project.yaml文件的。

支持在config里面打开嵌入在演讲稿下面的https://colab.research.google.com 或者网页版vscode jupyter lab。这样就实现了latex、markdown、演讲script、python代码运行的完整工作站。

页面拖拽支持跨项目拖拽，如果可以的话，鼠标停留可以查看页面缩略图。





新增导航栏按钮“ai助理”，实现调出学习弹窗，学习的内容可以是来自于鼠标选中内容（支持检测选中的内容）和手动输入想要学习的内容，然后下面是学习方式按钮（其实就是不同的ai提示词）

默认提示词：
- 句子英语学习：翻译、优化选中内容、知识介绍
- 单词英语学习：翻译、查看词根词缀近反义词例句等单词词典内容、单词相关的知识补充与常识
- 新的知识概念：指第一次遇到的概念
- 代码学习解析：除了介绍语法，还要有额外的相关补充

自定义提示词：
- 支持在弹窗里面添加新提示词，支持永久保存反复使用

无提示词：
- 直接将学习内容（选中的或者输入的）交给ai提问，不需要额外提示词

点击提示词即可自动进行提问学习，ai会在所有提示词的后面即刻输出学习结果，每点一个就会自动多一个输出内容框，用户可以对其进行编辑以及选择是否记录。记录时后端会自动把统一个输入的学习记录存储到统一个learn_project.yaml条目中，包含学习方式的名称和用户手动优化后的输出结果，文本格式为支持latex的markdown。

弹窗里也支持对所有提示词的查看、编辑、删除操作。实现方法为点击按钮之后点击对应的提示词即可。

代码结构、提示词存储位置等需要与我现有的匹配，不要改变我的整体代码规范性。如果难以实现或者你认为不必要检测选中内容的话，就用拷贝的方式，到弹窗里再粘贴也行。但是最好可以识别选中内容，并且最好是ai还能自动将学习内容的上下文带进去，这样ai的回答更加准确和针对性。

代码保持现在这样只有一个html文件。

新增导航栏按钮“ai复习助理”，读取learn_project.yaml里的条目内容，弹窗最上面是一个检索框，可以检索并即时列出筛选匹配项，选中某一项后通过文本框展示该条目下对应的所有内容。当然，如果检索框没有任何内容关键词，我也点击了搜索按钮，也不报错，而是进入复习模式，即点一下随机获取并填入一个已有的学习内容，但此时先不出现该条目的内容，先让用户自测一下，再点击一下搜索按钮才出现内容。继续点击搜索按钮则换另一个学习内容，再次点击出结果，继续换词，以此循环往复的随机对讲解内容和想法记录进行复习。对于自认为质量高的每条记录，还有按钮支持选择性加上收藏，从而在yaml里对该条记录标注为收藏。当然，对于觉得很简单的学习内容也支持删除！

更像是一个浏览器插件的功能，不再进行这个修改，防止应用变得没用冗余。

benort优化支持对手机的窄屏幕的适配，不要改变已有的大屏幕的体验以及维持仅一个html文件包所有的前提下，为了适配手机端宅屏端，在窄屏下默认锁死在编辑markdown界面，能用的功能只有在default项目里支持换页写markdown，其它的功能全部隐藏不给用。

新增双击菜单栏的页码前往当前project的第一页的功能。（这样结合跳转功能，第一页可以作为目录）