"""项目级配置常量与初始化辅助函数。"""

import os
import textwrap


DEFAULT_PROJECT_NAME = "default"
# 默认模板文件名，可根据需要在 temps 中新增不同方案
DEFAULT_TEMPLATE_FILENAME = "base_template.yaml"

# 若项目未定制模板，使用该结构作为兜底的 LaTeX 片段
FALLBACK_TEMPLATE: dict[str, str] = {
    "header": textwrap.dedent(
        r"""
        \documentclass{beamer}
        \usetheme{Madrid}
        \usecolortheme{seahorse}
        \usepackage{graphicx}
        \usepackage{hyperref}
        \usepackage{booktabs}
        \usepackage{amsmath, amssymb}
        \usepackage{fontspec}
        \usepackage{mwe}
        \usepackage{xeCJK}
        \setCJKmainfont{PingFang SC}
        \setsansfont{PingFang SC}
        \setmainfont{PingFang SC}
        \graphicspath{{.}{images/}{../images/}{../attachments/}{../}}
        \makeatletter
        \newcommand{\img}[2][]{
          \IfFileExists{#2}{\includegraphics[#1]{#2}}{
            \typeout{[warn] Missing image #2, using placeholder}
            \includegraphics[#1]{example-image}
          }
        }
        \makeatother
        \usepackage[backend=bibtex,style=chem-acs,maxnames=6,giveninits=true,articletitle=true]{biblatex}
        \addbibresource{refs.bib}
        \setbeameroption{show notes}
        \title{report}
        \author{Ben}
        """
    ).strip(),
    "beforePages": "\\begin{document}",
    "footer": "\\end{document}",
}

# OSS / 附件存储相关默认配置
DEFAULT_LOCAL_ATTACHMENTS_DIRNAME = "attachments_store"
DEFAULT_LOCAL_RESOURCES_DIRNAME = "resources_store"

# OpenAI ChatCompletion 相关配置
OPENAI_CHAT_COMPLETIONS_MODEL = "gpt-3.5-turbo"

# OpenAI 语音合成参数，可按需调整音色/格式/语速
OPENAI_TTS_MODEL = "tts-1"
OPENAI_TTS_VOICE = "alloy"
OPENAI_TTS_RESPONSE_FORMAT = "mp3"
OPENAI_TTS_SPEED = 1.0

# 不同优化场景对应的系统提示与用户模板
AI_PROMPTS = {
    "script": {
        "system": "你是一个幻灯片演讲稿写作专家，服从我的指示，返回优化后的讲稿文本。",
        "template": (
            "你是一个幻灯片演讲稿写作专家。选择合适的演讲风格，可以进行高级感的幽默和帮助演讲者学习英语。\n"
            "优化对应的讲稿，使其表达更清晰、逻辑更流畅、适合演讲，内容不要脱离幻灯片主题。\n"
            "LaTeX页面内容（其中每一个%后面都是给你的一些指示要求）如下：\n{page_tex}\n\n"
            "原始讲稿（如果没有特别说明，无论原始内容是什么语言，默认输出语言是英文en）如下：\n{content}\n\n"
            "请直接返回优化后的讲稿文本，注意是纯文本演讲稿，没有latex语法，可以备注附上一些演讲技巧提示。\n"
        ),
    },
    "note": {
        "system": "你是一个笔记写作专家，返回优化后的笔记（Markdown）。",
        "template": (
            "你是一个幻灯片笔记/摘要写作专家。请根据下方LaTeX页面内容和原始笔记。\n"
            "生成或优化一份适合阅读和记录的笔记（Markdown格式），保留要点、关键结论和联系。\n"
            "LaTeX页面内容如下：\n{page_tex}\n\n"
            "原始笔记如下：\n{content}\n\n"
            "请直接返回优化后的笔记文本，使用Markdown格式。\n"
            "如果原始笔记为空，则主要根据我的现有笔记进行优化和拓展。\n"
            "输出内容的语言应该和我的笔记原始文本保持一致，主要是en和cn两种。\n"
        ),
    },
    "latex": {
        "system": (
            "你是一个LaTeX Beamer幻灯片专家，只能在当前模板允许的宏包和命令范围内工作。 "
            "禁止添加新的宏包、命令或依赖，确保生成的代码在现有模板下可直接编译。"
        ),
        "template": (
            "请优化以下LaTeX Beamer幻灯片页面内容(每一个%后面都是给你的一些指示）。你需要完成“%”给你的命令任务指示要求，用beamer输出完成的结果！\n"
            "使其更规范、简洁、美观，并保留原有结构：\n{content}\n\n"
            "当前页的笔记内容如下：\n{notes}\n\n"
            "如果上述 LaTeX 内容为空但笔记存在，请基于笔记生成新的、可直接编译的幻灯片内容。并且注意如有需要，记得用多个frame以防止单个页面的内容溢出\n"
            "严格遵守以下规则：\n"
            "1. 只使用当前模板已经加载的宏包({allowed_packages})和命令({custom_macros})，禁止新增宏包、字体或 \\usepackage/\\RequirePackage 指令。\n"
            "2. 不要输出 \\documentclass、\\begin{{document}}、\\end{{document}} 等全局结构，不要出现```latex，```等无关标记，只返回纯LaTeX代码。\n"
            "3. 不允许使用需要额外宏包才能编译的命令，也不要新增 \\newcommand/\\renewcommand/\\DeclareMathOperator 等定义。\n"
            "4. 输出内容的语言应该与我的原有内容一致，除非通过 % 特别要求。主要是 en 和 cn 两种。\n"
        ),
    },
}


