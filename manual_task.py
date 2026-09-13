import argparse
import json
from pathlib import Path

from desktop_env.desktop_env import DesktopEnv


DEFAULT_TASK = Path(
    "evaluation_examples/examples/os/28cc3b7e-b194-4bc9-8353-d04c0f4d56d2.json"
)


def parse_args():
    parser = argparse.ArgumentParser(description="Manually complete one OSWorld task.")
    parser.add_argument("--task", type=Path, default=DEFAULT_TASK)
    parser.add_argument(
        "--enable-proxy",
        action="store_true",
        help="Enable proxy setup for tasks whose JSON contains proxy=true.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    with args.task.open("r", encoding="utf-8") as task_file:
        task = json.load(task_file)

    env = None
    try:
        print("Starting the OSWorld task environment...")
        env = DesktopEnv(
            provider_name="docker",
            headless=True,
            enable_proxy=args.enable_proxy,
        )
        env.reset(task_config=task)

        url = f"http://localhost:{env.vnc_port}/vnc.html?autoconnect=true&resize=scale"
        print(f"\nTask ID: {task['id']}")
        print(f"Instruction: {task['instruction']}")
        print(f"Desktop: {url}")
        input("\nComplete the task in the browser, then press Enter here to evaluate... ")

        score = float(env.evaluate())
        print(f"\nScore: {score:.1f}")
        print("PASS" if score == 1.0 else "FAIL")
    except KeyboardInterrupt:
        print("\nStopped without evaluation.")
    finally:
        if env is not None:
            env.close()
        print("Environment closed.")


if __name__ == "__main__":
    main()
