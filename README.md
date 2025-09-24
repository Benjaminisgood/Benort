# Beamer AI 演讲稿编辑与语音生成平台

本项目是一个基于 Flask + Bootstrap + OpenAI API 的可视化 Beamer 幻灯片编辑与英文演讲辅助平台。支持在线编辑 LaTeX Beamer、AI 优化演讲稿、自动生成美式英语语音、参考文献管理、PDF 导出等一站式功能。

## 主要功能

- **可视化编辑**：左侧 CodeMirror 编辑器实时编辑每页 LaTeX Beamer 代码，右侧 PDF.js 预览编译结果。
- **笔记/讲稿管理**：每页可单独输入演讲笔记，支持 AI 优化（GPT-4）生成更流畅的英文讲稿。
- **AI 语音合成**：一键将当前页或全部笔记合并，通过 OpenAI TTS API 生成高质量美式英语 mp3 音频，支持在线播放和批量导出。
- **参考文献管理**：菜单栏“参考文献”按钮，输入 DOI 或链接，AI 自动补全并生成标准 bibtex 条目，支持点击跳转原文。
- **AI 优化 LaTeX**：对每页 LaTeX 代码可调用 GPT-4 优化，提升排版与表达。
- **页面拖拽排序**：支持页面顺序拖拽调整，操作直观。
- **模板自定义**：支持编辑全局 LaTeX 模板头部、主体、结尾。
- **文件导入导出**：支持上传/下载 .tex 文件，导出 PDF、全部音频（mp3）。
- **数据持久化**：所有内容（页面、笔记、模板、参考文献）均存储于 YAML 文件，支持断点续编。

## 技术栈

- 后端：Flask, PyYAML, requests, openai, python-dotenv, werkzeug, gunicorn
- 前端：Bootstrap 5, CodeMirror, PDF.js
- AI能力：OpenAI GPT-4 (ChatCompletion), OpenAI TTS (audio.speech)
- 依赖管理：requirements.txt（见下）

## 快速开始

1. 安装依赖

```sh
source venv/bin/activate
. venv/bin/activate
pip install -r requirements.txt
```

1. 配置 OpenAI API 密钥

在根目录新建 `.env` 文件，内容如下：

```markdown
OPENAI_API_KEY=你的OpenAI密钥
```

1. 启动服务

```sh
flask --app benort run
# 或生产环境
gunicorn benort:app
gunicorn -w 4 -b 0.0.0.0:5005 benort:app
```

1. 浏览器访问 `http://localhost:5000`，即可使用全部功能。

## 特色说明

- 支持多轮 AI 优化，风格可定制。
- 语音合成采用 OpenAI 最新 TTS，音质媲美真人。
- 参考文献管理智能化，自动补全 bibtex 并可跳转原文。
- 所有数据本地 YAML 存储，安全可控。
- 支持自定义 Beamer 模板，适合学术/会议/教学等多场景。

---

如需进一步定制或扩展功能，请在 issues 留言或自行修改本项目代码。
现在优化后端路径结构，支持多个不同的项目，每个项目是独立的，有自己的static和uploads，一个项目对应一个路径，保证结构的规范性和可迁移性，后端可以自动检测有多少个项目（文件夹名即项目名），可以来回切换和查看。

项目目录：在项目根目录下新增 `projects/` 文件夹，每个子文件夹为一个独立项目，示例结构：

git add .
git status
git commit -m "first commit"
git branch -M main
git remote add origin https://github.com/Benjaminisgood/Benort.git
git push -u origin main


## 模板库

- 基础 Beamer 模版存放在 `temps/base_template.yaml`，包含 `header`、`beforePages` 与 `footer` 字段，可直接编辑用于自定义默认样式。
- 修改该文件后，无需重启即可生效，如需强制刷新可在运行中调用 `benort.template_store.refresh_template_cache()`。
