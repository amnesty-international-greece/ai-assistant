"""The example config and the dependency list must match reality (B8).

An operator setting this up copies `config.yaml.example` and installs from
`pyproject.toml`. Anything the example omits is a setting they will never know
exists; anything it names that the model ignores is a setting they will change
with no effect; a dependency nobody declared is an import error on a fresh
machine.
"""
from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import yaml
from pydantic import BaseModel

from src.config import Settings

ROOT = Path(__file__).resolve().parent.parent

# Import name -> the distribution that provides it.
_DISTRIBUTIONS = {
    "PIL": "pillow",
    "PyPDF2": "PyPDF2",
    "apscheduler": "APScheduler",
    "av": "av",
    "discord": "discord.py",
    "docx": "python-docx",
    "faster_whisper": "faster-whisper",
    "google": "google-auth",            # also google-genai; both declared
    "google_auth_oauthlib": "google-auth-oauthlib",
    "googleapiclient": "google-api-python-client",
    "numpy": "numpy",
    "pillow_heif": "pillow-heif",
    "pydantic_settings": "pydantic-settings",
    "yaml": "pyyaml",
}
_TEST_ONLY = {"pytest"}


def _model_paths(model: type[BaseModel], prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    for name, field in model.model_fields.items():
        annotation = field.annotation
        path = f"{prefix}{name}"
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            out.update(_model_paths(annotation, path + "."))
        else:
            out[path] = annotation
    return out


def _yaml_paths(data: dict, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in (data or {}).items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            out[path] = value          # the mapping itself is a configured value
            out.update(_yaml_paths(value, path + "."))
        else:
            out[path] = value
    return out


def _example() -> dict[str, object]:
    return _yaml_paths(yaml.safe_load((ROOT / "config.yaml.example").read_text(encoding="utf-8")))


def test_every_key_in_the_example_reaches_the_model():
    model = _model_paths(Settings)
    unknown = [
        key for key in _example()
        if key not in model
        # a section holding known settings, or a key inside a dict-typed one
        and not any(m.startswith(key + ".") or key.startswith(m + ".") for m in model)
    ]
    assert not unknown, f"config.yaml.example sets values the model ignores: {unknown}"


def test_settings_that_belong_in_the_yaml_are_shown_in_the_example():
    """Secrets live in .env; everything else should be visible in the example."""
    secrets = set(Settings.model_fields) - {
        name for name, field in Settings.model_fields.items()
        if isinstance(field.annotation, type) and issubclass(field.annotation, BaseModel)
    }
    example = _example()
    missing = [
        path for path in _model_paths(Settings)
        if "." in path and path not in example
        and not any(path.startswith(s + ".") for s in secrets)
    ]
    assert not missing, f"Settings absent from config.yaml.example: {sorted(missing)}"


def test_env_example_and_the_settings_agree():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    documented = {m.group(1).lower() for m in re.finditer(r"^([A-Z0-9_]+)=", text, re.M)}
    env_fields = {
        name for name, field in Settings.model_fields.items()
        if not (isinstance(field.annotation, type) and issubclass(field.annotation, BaseModel))
    }
    assert not (documented - env_fields), (
        f".env.example documents variables nothing reads: {sorted(documented - env_fields)}"
    )
    assert not (env_fields - documented), (
        f"Settings read variables .env.example never mentions: {sorted(env_fields - documented)}"
    )


def test_one_dependency_file_and_it_declares_every_import():
    assert not (ROOT / "requirements.txt").exists(), (
        "pyproject.toml is the single dependency file; requirements.txt drifted from it"
    )
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = {
        re.split(r"[<>=\[]", spec)[0].strip().lower()
        for spec in project["dependencies"]
        + [s for group in project.get("optional-dependencies", {}).values() for s in group]
    }

    imported: set[str] = set()
    for path in (ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])

    import sys
    third_party = {
        name for name in imported
        if name not in sys.stdlib_module_names and name not in {"src"} | _TEST_ONLY
    }
    undeclared = sorted(
        name for name in third_party
        if _DISTRIBUTIONS.get(name, name).lower() not in declared
    )
    assert not undeclared, f"imported but not declared in pyproject.toml: {undeclared}"
