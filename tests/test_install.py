import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("install", ROOT / "install.py")
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)

CALLBACK = "pensero_litellm.handler"


def edit(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    install.add_callback_to_file(path)
    return path.read_text()


def test_adds_callback_to_empty_config(tmp_path):
    assert edit(tmp_path, "") == f"litellm_settings:\n  callbacks: {CALLBACK}\n"


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("litellm_settings:\n  callbacks: custom.handler\n", ["custom.handler", CALLBACK]),
        ("litellm_settings:\n  callbacks: [otel, custom.handler]\n", ["otel", "custom.handler", CALLBACK]),
        ("litellm_settings:\n  drop_params: true\n", CALLBACK),
    ],
)
def test_keeps_existing_callbacks(tmp_path, before, after):
    edit(tmp_path, before)
    assert install.load_yaml(tmp_path / "config.yaml")["litellm_settings"]["callbacks"] == after


def test_keeps_comments_and_writes_a_backup(tmp_path):
    before = "# models\nmodel_list: []  # none yet\n"
    after = edit(tmp_path, before)
    assert "# models" in after and "# none yet" in after
    assert (tmp_path / "config.yaml.bak").read_text() == before


def test_second_run_changes_nothing(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(f"litellm_settings:\n  callbacks: [{CALLBACK}]\n")
    install.add_callback_to_file(path)
    assert path.read_text() == f"litellm_settings:\n  callbacks: [{CALLBACK}]\n"
    assert not (tmp_path / "config.yaml.bak").exists()


def test_env_file_replaces_only_its_keys(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=1\nOTEL_EXPORTER_OTLP_ENDPOINT=old\n")
    install.set_env_file(path, {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://x", "OTEL_EXPORTER_OTLP_HEADERS": "a=b c"})
    assert path.read_text() == 'A=1\nOTEL_EXPORTER_OTLP_ENDPOINT="https://x"\nOTEL_EXPORTER_OTLP_HEADERS="a=b c"\n'
    assert path.stat().st_mode & 0o777 == 0o600


def test_pensero_endpoint_asks_only_for_the_token(monkeypatch):
    args = install.argparse.Namespace(endpoint=None, token=None, headers=None)
    answers = iter(["", "tok:secret"])
    monkeypatch.setattr(install, "ask", lambda *a, **k: next(answers) or a[1])
    assert install.otel_settings(args) == {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://app.pensero.ai/otel",
        "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer tok:secret",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
    }


def test_other_endpoint_asks_for_headers(monkeypatch):
    args = install.argparse.Namespace(endpoint="https://collector:4318", token=None, headers="x-key=1")
    assert install.otel_settings(args) == {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://collector:4318",
        "OTEL_EXPORTER_OTLP_HEADERS": "x-key=1",
    }


def test_finds_the_host_path_of_the_config():
    container = {
        "Config": {"Entrypoint": ["docker/prod_entrypoint.sh"], "Cmd": ["--config", "/app/conf/config.yaml"]},
        "Mounts": [{"Source": "/srv/litellm", "Destination": "/app/conf"}],
    }
    assert install.container_config(container) == Path("/srv/litellm/config.yaml")
    assert install.container_config({"Config": {"Cmd": ["--port", "4000"]}, "Mounts": []}) is None
