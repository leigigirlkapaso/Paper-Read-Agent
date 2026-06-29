"""
core/frontend.py
CoreFrontend — 全局前端组件注册与注入管理。
模块通过此层注册浮动面板、全局 JS/CSS，自动注入到所有页面。
"""

from __future__ import annotations

from dataclasses import dataclass

from .decorators import stable, evolving


@dataclass
class GlobalComponent:
    name: str
    template: str           # 相对于模块 templates 目录的模板路径
    mount_point: str        # "body-end" | "head" | "nav-end"
    init_script: str = ""   # 相对于模块 static 目录的 JS 文件路径
    css_file: str = ""      # 相对于模块 static 目录的 CSS 文件路径


class CoreFrontend:
    """
    管理所有模块的全局前端组件。

    模块注册组件后，前端注入层在每个页面渲染时自动合并：
    - {{ core_head_inject }} → CSS <link> 标签
    - {{ core_body_end_inject }} → 组件 HTML 模板
    - {{ core_scripts_inject }} → JS <script> 标签
    """

    def __init__(self):
        self._components: list[GlobalComponent] = []
        self._head_cache: str | None = None
        self._body_cache: str | None = None
        self._scripts_cache: str | None = None

    @evolving
    def register_global_component(
        self,
        *,
        name: str,
        template: str,
        mount_point: str = "body-end",
        init_script: str = "",
        css_file: str = "",
    ) -> None:
        """注册一个全局 UI 组件。"""
        comp = GlobalComponent(
            name=name,
            template=template,
            mount_point=mount_point,
            init_script=init_script,
            css_file=css_file,
        )
        self._components.append(comp)
        self._invalidate_cache()

    @evolving
    def unregister_module(self, module_name: str) -> None:
        """移除某模块的所有全局组件。用于模块卸载。"""
        self._components = [
            c for c in self._components
            if not c.template.startswith(f"{module_name}/")
        ]
        self._invalidate_cache()

    def _invalidate_cache(self):
        self._head_cache = None
        self._body_cache = None
        self._scripts_cache = None

    @stable
    def get_head_inject(self) -> str:
        """返回要注入到 <head> 的 CSS <link> 标签（缓存，仅注册时刷新）。"""
        if self._head_cache is not None:
            return self._head_cache
        import time
        _v = int(time.time())
        tags = []
        seen = set()
        for comp in self._components:
            if comp.css_file and comp.css_file not in seen:
                seen.add(comp.css_file)
                module_name = comp.template.split("/")[0]
                tags.append(
                    f'<link rel="stylesheet" href="/{module_name}/static/{comp.css_file.split("/", 1)[-1]}?v={_v}">'
                )
        self._head_cache = "\n    ".join(tags)
        return self._head_cache

    @stable
    def get_body_end_inject(self) -> str:
        """返回要注入到 </body> 前的组件 HTML（缓存，仅注册时刷新）。"""
        if self._body_cache is not None:
            return self._body_cache
        from jinja2 import Environment, FileSystemLoader
        from pathlib import Path
        import sys

        parts = []
        for comp in self._components:
            if comp.mount_point != "body-end":
                continue
            # 按模块名查找模板目录
            module_name = comp.template.split("/")[0]
            template_base = Path("paperreadagent/modules") / module_name / "templates"
            if template_base.exists():
                env = Environment(loader=FileSystemLoader(str(template_base)), autoescape=True)
                try:
                    tmpl_name = comp.template.split("/", 1)[1]
                    tmpl = env.get_template(tmpl_name)
                    parts.append(tmpl.render())
                except Exception:
                    parts.append(
                        f'<!-- [CoreFrontend] 无法加载模板: {comp.template} -->'
                    )
            else:
                parts.append(
                    f'<!-- [CoreFrontend] 模板目录不存在: {template_base} -->'
                )
        self._body_cache = "\n".join(parts)
        return self._body_cache

    @stable
    def get_scripts_inject(self) -> str:
        """返回要注入到 </body> 前的 JS <script> 标签（缓存，仅注册时刷新）。"""
        if self._scripts_cache is not None:
            return self._scripts_cache
        tags = []
        seen = set()
        for comp in self._components:
            if comp.init_script and comp.init_script not in seen:
                seen.add(comp.init_script)
                module_name = comp.template.split("/")[0]
                tags.append(
                    f'<script src="/{module_name}/static/{comp.init_script.split("/", 1)[-1]}"></script>'
                )
        self._scripts_cache = "\n    ".join(tags)
        return self._scripts_cache
