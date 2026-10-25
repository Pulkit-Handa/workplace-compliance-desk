from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from string import Template

import yaml

from compliance.config import settings


@dataclass(frozen=True)
class PromptArtifact:
    task: str
    variant: str
    version: str
    path: str
    text: str
    sha256: str


class PromptRegistry:
    def __init__(self, prompts_dir: Path | None = None, variant: str | None = None):
        self.prompts_dir = prompts_dir or settings.prompts_dir
        self.variant = variant or settings.prompt_variant
        registry_path = self.prompts_dir / "registry.yaml"
        self.registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        if self.variant not in self.registry.get("variants", {}):
            raise ValueError(f"prompt variant {self.variant!r} is not in {registry_path}")
        self._cache: dict[str, PromptArtifact] = {}

    @property
    def variants(self) -> list[str]:
        return sorted(self.registry.get("variants", {}))

    def get(self, task: str, variant: str | None = None) -> PromptArtifact:
        variant = variant or self.variant
        cache_key = f"{variant}:{task}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        try:
            relative = self.registry["variants"][variant]["tasks"][task]
        except KeyError as exc:
            raise KeyError(f"no prompt for task {task!r} in variant {variant!r}") from exc
        path = self.prompts_dir / relative
        text = path.read_text(encoding="utf-8")
        artifact = PromptArtifact(
            task=task,
            variant=variant,
            version=f"{task}-{variant}",
            path=str(path),
            text=text,
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
        self._cache[cache_key] = artifact
        return artifact

    def render(self, task: str, variant: str | None = None, **values: object) -> PromptArtifact:
        artifact = self.get(task, variant)
        rendered = Template(artifact.text).safe_substitute({k: str(v) for k, v in values.items()})
        return PromptArtifact(
            task=artifact.task,
            variant=artifact.variant,
            version=artifact.version,
            path=artifact.path,
            text=rendered,
            sha256=artifact.sha256,
        )

    def versions(self, variant: str | None = None) -> dict[str, str]:
        variant = variant or self.variant
        return {
            task: self.get(task, variant).version
            for task in self.registry["variants"][variant]["tasks"]
        }

    def manifest(self) -> dict:
        return {
            "active_variant": self.variant,
            "variants": {
                name: {
                    "description": data.get("description", ""),
                    "tasks": {
                        task: {
                            "version": f"{task}-{name}",
                            "path": path,
                            "sha256": hashlib.sha256(
                                (self.prompts_dir / path).read_bytes()
                            ).hexdigest(),
                        }
                        for task, path in data.get("tasks", {}).items()
                    },
                }
                for name, data in self.registry.get("variants", {}).items()
            },
            "changelog": self.registry.get("changelog", {}),
        }
