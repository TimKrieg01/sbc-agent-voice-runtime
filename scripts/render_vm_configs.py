from __future__ import annotations

import argparse
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = ROOT / "deploy" / "templates"
DEFAULT_ENV_FILE = ROOT / "deploy" / "vm.env"
DEFAULT_OUTPUT_ROOT = ROOT / "deploy" / "rendered"

PLACEHOLDER_RE = re.compile(r"{{([A-Z0-9_]+)}}")


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Invalid line in {path}: {raw_line!r}")
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def render_template(template_text: str, values: dict[str, str], template_path: Path) -> str:
    missing: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            missing.add(key)
            return match.group(0)
        return values[key]

    rendered = PLACEHOLDER_RE.sub(replace, template_text)
    if missing:
        missing_list = ", ".join(sorted(missing))
        raise ValueError(f"Missing values for {template_path}: {missing_list}")
    return rendered


def main() -> int:
    parser = argparse.ArgumentParser(description="Render VM-specific deploy config templates.")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE), help="Path to vm.env file")
    parser.add_argument(
        "--output-root",
        default=str(DEFAULT_OUTPUT_ROOT),
        help="Directory where rendered files should be written",
    )
    args = parser.parse_args()

    env_file = Path(args.env_file).resolve()
    output_root = Path(args.output_root).resolve()

    if not env_file.exists():
        raise FileNotFoundError(
            f"Env file not found: {env_file}. Copy deploy/vm.env.example to deploy/vm.env first."
        )

    values = parse_env_file(env_file)

    rendered_files: list[Path] = []
    for template_path in TEMPLATE_ROOT.rglob("*.template"):
        relative_path = template_path.relative_to(TEMPLATE_ROOT)
        output_relative = relative_path.with_suffix("")
        output_path = output_root / output_relative
        output_path.parent.mkdir(parents=True, exist_ok=True)

        rendered = render_template(template_path.read_text(encoding="utf-8"), values, template_path)
        output_path.write_text(rendered, encoding="utf-8", newline="\n")
        rendered_files.append(output_path)

    print("Rendered files:")
    for path in rendered_files:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
