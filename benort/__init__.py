"""Benort 项目管理器的 Flask 应用工厂。"""

import os

from dotenv import load_dotenv
from flask import Flask

from .config import DEFAULT_PROJECT_NAME, init_app_config


# 读取 .env 配置以加载 OPENAI_API_KEY 等敏感信息
load_dotenv()


def create_app(config: dict | None = None) -> Flask:
    """创建并配置 Flask 应用实例。"""

    app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), "templates"))

    if config:
        app.config.update(config)

    init_app_config(app)

    from .views import bp as routes_bp

    # 注册所有路由蓝图
    app.register_blueprint(routes_bp)

    with app.app_context():
        from .project_store import ensure_project, list_projects

        # 若无任何项目，自动初始化 default 项目
        if not list_projects():
            ensure_project(DEFAULT_PROJECT_NAME)

    return app


# 为 gunicorn 等入口暴露一个默认 app 实例
app = create_app()
