"""`models.yaml` → validated `ModelSpec` objects (`docs/BACKENDS.md` §3).

An invalid catalog is a hard startup failure with a pydantic error naming the bad
field (SPEC §5.4, acceptance L12). There is deliberately no "skip the broken entry
and carry on" path: a silent partial catalog means the desktop app's dropdown
disagrees with the server about what exists.
"""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class CatalogError(Exception):
    """The catalog could not be read or did not validate. Fatal at startup."""


class ModelSpec(BaseModel):
    """One entry in `models.yaml`.

    `model_ref` is backend-dependent — an Ollama tag, a Hugging Face repo, or a
    provider's model string — which is exactly why it is not called `hf_repo`.
    """

    # `model_` is pydantic's protected namespace; these fields are the contract's
    # names, so turn the warning off rather than rename them.
    model_config = ConfigDict(protected_namespaces=(), extra="forbid")

    id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    backend: str = Field(min_length=1)
    model_ref: str = Field(min_length=1)
    params: str | None = None
    quantization: str | None = None
    context_length: int | None = Field(default=None, gt=0)
    estimated_load_seconds: int = Field(ge=0)
    #: Passed through only by backends that can use it (`vllm`).
    args: list[str] = Field(default_factory=list)


class _CatalogFile(BaseModel):
    """The file's own shape, before defaults are applied."""

    model_config = ConfigDict(extra="forbid")

    defaults: dict[str, Any] = Field(default_factory=dict)
    models: list[dict[str, Any]] = Field(min_length=1)


class Catalog:
    """The parsed catalog: ordered, and addressable by id."""

    def __init__(self, specs: list[ModelSpec]) -> None:
        self._specs = list(specs)
        self._by_id = {spec.id: spec for spec in self._specs}

    @property
    def specs(self) -> list[ModelSpec]:
        return list(self._specs)

    @property
    def ids(self) -> list[str]:
        return [spec.id for spec in self._specs]

    def get(self, model_id: str) -> ModelSpec | None:
        return self._by_id.get(model_id)

    def __contains__(self, model_id: object) -> bool:
        return model_id in self._by_id

    def __len__(self) -> int:
        return len(self._specs)

    def __iter__(self) -> Any:
        return iter(self._specs)


def load_catalog(path: Path) -> Catalog:
    """Read and validate `path`. Raises `CatalogError` on anything wrong."""
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CatalogError(f"cannot read catalog {path}: {exc}") from exc

    try:
        raw = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise CatalogError(f"{path} is not valid YAML: {exc}") from exc

    if raw is None:
        raise CatalogError(f"{path} is empty; it must define at least one model")
    if not isinstance(raw, dict):
        raise CatalogError(
            f"{path} must be a mapping with a `models:` key, got {type(raw).__name__}"
        )

    try:
        parsed = _CatalogFile.model_validate(raw)
    except ValidationError as exc:
        raise CatalogError(f"{path} failed validation:\n{exc}") from exc

    specs: list[ModelSpec] = []
    for index, entry in enumerate(parsed.models):
        if not isinstance(entry, dict):
            raise CatalogError(
                f"{path}: models[{index}] must be a mapping, got {type(entry).__name__}"
            )
        merged = {**parsed.defaults, **entry}
        try:
            specs.append(ModelSpec.model_validate(merged))
        except ValidationError as exc:
            model_id = entry.get("id", f"<models[{index}]>")
            raise CatalogError(f"{path}: model {model_id!r} failed validation:\n{exc}") from exc

    duplicates = _duplicates([spec.id for spec in specs])
    if duplicates:
        raise CatalogError(f"{path}: duplicate model id(s): {', '.join(sorted(duplicates))}")

    return Catalog(specs)


def _duplicates(ids: list[str]) -> set[str]:
    seen: set[str] = set()
    dupes: set[str] = set()
    for value in ids:
        if value in seen:
            dupes.add(value)
        seen.add(value)
    return dupes
