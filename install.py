# /// script
# requires-python = ">=3.10"
# dependencies = ["ruamel.yaml>=0.18"]
# ///
"""Add the pensero-litellm callback to a LiteLLM proxy, or start a new proxy that has it.

    curl -fsSL https://app.pensero.ai/otel/litellm/install | sh

It finds the proxy on this machine (Docker Compose, Docker, or pip) and changes 3 things:
the image (or the Python environment) gets the package, `config.yaml` gets the callback,
and the proxy gets the OTel endpoint and headers. It saves a `.bak` copy of each file it edits
and asks before it restarts anything.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

from ruamel.yaml import YAML

CALLBACK = "pensero_litellm.handler"
PACKAGE = os.environ.get("PENSERO_LITELLM_PACKAGE", "pensero-litellm")
PENSERO_ENDPOINT = "https://app.pensero.ai/otel"
# A fixed version and digest: the moving tags (main-stable, latest) served the compromised v1.107.0 on 2026-10-21.
NEW_PROXY_IMAGE = (
    "ghcr.io/berriai/litellm:v1.106.2@sha256:7294b90f868c5c38c99b83021c7ba46f9b439e95f8e32964c925c7d2563a03f7"
)
# The LiteLLM image has no pip, so the build copies in uv and installs into the image's own Python.
DOCKERFILE = """FROM {base}
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv
RUN uv pip install --no-cache --python "$(command -v python)" "{package}"
"""

yaml = YAML()
yaml.preserve_quotes = True
yaml.indent(mapping=2, sequence=4, offset=2)


def say(message: str = "") -> None:
    print(message, flush=True)


def ask(prompt: str, default: str = "", secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    read = getpass.getpass if secret else input
    try:
        return read(f"{prompt}{suffix}: ").strip() or default
    except EOFError:
        if default:
            return default
        sys.exit(f"No answer for '{prompt}'. Run this in a terminal, or give the answer as an option (see --help).")


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    return input(f"{prompt} [y/N]: ").strip().lower() in ("y", "yes")


def run(*args: str, cwd: Path | None = None, capture: bool = False, stdin: str | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, input=stdin, text=True, capture_output=capture, check=True)
    return result.stdout if capture else ""


def backup(path: Path) -> None:
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))


def load_yaml(path: Path):
    return yaml.load(path.read_text()) if path.exists() and path.read_text().strip() else {}


def write_yaml(path: Path, data) -> None:
    with path.open("w") as f:
        yaml.dump(data, f)


def add_callback(data) -> bool:
    """Add the callback to `litellm_settings.callbacks`, and keep the callbacks that are there."""
    if data is None:
        data = {}
    settings = data.get("litellm_settings")
    if settings is None:
        data["litellm_settings"] = settings = {}
    callbacks = settings.get("callbacks")
    if callbacks is None:
        settings["callbacks"] = CALLBACK
        return True
    if isinstance(callbacks, str):
        if callbacks == CALLBACK:
            return False
        settings["callbacks"] = [callbacks, CALLBACK]
        return True
    if CALLBACK in callbacks:
        return False
    callbacks.append(CALLBACK)
    return True


def add_callback_to_file(path: Path) -> None:
    data = load_yaml(path) or {}
    if add_callback(data):
        backup(path)
        write_yaml(path, data)
        say(f"Added `callbacks: {CALLBACK}` to {path}")
    else:
        say(f"{path} already has the callback.")


def set_env_file(path: Path, values: dict[str, str]) -> None:
    """Set the keys in a `.env` file, and keep the other lines."""
    lines = path.read_text().splitlines() if path.exists() else []
    kept = [line for line in lines if line.split("=", 1)[0].strip() not in values]
    backup(path)
    path.write_text("\n".join(kept + [f"{k}={json.dumps(v)}" for k, v in values.items()]) + "\n")
    path.chmod(0o600)


def otel_settings(args) -> dict[str, str]:
    endpoint = args.endpoint or ask("OTel endpoint", PENSERO_ENDPOINT)
    if endpoint.rstrip("/") == PENSERO_ENDPOINT:
        token = args.token or ask("Pensero OTel token (Settings > AI tools)", secret=True)
        # Pensero reads OTLP/JSON.
        return {**otel_env(endpoint, f"Authorization=Bearer {token}"), "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json"}
    headers = args.headers if args.headers is not None else ask("OTel headers, as key=value,key2=value2 (or empty)")
    return otel_env(endpoint, headers)


def otel_env(endpoint: str, headers: str) -> dict[str, str]:
    return {"OTEL_EXPORTER_OTLP_ENDPOINT": endpoint, "OTEL_EXPORTER_OTLP_HEADERS": headers}


def build_image(base: str) -> str:
    # A second run builds again from the image of the first run, so it keeps the same tag.
    name = base.rsplit("/", 1)[-1].replace(":", "-").replace("@", "-")
    tag = base if base.startswith("pensero-litellm:") else f"pensero-litellm:{name}"
    say(f"Building {tag} from {base} with {PACKAGE} ...")
    run("docker", "build", "-q", "-t", tag, "-", stdin=DOCKERFILE.format(base=base, package=PACKAGE))
    return tag


def litellm_container() -> dict | None:
    """The running container of a LiteLLM image, if there is exactly one."""
    if not shutil.which("docker"):
        return None
    try:
        ids = run("docker", "ps", "-q", capture=True).split()
        found = json.loads(run("docker", "inspect", *ids, capture=True)) if ids else []
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None
    found = [c for c in found if "litellm" in c["Config"]["Image"]]
    if len(found) > 1:
        names = ", ".join(c["Name"].lstrip("/") for c in found)
        sys.exit(f"Found more than one LiteLLM container ({names}). Stop the others or use the README steps.")
    return found[0] if found else None


def container_config(container: dict) -> Path | None:
    """The host path of the `--config` file that the container reads."""
    args = (container["Config"].get("Entrypoint") or []) + (container["Config"].get("Cmd") or [])
    inside = next((args[i + 1] for i, a in enumerate(args[:-1]) if a == "--config"), None)
    if inside is None:
        return None
    for mount in container.get("Mounts", []):
        dest = mount["Destination"].rstrip("/")
        if inside == dest or inside.startswith(dest + "/"):
            return Path(mount["Source"] + inside[len(dest) :])
    return None


def install_compose(container: dict, args) -> None:
    labels = container["Config"].get("Labels") or {}
    workdir = Path(labels["com.docker.compose.project.working_dir"])
    files = [Path(f) for f in labels["com.docker.compose.project.config_files"].split(",")]
    service_name = labels["com.docker.compose.service"]
    compose_file = next((f for f in files if service_name in (load_yaml(f).get("services") or {})), files[0])
    compose = load_yaml(compose_file)
    service = compose["services"][service_name]
    if "build" in service:
        sys.exit(
            f"Service `{service_name}` builds its own image. Add this line to its Dockerfile and run this again:\n"
            f"  RUN uv pip install --system {PACKAGE}   (or: pip install {PACKAGE})"
        )
    config = container_config(container) or Path(ask("Path of the LiteLLM config.yaml on this machine"))
    settings = otel_settings(args)
    image = build_image(container["Config"]["Image"])

    service["image"] = image
    env = service.get("environment")
    if env is None:
        service["environment"] = env = {}
    for key in settings:
        if isinstance(env, list):
            env[:] = [e for e in env if e.split("=", 1)[0] != key] + [f"{key}=${{{key}}}"]
        else:
            env[key] = f"${{{key}}}"
    say(f"\nChanges in {workdir}:")
    say(f"  {compose_file.name}: service `{service_name}` uses image {image} and reads {', '.join(settings)}")
    say("  .env: the OTel endpoint and headers")
    say(f"  {config}: callbacks: {CALLBACK}")
    if not confirm(f"Write these changes and restart `{service_name}`?", args.yes):
        sys.exit("Nothing changed.")
    backup(compose_file)
    write_yaml(compose_file, compose)
    set_env_file(workdir / ".env", settings)
    add_callback_to_file(config)
    compose_args = [a for f in files for a in ("-f", str(f))]
    run("docker", "compose", *compose_args, "up", "-d", service_name, cwd=workdir)
    say(f"\nDone. `{service_name}` restarted with the callback. The first data arrives after the next request.")


def install_docker(container: dict, args) -> None:
    settings = otel_settings(args)
    image = build_image(container["Config"]["Image"])
    config = container_config(container)
    if config and confirm(f"Add the callback to {config}?", args.yes):
        add_callback_to_file(config)
    name = container["Name"].lstrip("/")
    env = " ".join(f"-e {k}={json.dumps(v)}" for k, v in settings.items())
    say(f"\n`{name}` was started with `docker run`, so this script cannot restart it with the same options.")
    say(f"Start it again with your usual command, with these changes:\n  image: {image}\n  {env}")
    if not config:
        say(f"  config.yaml: litellm_settings: callbacks: {CALLBACK}")


def install_pip(litellm: str, args) -> None:
    python = Path(litellm).read_text(errors="ignore").splitlines()[0].removeprefix("#!").strip()
    if not python or not Path(python.split()[0]).exists():
        python = sys.executable
    config = Path(args.config or ask("Path of the LiteLLM config.yaml", "config.yaml"))
    settings = otel_settings(args)
    if not confirm(f"Install {PACKAGE} with {python} and add the callback to {config}?", args.yes):
        sys.exit("Nothing changed.")
    try:
        run(*python.split(), "-m", "pip", "--version", capture=True)
        run(*python.split(), "-m", "pip", "install", "-q", PACKAGE)
    except subprocess.CalledProcessError:
        run("uv", "pip", "install", "-q", "--python", python.split()[0], PACKAGE)
    add_callback_to_file(config)
    say("\nDone. Set these variables where LiteLLM starts, then restart it:")
    for key, value in settings.items():
        say(f"  export {key}={json.dumps(value)}")


def install_new(args) -> None:
    if not shutil.which("docker"):
        sys.exit("No LiteLLM was found and Docker is not installed. Install Docker, or see the README for pip.")
    folder = Path(args.dir or ask("No LiteLLM found. Folder for a new LiteLLM proxy", "litellm-proxy")).resolve()
    provider = (args.provider or ask("Model provider: anthropic or openai", "anthropic")).lower()
    key_name = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
    api_key = os.environ.get(key_name) or ask(key_name, secret=True)
    settings = otel_settings(args)
    if not confirm(f"Create the proxy in {folder} and start it on port {args.port}?", args.yes):
        sys.exit("Nothing changed.")
    folder.mkdir(parents=True, exist_ok=True)
    image = build_image(NEW_PROXY_IMAGE)
    config = {
        "model_list": [
            {"model_name": "*", "litellm_params": {"model": f"{provider}/*", "api_key": f"os.environ/{key_name}"}}
        ],
        "litellm_settings": {"callbacks": CALLBACK},
    }
    if not (folder / "config.yaml").exists():
        write_yaml(folder / "config.yaml", config)
    else:
        add_callback_to_file(folder / "config.yaml")
    compose = {
        "services": {
            "litellm": {
                "image": image,
                "command": ["--config", "/app/config.yaml", "--port", "4000"],
                "ports": [f"{args.port}:4000"],
                "volumes": ["./config.yaml:/app/config.yaml:ro"],
                "env_file": [".env"],
                "restart": "unless-stopped",
            }
        }
    }
    write_yaml(folder / "compose.yaml", compose)
    master_key = "sk-" + secrets.token_urlsafe(24)
    set_env_file(folder / ".env", {"LITELLM_MASTER_KEY": master_key, key_name: api_key, **settings})
    run("docker", "compose", "up", "-d", cwd=folder)
    say(f"\nDone. LiteLLM runs on http://localhost:{args.port}. The master key is in {folder / '.env'}.")
    if provider == "anthropic":
        say(f"Claude Code: export ANTHROPIC_BASE_URL=http://localhost:{args.port} ANTHROPIC_AUTH_TOKEN=<a LiteLLM key>")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Add the pensero-litellm callback to a LiteLLM proxy.")
    parser.add_argument("--endpoint", help=f"OTel endpoint (default {PENSERO_ENDPOINT})")
    parser.add_argument("--token", help="Pensero OTel token")
    parser.add_argument("--headers", help="OTel headers for other backends, as key=value,key2=value2")
    parser.add_argument("--config", help="config.yaml of a LiteLLM installed with pip")
    parser.add_argument("--dir", help="folder for a new proxy")
    parser.add_argument("--port", default="4000", help="port for a new proxy")
    parser.add_argument("--provider", choices=["anthropic", "openai"], help="model provider for a new proxy")
    parser.add_argument("--yes", action="store_true", help="do not ask before changes")
    args = parser.parse_args(argv)

    container = litellm_container()
    labels = (container or {}).get("Config", {}).get("Labels") or {}
    if container and "com.docker.compose.service" in labels:
        say(f"Found LiteLLM in Docker Compose: service `{labels['com.docker.compose.service']}`.")
        install_compose(container, args)
    elif container:
        say(f"Found LiteLLM in Docker: container `{container['Name'].lstrip('/')}`.")
        install_docker(container, args)
    elif litellm := shutil.which("litellm"):
        say(f"Found LiteLLM installed with pip: {litellm}")
        install_pip(litellm, args)
    else:
        install_new(args)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.exit(f"`{' '.join(error.cmd)}` failed with exit code {error.returncode}.")
    except KeyboardInterrupt:
        sys.exit(1)
