import sys
from pathlib import Path

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    from data360agent.agent import root_agent

    history = []
    while True:
        message = input("You: ").strip()
        if message.lower() in {"exit", "quit"}:
            break
        if not message:
            continue
        result = root_agent.query(message=message, history=history)
        print("Agent:", result["reply"])
        if result.get("tools_unavailable"):
            print("Data360 warning:", result["tools_unavailable"])
        history.extend([
            {"role": "user", "content": message},
            {"role": "assistant", "content": result["reply"]},
        ])


if __name__ == "__main__":
    main()