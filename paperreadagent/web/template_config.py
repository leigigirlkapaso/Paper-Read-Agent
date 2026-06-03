"""共享 Jinja2Templates 实例，所有路由共用。"""
from pathlib import Path
from fastapi.templating import Jinja2Templates

_templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))
