import io
import subprocess
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from managed_agents.sources import SourceError, bundle_source, local_files, zip_files


def make_zip(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            entry = zipfile.ZipInfo()
            entry.filename = name
            archive.writestr(entry, content)
    return buffer.getvalue()


def test_local_source_is_bounded_and_excludes_secrets(tmp_path):
    (tmp_path / "agent.py").write_text("root_agent = object()")
    (tmp_path / "requirements.txt").write_text("")
    (tmp_path / ".env").write_text("SECRET=not-shipped")
    (tmp_path / "private.key").write_text("not-shipped")
    files = local_files(str(tmp_path), tmp_path)
    assert set(files) == {"agent.py", "requirements.txt"}


def test_local_folder_must_be_inside_allowed_root(tmp_path):
    child = tmp_path / "allowed"
    child.mkdir()
    with pytest.raises(SourceError):
        local_files(str(tmp_path), child)


@pytest.mark.parametrize("name", ["../agent.py", "/agent.py", "nested/../../agent.py",
                                 "C:/agent.py", "nested\\..\\agent.py", "\\\\server\\agent.py",
                                 "./../agent.py", "./C:/agent.py"])
def test_zip_rejects_traversal(name):
    with pytest.raises(SourceError):
        zip_files(make_zip({name: "malicious"}))


@pytest.mark.parametrize("prefix", ["./", ".\\", "project\\", "./project/", "././project/"])
def test_zip_accepts_portable_relative_paths(prefix):
    files = zip_files(make_zip({prefix + "agent.py": "root_agent = object()",
                               prefix + "requirements.txt": ""}))
    assert set(files) == {"agent.py", "requirements.txt"}


def test_zip_accepts_explicit_root_directory():
    files = zip_files(make_zip({"./": "", "./agent.py": "root_agent = object()",
                               "./requirements.txt": ""}))
    assert set(files) == {"agent.py", "requirements.txt"}


def test_zip_rejects_duplicates_after_path_normalization():
    with pytest.raises(SourceError, match="duplicate"):
        zip_files(make_zip({"agent.py": "one", "./agent.py": "two"}))


def test_github_style_zip_root_is_removed():
    files = zip_files(make_zip({"repo-sha/agent.py": "root_agent = object()",
                               "repo-sha/requirements.txt": ""}))
    assert set(files) == {"agent.py", "requirements.txt"}


def test_zip_symlink_rejected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        entry = zipfile.ZipInfo("agent.py")
        entry.external_attr = 0o120777 << 16
        archive.writestr(entry, "/etc/passwd")
    with pytest.raises(SourceError):
        zip_files(buffer.getvalue())


def test_source_snapshot_is_deterministic_and_does_not_execute_code():
    files = {"agent.py": b"raise RuntimeError('must never import locally')",
             "requirements.txt": b""}
    first = bundle_source(files, "agent", "requirements.txt")
    second = bundle_source(files, "agent", "requirements.txt")
    assert first.digest == second.digest
    with tarfile.open(fileobj=io.BytesIO(first.archive), mode="r:gz") as archive:
        assert archive.getnames() == ["agent.py", "requirements.txt"]


def test_entrypoint_and_requirements_must_exist():
    with pytest.raises(SourceError):
        bundle_source({"agent.py": b""}, "agent", "requirements.txt")
    with pytest.raises(SourceError):
        bundle_source({"requirements.txt": b""}, "agent", "requirements.txt")


def test_expanded_zip_limit():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("agent.py", b"x" * (8 * 1024 * 1024 + 1))
    with pytest.raises(SourceError):
        zip_files(buffer.getvalue())


def uv_source():
    return {"agent.py": b"raise RuntimeError('never import locally')",
            "pyproject.toml": b'[project]\nname = "agent-demo"\nversion = "0.1.0"\ndependencies = ["httpx==0.28.1"]\n',
            "uv.lock": b'version = 1\n[[package]]\nname = "httpx"\nversion = "0.28.1"\nsource = { registry = "https://pypi.org/simple" }\n'}


@pytest.mark.parametrize("selection", ["auto", "pyproject.toml", "uv.lock"])
def test_uv_source_exports_locked_runtime_dependencies(monkeypatch, selection):
    monkeypatch.setattr("managed_agents.sources.shutil.which", lambda name: "uv")
    def export(command, **options):
        assert {"--locked", "--offline", "--no-config", "--no-dev",
            "--no-build", "--no-sources", "--no-default-groups", "--no-emit-project"}.issubset(command)
        root = Path(options["cwd"])
        assert {path.name for path in root.iterdir()} == {"pyproject.toml", "uv.lock"}
        assert options["env"]["UV_PYTHON_DOWNLOADS"] == "never"
        assert "GOOGLE_APPLICATION_CREDENTIALS" not in options["env"]
        return SimpleNamespace(returncode=0, stdout="httpx==0.28.1\n")
    monkeypatch.setattr("managed_agents.sources.subprocess.run", export)
    source = uv_source()
    bundle = bundle_source(source, "agent", selection)
    assert bundle.dependency_format == "uv"
    assert bundle.requirements == ".agent-platform-requirements.txt"
    assert ".agent-platform-requirements.txt" not in source
    with tarfile.open(fileobj=io.BytesIO(bundle.archive), mode="r:gz") as archive:
        assert archive.extractfile(bundle.requirements).read() == b"httpx==0.28.1\n"
        assert archive.extractfile("uv.lock").read() == source["uv.lock"]


def test_auto_prefers_existing_requirements(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Existing requirements must not run uv")
    monkeypatch.setattr("managed_agents.sources.subprocess.run", forbidden)
    bundle = bundle_source({**uv_source(), "requirements.txt": b"httpx==0.28.1\n"}, "agent")
    assert bundle.dependency_format == "requirements"
    assert bundle.requirements == "requirements.txt"


def test_uv_lock_is_required():
    files = uv_source()
    del files["uv.lock"]
    with pytest.raises(SourceError, match="both pyproject.toml and uv.lock"):
        bundle_source(files, "agent")


@pytest.mark.parametrize("source", ['{ directory = "../private" }',
                                    '{ registry = "https://private.example/simple" }'])
def test_uv_unsupported_sources_rejected(source):
    files = uv_source()
    files["uv.lock"] = ('version = 1\n[[package]]\nname = "private"\nsource = ' + source).encode()
    with pytest.raises(SourceError, match="public PyPI"):
        bundle_source(files, "agent")


def test_uv_export_failure_does_not_expose_subprocess_output(monkeypatch):
    monkeypatch.setattr("managed_agents.sources.shutil.which", lambda name: "uv")
    monkeypatch.setattr("managed_agents.sources.subprocess.run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=1, stdout="private credential information"))
    with pytest.raises(SourceError, match="could not export locked") as error:
        bundle_source(uv_source(), "agent")
    assert "private credential" not in str(error.value)


def test_uv_export_timeout_is_actionable(monkeypatch):
    monkeypatch.setattr("managed_agents.sources.shutil.which", lambda name: "uv")
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("uv", 30)
    monkeypatch.setattr("managed_agents.sources.subprocess.run", timeout)
    with pytest.raises(SourceError, match="timed out"):
        bundle_source(uv_source(), "agent")


@pytest.mark.parametrize("metadata", [b'project = "invalid"',
                                      b'[project]\nname = "demo"\n[tool]\nuv = "invalid"'])
def test_uv_invalid_metadata_shape_is_rejected(metadata):
    files = uv_source()
    files["pyproject.toml"] = metadata
    with pytest.raises(SourceError, match="standard"):
        bundle_source(files, "agent")


def test_uv_dynamic_metadata_is_not_executed():
    files = uv_source()
    files["pyproject.toml"] = b'[project]\nname = "demo"\ndynamic = ["dependencies"]\n'
    with pytest.raises(SourceError, match="Dynamic project metadata"):
        bundle_source(files, "agent")


def test_uv_missing_executable_has_actionable_error(monkeypatch):
    monkeypatch.setattr("managed_agents.sources.shutil.which", lambda name: None)
    with pytest.raises(SourceError, match="uv must be installed"):
        bundle_source(uv_source(), "agent")


def test_adk_source_is_wrapped_without_importing_uploaded_code():
    files = {"agent.py": b"raise RuntimeError('must never import locally')",
             "requirements.txt": b"google-cloud-aiplatform[agent_engines,adk]==1.163.0\n"}
    bundle = bundle_source(files, "agent", framework="google-adk", object_name="weather_agent")
    assert bundle.framework == "google-adk"
    assert bundle.entrypoint_module == "_agent_platform_adk"
    assert bundle.entrypoint_object == "root_agent"
    assert "_agent_platform_adk.py" not in files
    with tarfile.open(fileobj=io.BytesIO(bundle.archive), mode="r:gz") as archive:
        wrapper = archive.extractfile("_agent_platform_adk.py").read().decode()
        compile(wrapper, "_agent_platform_adk.py", "exec")
        assert "import_module('agent'), 'weather_agent'" in wrapper
        assert "AdkApp(agent=source_agent)" in wrapper


def test_adk_reserved_wrapper_cannot_be_overwritten():
    with pytest.raises(SourceError, match="reserved"):
        bundle_source({"agent.py": b"", "requirements.txt": b"", "_agent_platform_adk.py": b""},
                      "agent", framework="google-adk")


def test_adk_session_databases_are_not_uploaded():
    files = zip_files(make_zip({"weather/agent.py": "root_agent = object()",
        "weather/requirements.txt": "", "weather/.adk/session.db": "private session data"}))
    assert set(files) == {"agent.py", "requirements.txt"}