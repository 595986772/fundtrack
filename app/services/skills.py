"""Skill Loading 服务。

s07 Skill Loading 要学习的重点是“知识按需加载”：
- system prompt 里只放轻量目录，也就是有哪些 skill、每个 skill 大概做什么；
- 真正需要完整说明时，再通过 load_skill_tool 读取 SKILL.md；
- 这样可以避免把所有知识一次性塞进上下文。

当前实现只读取当前项目内的 skills/ 目录，不读取用户主目录、.env 或任意系统文件。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import re
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = PROJECT_ROOT / "skills"
MAX_RESOURCE_CHARS = 40_000
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@dataclass(frozen=True)
class SkillCatalogItem:
    """一个 skill 的轻量目录信息。"""

    name: str
    description: str
    folder: str
    skill_path: str
    references: list[str]


class SkillLibraryService:
    """扫描和加载项目内 skill 文件。"""

    def list_skills(self, *, max_results: int = 20) -> list[dict[str, Any]]:
        """只返回轻量 catalog，不返回完整 SKILL.md 内容。"""

        if not SKILLS_ROOT.exists():
            return []

        skills: list[dict[str, Any]] = []

        for skill_dir in sorted(SKILLS_ROOT.iterdir()):
            if len(skills) >= max_results:
                break

            if not skill_dir.is_dir():
                continue

            skill_file = skill_dir / "SKILL.md"

            if not skill_file.is_file():
                continue

            text = self._read_text(skill_file)
            metadata = self._parse_frontmatter(text)
            name = str(metadata.get("name") or skill_dir.name).strip()
            description = str(metadata.get("description") or "").strip()

            if not self._is_valid_skill_name(name):
                name = skill_dir.name

            item = SkillCatalogItem(
                name=name,
                description=description,
                folder=skill_dir.name,
                skill_path=self._relative_path(skill_file),
                references=self._list_references(skill_dir),
            )
            skills.append(asdict(item))

        return skills

    def load_skill(
        self,
        *,
        name: str,
        reference_path: str | None = None,
    ) -> dict[str, Any]:
        """读取某个 skill 的完整内容或受限 reference。

        reference_path 为空时读取 SKILL.md；
        reference_path 不为空时，只允许读取该 skill 的 references/*.md。
        """

        skill_dir = self._safe_skill_dir(name)

        if reference_path:
            target_path = self._safe_reference_path(
                skill_dir=skill_dir,
                reference_path=reference_path,
            )
            resource_type = "reference"
        else:
            target_path = skill_dir / "SKILL.md"
            resource_type = "skill"

        if not target_path.is_file():
            raise ValueError(f"skill 资源不存在：{self._relative_path(target_path)}")

        content = self._read_text(target_path)
        truncated = False

        if len(content) > MAX_RESOURCE_CHARS:
            content = content[:MAX_RESOURCE_CHARS]
            truncated = True

        metadata = self._parse_frontmatter(content)

        return {
            "ok": True,
            "name": name,
            "resource_type": resource_type,
            "path": self._relative_path(target_path),
            "metadata": metadata,
            "content": content,
            "char_count": len(content),
            "truncated": truncated,
            "message": "Skill 内容已按需加载到当前 Agent 上下文。",
        }

    def catalog_for_prompt(self, *, max_results: int = 12) -> str:
        """生成适合放进 system prompt 的轻量 skill catalog。"""

        skills = self.list_skills(max_results=max_results)

        if not skills:
            return "当前没有发现可用 skill。"

        lines = [
            "当前可用 skills catalog（只包含轻量目录，不包含完整正文）：",
        ]

        for item in skills:
            description = item["description"] or "无描述"
            lines.append(f"- {item['name']}: {description}")

        return "\n".join(lines)

    def _safe_skill_dir(self, name: str) -> Path:
        """根据 skill 名称找到安全目录。"""

        normalized_name = str(name or "").strip()

        if not self._is_valid_skill_name(normalized_name):
            raise ValueError("skill name 只能包含小写字母、数字和短横线。")

        skill_dir = (SKILLS_ROOT / normalized_name).resolve()

        try:
            skill_dir.relative_to(SKILLS_ROOT.resolve())
        except ValueError as exc:
            raise ValueError("skill 目录必须位于当前项目 skills/ 下。") from exc

        if not skill_dir.is_dir():
            raise ValueError(f"未找到 skill：{normalized_name}")

        return skill_dir

    def _safe_reference_path(self, *, skill_dir: Path, reference_path: str) -> Path:
        """只允许读取某个 skill 下 references/*.md。"""

        raw_reference_path = str(reference_path or "").strip()

        if not raw_reference_path or "\x00" in raw_reference_path:
            raise ValueError("reference_path 不能为空，也不能包含非法字符。")

        references_dir = (skill_dir / "references").resolve()
        target_path = (skill_dir / raw_reference_path).resolve()

        try:
            target_path.relative_to(references_dir)
        except ValueError as exc:
            raise ValueError("reference_path 只能位于该 skill 的 references/ 目录。") from exc

        if target_path.suffix.lower() != ".md":
            raise ValueError("当前只允许加载 .md reference。")

        return target_path

    def _list_references(self, skill_dir: Path) -> list[str]:
        """列出该 skill 下可按需加载的 reference 文件。"""

        references_dir = skill_dir / "references"

        if not references_dir.is_dir():
            return []

        references: list[str] = []

        for path in sorted(references_dir.glob("*.md")):
            # 这里返回相对 skill 目录的路径。
            # 这样模型可以直接把 catalog 里的 references/xxx.md 传给 load_skill_tool。
            references.append(path.relative_to(skill_dir).as_posix())

        return references

    def _parse_frontmatter(self, text: str) -> dict[str, str]:
        """解析 SKILL.md 顶部的极简 YAML frontmatter。

        当前只需要 name / description，所以不用引入额外 YAML 依赖。
        """

        if not text.startswith("---\n"):
            return {}

        end_index = text.find("\n---\n", 4)

        if end_index == -1:
            return {}

        metadata: dict[str, str] = {}
        frontmatter = text[4:end_index]

        for line in frontmatter.splitlines():
            if ":" not in line:
                continue

            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip().strip('"').strip("'")

        return metadata

    def _read_text(self, path: Path) -> str:
        """统一读取 UTF-8 文本。"""

        return path.read_text(encoding="utf-8")

    def _relative_path(self, path: Path) -> str:
        """返回项目内相对路径，方便接口展示。"""

        return path.resolve().relative_to(PROJECT_ROOT).as_posix()

    def _is_valid_skill_name(self, name: str) -> bool:
        """skill 目录名和 frontmatter name 的安全格式。"""

        return bool(SKILL_NAME_PATTERN.fullmatch(str(name or "")))


skill_library_service = SkillLibraryService()
