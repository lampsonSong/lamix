"""存储路径一致性测试：确保所有代码使用 config.py 中的统一定义，而非硬编码。"""

import ast
from pathlib import Path

import pytest

import src.core.config as config_module


class TestPathDefinitions:
    """验证 config.py 中路径定义正确。"""

    def test_skills_dir_under_memory(self):
        """SKILLS_DIR 必须在 memory/ 下。"""
        assert "memory" in str(config_module.SKILLS_DIR)

    def test_projects_dir_under_memory(self):
        """PROJECTS_DIR 必须在 memory/ 下。"""
        assert "memory" in str(config_module.PROJECTS_DIR)

    def test_info_dir_under_memory(self):
        """INFO_DIR 必须在 memory/ 下。"""
        assert "memory" in str(config_module.INFO_DIR)


class TestConfigYamlConsistency:
    """验证 config.yaml 中的路径与 config.py 定义一致。"""

    def _load_yaml(self, path: Path) -> dict:
        import yaml
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def test_config_yaml_skills_path(self):
        """config.yaml 的 skills_path 必须与 SKILLS_DIR 一致。"""
        cfg = self._load_yaml(Path.home() / ".lamix" / "config.yaml")
        expected = str(config_module.SKILLS_DIR)
        actual = cfg.get("skills_path", "")
        assert actual == expected, (
            f"config.yaml skills_path='{actual}' 与 SKILLS_DIR='{expected}' 不一致"
        )

    def test_config_yaml_projects_path(self):
        """config.yaml 的 projects_path 必须与 PROJECTS_DIR 一致。"""
        cfg = self._load_yaml(Path.home() / ".lamix" / "config.yaml")
        expected = str(config_module.PROJECTS_DIR)
        actual = cfg.get("projects_path", "")
        assert actual == expected, (
            f"config.yaml projects_path='{actual}' 与 PROJECTS_DIR='{expected}' 不一致"
        )

    def test_config_yaml_info_path(self):
        """config.yaml 的 info_path 必须与 INFO_DIR 一致。"""
        cfg = self._load_yaml(Path.home() / ".lamix" / "config.yaml")
        expected = str(config_module.INFO_DIR)
        actual = cfg.get("info_path", "")
        assert actual == expected, (
            f"config.yaml info_path='{actual}' 与 INFO_DIR='{expected}' 不一致"
        )


class TestNoHardcodedPaths:
    """验证关键模块不从 config.py 外部硬编码路径。"""

    def _get_imported_names(self, module_path: Path) -> dict[str, str]:
        """解析模块源码，返回 {name: from_what} 的导入映射。"""
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    name = alias.asname or alias.name
                    imports[name] = node.module or ""
        return imports

    def _has_hardcoded_dotlamix_path(self, module_path: Path) -> bool:
        """检查源码中是否存在硬编码的 ~/.lamix/skills/info/projects 路径字符串。"""
        source = module_path.read_text(encoding="utf-8")
        for pattern in [
            '.lamix/skills"',
            ".lamix/skills'",
            '.lamix/info"',
            ".lamix/info'",
            '.lamix/projects"',
            ".lamix/projects'",
        ]:
            if pattern in source:
                return True
        return False

    def test_skills_tools_uses_config_import(self):
        """skills_tools.py 必须从 config 导入 SKILLS_DIR，而非自己定义。"""
        path = Path(__file__).parent.parent / "src" / "core" / "skills_tools.py"
        imports = self._get_imported_names(path)
        assert "SKILLS_DIR" in imports, (
            "skills_tools.py 应从 src.core.config 导入 SKILLS_DIR"
        )
        assert "src.core.config" in imports["SKILLS_DIR"], (
            f"SKILLS_DIR 应从 src.core.config 导入，实际从 '{imports['SKILLS_DIR']}' 导入"
        )

    def test_session_store_uses_config_import(self):
        """session_store.py 必须从 config 导入 SKILLS_DIR 和 PROJECTS_DIR。"""
        path = Path(__file__).parent.parent / "src" / "memory" / "session_store.py"
        imports = self._get_imported_names(path)

        assert "SKILLS_DIR" in imports, "session_store.py 应从 config 导入 SKILLS_DIR"
        assert "PROJECTS_DIR" in imports, "session_store.py 应从 config 导入 PROJECTS_DIR"
        assert "src.core.config" in imports["SKILLS_DIR"], (
            f"SKILLS_DIR 应从 src.core.config 导入，实际从 '{imports['SKILLS_DIR']}' 导入"
        )
        assert "src.core.config" in imports["PROJECTS_DIR"], (
            f"PROJECTS_DIR 应从 src.core.config 导入，实际从 '{imports['PROJECTS_DIR']}' 导入"
        )

    def test_skills_tools_no_hardcoded_paths(self):
        """skills_tools.py 中不应存在硬编码的 .lamix/skills/info/projects 字符串。"""
        path = Path(__file__).parent.parent / "src" / "core" / "skills_tools.py"
        assert not self._has_hardcoded_dotlamix_path(path), (
            f"{path.name} 中存在硬编码路径，应使用 config.SKILLS_DIR 等"
        )

    def test_session_store_no_hardcoded_paths(self):
        """session_store.py 中不应存在硬编码的 .lamix/skills/info/projects 字符串。"""
        path = Path(__file__).parent.parent / "src" / "memory" / "session_store.py"
        assert not self._has_hardcoded_dotlamix_path(path), (
            f"{path.name} 中存在硬编码路径，应使用 config.SKILLS_DIR 等"
        )
