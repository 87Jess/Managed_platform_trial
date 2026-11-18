"""The console registers each custom agent's real query() signature, read from source."""

from pathlib import Path

from managed_agents.sources import DEFAULT_METHODS, infer_class_methods

ROOT = Path(__file__).resolve().parents[1]


def methods(source: str, extra: dict[str, bytes] | None = None):
    files = {"agent.py": source.encode(), **(extra or {})}
    return infer_class_methods(files, "agent", "root_agent")


def test_data360_example_registers_its_real_signature():
    files = {"agent.py": (ROOT / "examples/data360-agent/agent.py").read_bytes()}
    found, inferred = infer_class_methods(files, "agent", "root_agent")
    assert inferred
    query = found[0]
    assert query["name"] == "query" and query["api_mode"] == ""
    assert set(query["parameters"]["properties"]) == {"message", "user_id", "history"}
    assert query["parameters"]["properties"]["message"] == {"type": "string"}
    assert query["parameters"]["properties"]["history"] == {"type": "array", "items": {"type": "object"}}
    assert "required" not in query["parameters"]  # message also accepted as input/prompt


def test_types_defaults_and_stream_methods():
    found, inferred = methods('''
class Agent:
    def set_up(self): ...
    def query(self, *, input: dict, limit: int = 5, verbose: bool | None = None, **kwargs): ...
    def stream_query(self, prompt: str): ...
    def _private(self, x): ...
root_agent = Agent()
''')
    assert inferred
    by_name = {method["name"]: method for method in found}
    assert set(by_name) == {"query", "stream_query"}
    assert by_name["stream_query"]["api_mode"] == "stream"
    assert by_name["query"]["parameters"] == {
        "type": "object",
        "properties": {"input": {"type": "object"}, "limit": {"type": "integer"}, "verbose": {"type": "boolean"}},
        "required": ["input"],
    }


def test_class_imported_from_another_module():
    found, inferred = methods(
        "from .impl import Worker as W\nroot_agent = W()\n",
        {"impl.py": b"class Worker:\n    def query(self, question: str): ...\n"},
    )
    assert inferred and found[0]["parameters"]["required"] == ["question"]


def test_falls_back_when_the_signature_cannot_be_read():
    assert methods("root_agent = make_agent()\n") == (DEFAULT_METHODS, False)
    assert methods("def broken(:\n") == (DEFAULT_METHODS, False)
    assert methods("class A:\n    pass\nroot_agent = A()\n") == (DEFAULT_METHODS, False)


def test_every_array_has_items():
    found, _ = methods("class A:\n    def query(self, tags: list, ids: list[int] | None = None): ...\nroot_agent = A()\n")
    props = found[0]["parameters"]["properties"]
    assert props["tags"] == {"type": "array", "items": {"type": "string"}}
    assert props["ids"] == {"type": "array", "items": {"type": "integer"}}
