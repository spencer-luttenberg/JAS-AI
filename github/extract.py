import os
from pathlib import PurePosixPath

INDEXABLE_EXTENSIONS = {
    ".bat",
    ".c",
    ".cc",
    ".cfg",
    ".cmake",
    ".cpp",
    ".cs",
    ".css",
    ".csv",
    ".cxx",
    ".h",
    ".hh",
    ".hpp",
    ".htm",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".lua",
    ".md",
    ".mjs",
    ".mm",
    ".ps1",
    ".py",
    ".rb",
    ".rst",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".uplugin",
    ".uproject",
    ".usf",
    ".ush",
    ".xml",
    ".yaml",
    ".yml",
}

INDEXABLE_NAMES = {
    ".dockerignore",
    ".editorconfig",
    ".env.example",
    ".gitattributes",
    ".gitignore",
    "cmakelists.txt",
    "dockerfile",
    "license",
    "makefile",
    "readme",
}

EXCLUDED_DIRECTORIES = {
    ".git",
    ".idea",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".vs",
    ".vscode",
    "binaries",
    "build",
    "coverage",
    "deriveddatacache",
    "dist",
    "intermediate",
    "node_modules",
    "saved",
    "vendor",
    "venv",
}

EXCLUDED_NAMES = {
    "credentials.json",
    "id_dsa",
    "id_ed25519",
    "id_rsa",
    "package-lock.json",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "yarn.lock",
}

SENSITIVE_SUFFIXES = {".key", ".p12", ".pfx", ".pem"}


def is_indexable_github_path(path: str) -> bool:
    normalized = PurePosixPath(path)
    parts = [part.casefold() for part in normalized.parts]
    if not parts or any(part in EXCLUDED_DIRECTORIES for part in parts[:-1]):
        return False

    name = parts[-1]
    if name in EXCLUDED_NAMES or name.endswith(".min.js"):
        return False
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return False
    if normalized.suffix.casefold() in SENSITIVE_SUFFIXES:
        return False
    if "secret" in name and normalized.suffix.casefold() in {".json", ".txt"}:
        return False
    return name in INDEXABLE_NAMES or normalized.suffix.casefold() in INDEXABLE_EXTENSIONS


def decode_github_text(data: bytes, path: str) -> str:
    if b"\x00" in data[:8_192]:
        raise ValueError(f"Binary content detected in {path}")

    text = data.decode("utf-8-sig", errors="replace")
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    max_characters = _positive_int_environment_value(
        "GITHUB_MAX_EXTRACTED_CHARACTERS",
        2_000_000,
    )
    return text[:max_characters]


def _positive_int_environment_value(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a whole number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value
