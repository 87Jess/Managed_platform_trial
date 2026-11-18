import ast
import gzip
import hashlib
import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_FILES = 1000
EXCLUDED = {".git", ".adk", ".venv", "venv", "node_modules", ".terraform", "__pycache__",
            ".pytest_cache", ".ruff_cache", ".ssh", ".aws", ".azure", ".config"}


class SourceError(ValueError):
    pass


def safe_path(name: str) -> str:
    if (not name or name.startswith("/") or "\\" in name or ":" in name or "\x00" in name
            or any(part in {"", ".", ".."} for part in name.split("/"))):
        raise SourceError("Source contains an unsafe path")
    return name


def excluded(name: str) -> bool:
    path = PurePosixPath(name)
    return (any(part in EXCLUDED for part in path.parts)
            or path.name.startswith(".env")
            or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".tfstate", ".pyc"}
            or path.name.lower() in {"credentials.json", "service-account.json", "adc.json",
                                     "application_default_credentials.json"})


def checked_files(files: dict[str, bytes]) -> dict[str, bytes]:
    if not files:
        raise SourceError("Source contains no deployable files")
    if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_SOURCE_BYTES:
        raise SourceError("Source exceeds 1,000 files or the 8 MiB limit")
    return files


def local_files(location: str, allowed_root: Path) -> dict[str, bytes]:
    if location.startswith(("\\\\", "//")):
        raise SourceError("Network shares are not accepted")
    root = allowed_root.resolve()
    candidate = Path(location).expanduser()
    candidate = candidate if candidate.is_absolute() else root / candidate
    source = candidate.resolve()
    if not source.is_relative_to(root) or not source.is_dir():
        raise SourceError("Folder must exist inside the configured source root")
    files = {}
    size = 0
    for directory, directories, filenames in os.walk(source, followlinks=False):
        directories[:] = sorted(name for name in directories if name not in EXCLUDED)
        for name in directories + filenames:
            path = Path(directory) / name
            if path.is_symlink() or path.is_junction():
                raise SourceError("Symlinks and junctions are not accepted")
        for name in sorted(filenames):
            path = Path(directory) / name
            relative = safe_path(path.relative_to(source).as_posix())
            if excluded(relative):
                continue
            if not path.resolve().is_relative_to(root):
                raise SourceError("File escapes the source root")
            if not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
                raise SourceError("Unsupported or oversized source file")
            with path.open("rb") as stream:
                content = stream.read(MAX_SOURCE_BYTES + 1)
            size += len(content)
            if size > MAX_SOURCE_BYTES or len(files) >= MAX_FILES:
                raise SourceError("Source exceeds 1,000 files or the 8 MiB limit")
            files[relative] = content
    return checked_files(files)


def zip_files(content: bytes) -> dict[str, bytes]:
    if len(content) > MAX_SOURCE_BYTES:
        raise SourceError("ZIP exceeds the 8 MiB upload limit")
    files = {}
    size = 0
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if len(archive.infolist()) > MAX_FILES * 2:
                raise SourceError("ZIP contains too many entries")
            for entry in archive.infolist():
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise SourceError("ZIP symlinks are not accepted")
                normalized = entry.orig_filename.replace("\\", "/")
                is_directory = normalized.endswith("/") or stat.S_ISDIR(mode)
                while normalized.startswith("./"):
                    normalized = normalized[2:]
                if normalized in {"", "."} and is_directory:
                    continue
                try:
                    name = safe_path(normalized.rstrip("/"))
                except SourceError as error:
                    raise SourceError(
                        f"ZIP entry has an unsafe path: {entry.orig_filename[:160]!r}") from error
                if is_directory or excluded(name):
                    continue
                size += entry.file_size
                if size > MAX_SOURCE_BYTES or len(files) >= MAX_FILES:
                    raise SourceError("Expanded ZIP exceeds the source limit")
                if name in files:
                    raise SourceError("ZIP contains duplicate paths")
                files[name] = archive.read(entry)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
        raise SourceError("ZIP is invalid, encrypted, or uses unsupported compression") from error
    if files and all("/" in name for name in files):
        prefixes = {name.split("/", 1)[0] for name in files}
        if len(prefixes) == 1:
            files = {name.split("/", 1)[1]: data for name, data in files.items()}
    return checked_files(files)


@dataclass(frozen=True)
class Bundle:
    archive: bytes
    digest: str
    files: tuple[dict, ...]
    total_bytes: int
    requirements: str = "requirements.txt"
    dependency_format: str = "requirements"
    entrypoint_module: str = ""
    entrypoint_object: str = "root_agent"
    framework: str = "custom"
    class_methods: tuple = ()
    methods_inferred: bool = False