def template_library_root(app: object | None = None) -> str:
    """确定可复用 LaTeX 模板所在目录。"""

    if app is not None:
        # 在应用上下文内优先读取配置值
        root = getattr(app, "config", {}).get("TEMPLATE_LIBRARY")  # type: ignore[arg-type]
        if root:
            return root
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "temps"))


def init_app_config(app) -> None:
    """根据应用根目录初始化项目与模板文件夹。"""

    projects_root = os.path.join(app.root_path, "projects")
    app.config.setdefault("PROJECTS_ROOT", projects_root)
    os.makedirs(projects_root, exist_ok=True)

    attachments_root = os.path.join(app.root_path, DEFAULT_LOCAL_ATTACHMENTS_DIRNAME)
    app.config.setdefault("LOCAL_ATTACHMENTS_ROOT", os.environ.get("LOCAL_ATTACHMENTS_ROOT", attachments_root))
    os.makedirs(app.config["LOCAL_ATTACHMENTS_ROOT"], exist_ok=True)

    resources_root = os.path.join(app.root_path, DEFAULT_LOCAL_RESOURCES_DIRNAME)
    app.config.setdefault("LOCAL_RESOURCES_ROOT", os.environ.get("LOCAL_RESOURCES_ROOT", resources_root))
    os.makedirs(app.config["LOCAL_RESOURCES_ROOT"], exist_ok=True)

    # 预先加载 OSS 配置，允许通过环境变量覆盖
    app.config.setdefault("ALIYUN_OSS_ENDPOINT", os.environ.get("ALIYUN_OSS_ENDPOINT"))
    app.config.setdefault("ALIYUN_OSS_ACCESS_KEY_ID", os.environ.get("ALIYUN_OSS_ACCESS_KEY_ID"))
    app.config.setdefault("ALIYUN_OSS_ACCESS_KEY_SECRET", os.environ.get("ALIYUN_OSS_ACCESS_KEY_SECRET"))
    app.config.setdefault("ALIYUN_OSS_BUCKET", os.environ.get("ALIYUN_OSS_BUCKET"))
    app.config.setdefault("ALIYUN_OSS_PREFIX", os.environ.get("ALIYUN_OSS_PREFIX"))
    app.config.setdefault("ALIYUN_OSS_PUBLIC_BASE_URL", os.environ.get("ALIYUN_OSS_PUBLIC_BASE_URL"))

    template_root = template_library_root(app)
    app.config.setdefault("TEMPLATE_LIBRARY", template_root)
    os.makedirs(template_root, exist_ok=True)


__all__ = [
    "DEFAULT_PROJECT_NAME",
    "DEFAULT_TEMPLATE_FILENAME",
    "FALLBACK_TEMPLATE",
    "DEFAULT_LOCAL_ATTACHMENTS_DIRNAME",
    "DEFAULT_LOCAL_RESOURCES_DIRNAME",
    "OPENAI_CHAT_COMPLETIONS_MODEL",
    "OPENAI_TTS_MODEL",
    "OPENAI_TTS_VOICE",
    "OPENAI_TTS_RESPONSE_FORMAT",
    "OPENAI_TTS_SPEED",
    "AI_PROMPTS",
    "init_app_config",
    "template_library_root",
]
