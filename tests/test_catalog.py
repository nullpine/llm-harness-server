"""models.yaml validation. An invalid file must fail loudly and name the field.

Acceptance L12: "An invalid models.yaml fails startup with a pydantic error naming
the bad field." Half of these tests exist to assert the error message is useful,
not merely that something was raised.
"""

from pathlib import Path

import pytest

from harness_control.catalog import Catalog, CatalogError, ModelSpec, load_catalog

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_CATALOG = REPO_ROOT / "deploy" / "config" / "models.yaml"

VALID = """
defaults:
  backend: ollama

models:
  - id: glm-4.7-flash
    display_name: GLM 4.7 Flash
    model_ref: glm-4.7-flash:q4_K_M
    params: 30B-A3B (MoE)
    quantization: q4_K_M
    context_length: 32768
    estimated_load_seconds: 25
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_the_shipped_catalog() -> None:
    catalog = load_catalog(SHIPPED_CATALOG)
    assert catalog.ids == ["glm-4.7-flash", "qwen3.8-27b"]

    glm = catalog.get("glm-4.7-flash")
    assert glm is not None
    assert glm.backend == "ollama"
    assert glm.model_ref == "glm-4.7-flash:q4_K_M"

    qwen = catalog.get("qwen3.8-27b")
    assert qwen is not None
    assert qwen.model_ref == "qwen3.8:27b-q4_K_M"


def test_every_shipped_model_advertises_a_load_time() -> None:
    """The desktop app shows this in its switch dialog, so zero is a broken promise."""
    for spec in load_catalog(SHIPPED_CATALOG).specs:
        assert spec.estimated_load_seconds > 0, spec.id


def test_defaults_are_applied_to_entries_that_omit_the_field(tmp_path: Path) -> None:
    catalog = load_catalog(write(tmp_path, VALID))
    spec = catalog.get("glm-4.7-flash")
    assert spec is not None
    assert spec.backend == "ollama"


def test_an_explicit_field_beats_the_default(tmp_path: Path) -> None:
    text = VALID.replace("    model_ref:", "    backend: remote_openai\n    model_ref:")
    catalog = load_catalog(write(tmp_path, text))
    spec = catalog.get("glm-4.7-flash")
    assert spec is not None
    assert spec.backend == "remote_openai"


def test_optional_fields_default_sensibly(tmp_path: Path) -> None:
    text = """
models:
  - id: m
    display_name: M
    backend: ollama
    model_ref: m:latest
    estimated_load_seconds: 0
"""
    spec = load_catalog(write(tmp_path, text)).specs[0]
    assert spec.args == []
    assert spec.params is None
    assert spec.context_length is None


def test_args_round_trip(tmp_path: Path) -> None:
    text = VALID + "    args:\n      - --tool-call-parser=glm47\n"
    spec = load_catalog(write(tmp_path, text)).specs[0]
    assert spec.args == ["--tool-call-parser=glm47"]


def test_missing_file_is_a_catalog_error(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="cannot read catalog"):
        load_catalog(tmp_path / "nope.yaml")


def test_malformed_yaml_is_a_catalog_error(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="not valid YAML"):
        load_catalog(write(tmp_path, "models: [\n  - id: broken\n"))


def test_empty_file_is_a_catalog_error(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="empty"):
        load_catalog(write(tmp_path, ""))


def test_no_models_key_is_a_catalog_error(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="models"):
        load_catalog(write(tmp_path, "defaults:\n  backend: ollama\n"))


def test_empty_model_list_is_a_catalog_error(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="models"):
        load_catalog(write(tmp_path, "models: []\n"))


@pytest.mark.parametrize("field", ["id", "display_name", "model_ref", "estimated_load_seconds"])
def test_a_missing_required_field_names_that_field(tmp_path: Path, field: str) -> None:
    lines = []
    for line in VALID.splitlines():
        body = line.strip().removeprefix("- ")
        if not body.startswith(f"{field}:"):
            lines.append(line)
        elif line.strip().startswith("- "):
            # Dropping the first key would take the list-item marker with it.
            lines.append(line[: line.index("-") + 1])
    with pytest.raises(CatalogError) as excinfo:
        load_catalog(write(tmp_path, "\n".join(lines)))
    assert field in str(excinfo.value), "the error must name the bad field (L12)"


def test_a_wrongly_typed_field_names_that_field(tmp_path: Path) -> None:
    text = VALID.replace("estimated_load_seconds: 25", "estimated_load_seconds: soon")
    with pytest.raises(CatalogError) as excinfo:
        load_catalog(write(tmp_path, text))
    assert "estimated_load_seconds" in str(excinfo.value)


def test_a_negative_load_estimate_is_rejected(tmp_path: Path) -> None:
    text = VALID.replace("estimated_load_seconds: 25", "estimated_load_seconds: -1")
    with pytest.raises(CatalogError, match="estimated_load_seconds"):
        load_catalog(write(tmp_path, text))


def test_an_unknown_field_is_rejected_rather_than_ignored(tmp_path: Path) -> None:
    """A typo'd key must not silently do nothing — that is how hf_repo lingered."""
    text = VALID + "    hf_repo: zai-org/GLM-4.7-Flash\n"
    with pytest.raises(CatalogError, match="hf_repo"):
        load_catalog(write(tmp_path, text))


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    text = VALID + VALID.split("models:", 1)[1]
    with pytest.raises(CatalogError, match="duplicate"):
        load_catalog(write(tmp_path, text))


def test_catalog_lookups() -> None:
    spec = ModelSpec(
        id="m",
        display_name="M",
        backend="ollama",
        model_ref="m:latest",
        estimated_load_seconds=1,
    )
    catalog = Catalog([spec])
    assert len(catalog) == 1
    assert "m" in catalog
    assert "other" not in catalog
    assert catalog.get("other") is None
    assert list(catalog) == [spec]
