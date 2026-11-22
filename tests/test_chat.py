import sys
from pathlib import Path

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    from wdi_agent.agent import root_agent

    history = []
    while True:
        message = input("You: ").strip()
        if message.lower() in {"exit", "quit"}:
            break
        if not message:
            continue
        result = root_agent.query(message=message, history=history)
        print("Agent:", result["reply"])
        if result.get("tools_used"):
            print("  tools:", ", ".join(result["tools_used"]))
        history.extend([
            {"role": "user", "content": message},
            {"role": "assistant", "content": result["reply"]},
        ])


if __name__ == "__main__":
    main()