import pytest
from pydantic import ValidationError

from doc_pipeline import Config
from tests.support import PROJECT_ROOT


def test_root_template_runs_without_credentials_or_embedding():
    config = Config.from_toml(PROJECT_ROOT / "config.example.toml")
    assert config == Config()
    assert config.embedding is None


def test_omitted_sections_use_documented_defaults(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("")

    config = Config.from_toml(path)

    assert config.request_timeout_seconds == 60
    assert config.chunking.target_input_bytes == 2400
    assert config.chunking.max_input_bytes == 6000
    assert config.mineru.api_base_url == "https://mineru.net"
    assert config.mineru.timeout_seconds == 1800
    assert config.mineru.api_key.get_secret_value() == ""
    assert config.mineru.batch_size == 50
    assert config.local_workers == 4
    assert config.mineru.parse_options() == {
        "model_version": "vlm", "enable_formula": True, "enable_table": True,
        "language": "ch", "is_ocr": False,
    }
    assert config.embedding is None


def test_partial_sections_use_defaults_for_omitted_fields(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("""[chunking]
target_input_bytes = 1000
[mineru]
timeout_seconds = 20
""")

    config = Config.from_toml(path)

    assert config.chunking.target_input_bytes == 1000
    assert config.chunking.max_input_bytes == 6000
    assert config.mineru.api_base_url == "https://mineru.net"
    assert config.mineru.timeout_seconds == 20


@pytest.mark.parametrize("values", [
    {"unknown": True},
    {"chunking": {"unknown": True}},
    {"mineru": {"unknown": True}},
    {"embedding": {"api_base_url": "https://model.test/v1", "model": "m", "dimensions": 2,
                   "unknown": True}},
])
def test_unknown_configuration_fields_are_rejected(values):
    with pytest.raises(ValidationError):
        Config.model_validate(values)


@pytest.mark.parametrize(("key", "value"), [
    ("target_input_bytes", 2400),
    ("max_input_bytes", 6000),
    ("mineru_endpoint", "https://mineru.net"),
    ("mineru_api_key", "private-key"),
    ("mineru_timeout_seconds", 1800),
])
def test_legacy_flat_configuration_keys_are_rejected(key, value):
    with pytest.raises(ValidationError) as caught:
        Config.model_validate({key: value})
    assert caught.value.errors(include_input=False)[0]["loc"] == (key,)
    assert "private-key" not in str(caught.value)


@pytest.mark.parametrize("section", ["mineru", "embedding"])
@pytest.mark.parametrize("api_base_url", ["http://remote.test", "https://key:secret@model.test", "https://m.test?q=1"])
def test_service_api_base_url_cannot_include_credentials_or_plaintext_remote_transport(section, api_base_url):
    service = {"api_base_url": api_base_url}
    if section == "embedding":
        service["model"] = "m"
    with pytest.raises(ValidationError) as caught:
        Config.model_validate({section: service})
    assert caught.value.errors(include_input=False)[0]["loc"] == (section, "api_base_url")
    assert "key:secret" not in str(caught.value)


@pytest.mark.parametrize("values", [
    {"chunking": {"target_input_bytes": 63}},
    {"chunking": {"target_input_bytes": 64, "max_input_bytes": 63}},
    {"chunking": {"max_input_bytes": 2000}},
    {"chunking": {"target_input_bytes": 3000, "max_input_bytes": 2000}},
    {"request_timeout_seconds": 0},
    {"mineru": {"timeout_seconds": -1}},
    {"mineru": {"batch_size": 0}},
    {"mineru": {"batch_size": 51}},
    {"mineru": {"model_version": "MinerU-HTML"}},
    {"mineru": {"is_ocr": 1}},
    {"mineru": {"language": ""}},
    {"local_workers": 0},
    {"local_workers": 33},
])
def test_invalid_budgets_and_timeouts_are_rejected(values):
    with pytest.raises(ValidationError):
        Config(**values)


def test_file_credentials_do_not_appear_in_configuration_exports(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('''[mineru]
api_key = "private-mineru-key"
[embedding]
api_base_url = "https://embed.test/v1"
model = "another-provider-model"
api_key = "private-embedding-key"
''')
    config = Config.from_toml(path)
    assert config.mineru.api_key.get_secret_value() == "private-mineru-key"
    assert config.embedding.api_key.get_secret_value() == "private-embedding-key"
    assert config.embedding.dimensions is None
    for exported in [repr(config), str(config.model_dump()), config.model_dump_json()]:
        assert "private-mineru-key" not in exported
        assert "private-embedding-key" not in exported
    assert "api_key" not in config.model_dump()["mineru"]
    assert "api_key" not in config.model_dump()["embedding"]


@pytest.mark.parametrize("values", [
    {"mineru": {"api_key": ["private-key"]}},
    {"embedding": {"api_base_url": "https://embed.test/v1", "model": "m", "api_key": ["private-key"]}},
    {"chunking": {"target_input_bytes": 8000}, "mineru": {"api_key": "private-key"}},
])
def test_validation_error_does_not_echo_file_key(values):
    with pytest.raises(ValidationError) as caught:
        Config.model_validate(values)
    assert "private-key" not in str(caught.value)
