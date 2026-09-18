import json

import pytest
from vcfspec.schema import (DEFAULT_VERSION, SchemaIntegrityError, load_schema,
                            schema_path)


@pytest.fixture(autouse=True)
def clear_cache():
    load_schema.cache_clear()
    yield
    load_schema.cache_clear()


def test_loads_pinned_schema_and_root_is_sddcspec():
    schema = load_schema()
    assert schema["x-vcf-version"] == DEFAULT_VERSION
    assert schema["$ref"] == "#/$defs/SddcSpec"
    assert set(schema["$defs"]["SddcSpec"]["required"]) == {
        "sddcId", "dnsSpec", "networkSpecs", "vcenterSpec"}


def test_openapi_dialect_was_converted():
    text = schema_path().read_text(encoding="utf-8")
    assert '"nullable"' not in text
    assert '"exclusiveMinimum": true' not in text
    assert '"discriminator"' not in text


def test_checksum_mismatch_is_a_hard_failure(tmp_path, monkeypatch):
    dest = tmp_path / DEFAULT_VERSION
    dest.mkdir(parents=True)
    (dest / "sddc-spec.schema.json").write_text(json.dumps({"tampered": True}),
                                                encoding="utf-8")
    (dest / "sddc-spec.schema.json.sha256").write_text(
        (schema_path().parent / "sddc-spec.schema.json.sha256").read_text(),
        encoding="utf-8")
    monkeypatch.setattr("vcfspec.schema.SCHEMA_DIR", tmp_path)
    load_schema.cache_clear()
    with pytest.raises(SchemaIntegrityError):
        load_schema(DEFAULT_VERSION)


def test_unknown_version_raises():
    with pytest.raises(FileNotFoundError):
        load_schema("0.0.0")