# Agent Engine method names and how they are invoked (:query vs :streamQuery, sync vs async).
API_MODES = {"query": "", "stream_query": "stream", "async_query": "async", "async_stream_query": "async_stream"}
DEFAULT_METHODS = ({"name": "query", "api_mode": "", "parameters": {
    "type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}},)


def _annotation_schema(node) -> dict:
    """JSON schema for a parameter annotation, from its source text only."""
    if node is None:
        return {}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):  # X | None
        parts = [side for side in (node.left, node.right)
                 if not (isinstance(side, ast.Constant) and side.value is None)]
        return _annotation_schema(parts[0]) if len(parts) == 1 else {}
    item = None
    if isinstance(node, ast.Subscript):
        outer = node.value.id if isinstance(node.value, ast.Name) else getattr(node.value, "attr", "")
        if outer == "Optional":
            return _annotation_schema(node.slice)
        item = node.slice.elts[0] if isinstance(node.slice, ast.Tuple) and node.slice.elts else node.slice
        node = node.value
    name = node.id if isinstance(node, ast.Name) else getattr(node, "attr", "")
    kind = {"str": "string", "int": "integer", "float": "number", "bool": "boolean", "dict": "object",
            "Dict": "object", "list": "array", "List": "array", "Mapping": "object",
            "Sequence": "array"}.get(name)
    if kind == "array":  # Agent Engine (OpenAPI) schemas need items on every array
        return {"type": "array", "items": _annotation_schema(item) or {"type": "string"}}
    return {"type": kind} if kind else {}


def _method_schema(function) -> dict:
    args = function.args
    positional = [*args.posonlyargs, *args.args][1:]  # drop self
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    properties, required = {}, []
    for arg, default in [*zip(positional, defaults, strict=True),
                         *zip(args.kwonlyargs, args.kw_defaults, strict=True)]:
        properties[arg.arg] = _annotation_schema(arg.annotation)
        if default is None:
            required.append(arg.arg)
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _find_class(files: dict[str, bytes], module: str, name: str, depth: int = 0):
    """Locate a class definition by name in a module, following one level of imports."""
    path = module.replace(".", "/")
    source = files.get(path + ".py") or files.get(path + "/__init__.py")
    if source is None or depth > 2:
        return None
    try:
        tree = ast.parse(source.decode("utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    package = module.rsplit(".", 1)[0] if "." in module and not path.endswith("__init__") else ""
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and any((alias.asname or alias.name) == name for alias in node.names):
            original = next(alias.name for alias in node.names if (alias.asname or alias.name) == name)
            target = node.module or ""
            if node.level:
                target = ".".join(part for part in (package, target) if part)
            return _find_class(files, target, original, depth + 1)
    return None


def infer_class_methods(files: dict[str, bytes], module: str, object_name: str) -> tuple[tuple, bool]:
    """Read the entrypoint object's query methods from source, without importing it.

    Returns (class_methods, inferred). Falls back to query(message) when the object is not
    a plain `name = SomeClass(...)` assignment or the class can't be found.
    """
    path = module.replace(".", "/")
    source = files.get(path + ".py") or files.get(path + "/__init__.py")
    try:
        tree = ast.parse(source.decode("utf-8")) if source else None
    except (SyntaxError, UnicodeDecodeError):
        tree = None
    class_name = None
    for node in tree.body if tree else []:
        if (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == object_name
                                                 for target in node.targets)
                and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)):
            class_name = node.value.func.id
    found = _find_class(files, module, class_name) if class_name else None
    if found is None:
        return DEFAULT_METHODS, False
    methods = []
    for node in found.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in API_MODES:
            methods.append({"name": node.name, "api_mode": API_MODES[node.name], "parameters": _method_schema(node)})
    return (tuple(methods), True) if methods else (DEFAULT_METHODS, False)


def export_uv_requirements(files: dict[str, bytes]) -> bytes:
    if "pyproject.toml" not in files or "uv.lock" not in files:
        raise SourceError("uv projects require both pyproject.toml and uv.lock in the selected source. Run uv lock before uploading.")
    try:
        project = tomllib.loads(files["pyproject.toml"].decode("utf-8"))
        lock = tomllib.loads(files["uv.lock"].decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise SourceError("pyproject.toml or uv.lock is not valid UTF-8 TOML") from error
    tool = project.get("tool", {})
    uv_config = tool.get("uv", {}) if isinstance(tool, dict) else None
    packages = lock.get("package", [])
    if (not isinstance(project.get("project"), dict) or not isinstance(uv_config, dict)
            or not isinstance(packages, list) or not all(isinstance(item, dict) for item in packages)):
        raise SourceError("uv deployment requires standard [project] metadata and a valid uv.lock package list")
    if project["project"].get("dynamic"):
        raise SourceError("Dynamic project metadata is not supported; declare deployment dependencies statically")
    if uv_config.get("workspace") is not None:
        raise SourceError("uv workspaces are not supported; upload a standalone agent project")
    for package in packages:
        source = package.get("source", {})
        if source not in ({"registry": "https://pypi.org/simple"},
                          {"editable": "."}, {"virtual": "."}):
            raise SourceError("uv deployment currently supports public PyPI dependencies only, not local, Git, or private-index packages")
    executable = shutil.which("uv")
    if executable is None:
        raise SourceError("uv must be installed on the console server to export uv.lock")
    try:
        with tempfile.TemporaryDirectory(prefix="agent-dependencies-") as temporary:
            root = Path(temporary)
            for name in ("pyproject.toml", "uv.lock"):
                (root / name).write_bytes(files[name])
            environment = {key: value for key, value in os.environ.items() if key.upper() in {
                "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE",
                "LOCALAPPDATA", "APPDATA",
            }}
            environment.update({"UV_CACHE_DIR": str(root / "cache"), "UV_PYTHON": sys.executable,
                                "UV_PYTHON_DOWNLOADS": "never", "UV_NO_PROGRESS": "1"})
            result = subprocess.run(
                [executable, "export", "--locked", "--offline", "--no-config", "--no-dev",
                 "--no-build", "--no-sources", "--no-default-groups", "--no-emit-project", "--no-header", "--no-annotate",
                 "--format", "requirements-txt", "--project", str(root)],
                cwd=root, env=environment, capture_output=True, text=True, encoding="utf-8",
                timeout=30, check=False,
            )
            if result.returncode:
                raise SourceError("uv could not export locked dependencies offline. Run uv lock with your installed uv version, then upload the updated project and lockfile.")
            return result.stdout.encode("utf-8")
    except subprocess.TimeoutExpired as error:
        raise SourceError("uv dependency export timed out") from error
    except OSError as error:
        raise SourceError("uv could not run on this machine; check installation and application-control policy") from error


def bundle_source(files: dict[str, bytes], module: str, requirements: str = "auto",
                  subdirectory: str = "", framework: str = "custom",
                  object_name: str = "root_agent") -> Bundle:
    if subdirectory:
        prefix = safe_path(subdirectory) + "/"
        files = {name[len(prefix):]: data for name, data in files.items() if name.startswith(prefix)}
    checked_files(files)
    module_path = module.replace(".", "/")
    if module_path + ".py" not in files and module_path + "/__init__.py" not in files:
        raise SourceError("Entrypoint module was not found in the selected source")
    dependency_format = "requirements"
    if requirements == "auto":
        requirements = "requirements.txt" if "requirements.txt" in files else "pyproject.toml"
    if requirements in {"pyproject.toml", "uv.lock"}:
        exported = export_uv_requirements(files)
        requirements = ".agent-platform-requirements.txt"
        if requirements in files:
            raise SourceError("Source contains the reserved .agent-platform-requirements.txt file")
        files = {**files, requirements: exported}
        dependency_format = "uv"
        checked_files(files)
    elif safe_path(requirements) not in files:
        raise SourceError("Dependency file was not found. Select a requirements file or include pyproject.toml and uv.lock for auto detection.")
    if framework not in {"custom", "google-adk"}:
        raise SourceError("Unsupported agent framework")
    class_methods, methods_inferred = (infer_class_methods(files, module, object_name)
                                       if framework == "custom" else ((), False))
    if framework == "google-adk":
        wrapper_name = "_agent_platform_adk.py"
        if wrapper_name in files:
            raise SourceError("Source contains the reserved _agent_platform_adk.py file")
        wrapper = (
            "from importlib import import_module\n"
            "from vertexai.agent_engines import AdkApp\n\n"
            f"source_agent = getattr(import_module({module!r}), {object_name!r})\n"
            "root_agent = source_agent if isinstance(source_agent, AdkApp) else AdkApp(agent=source_agent)\n"
        )
        files = {**files, wrapper_name: wrapper.encode("utf-8")}
        module, object_name = "_agent_platform_adk", "root_agent"
        checked_files(files)
    buffer = io.BytesIO()
    with (gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed,
          tarfile.open(fileobj=compressed, mode="w") as archive):
        for name, data in sorted(files.items()):
            entry = tarfile.TarInfo(safe_path(name))
            entry.size = len(data)
            entry.mode = 0o644
            archive.addfile(entry, io.BytesIO(data))
    payload = buffer.getvalue()
    if len(payload) > MAX_SOURCE_BYTES:
        raise SourceError("Packaged source exceeds the 8 MiB limit")
    return Bundle(payload, hashlib.sha256(payload).hexdigest(),
                  tuple({"path": name, "bytes": len(data)} for name, data in sorted(files.items())),
                  sum(map(len, files.values())), requirements, dependency_format,
                  module, object_name, framework, class_methods, methods_inferred)