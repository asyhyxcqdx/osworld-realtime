from __future__ import annotations
import argparse
import datetime
import fcntl
import json
import logging
import os
import sys
import signal
import time
from typing import List
from multiprocessing import Process, Manager
from multiprocessing import current_process

# Add project root to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import lib_run_single
from desktop_env.desktop_env import DesktopEnv

# Global variables for signal handling
active_environments = []
processes = []
is_terminating = False

# import wandb

# load the environment variables from .env file
if os.path.exists(".env"):
    from dotenv import load_dotenv
    load_dotenv()

#  Logger Configs {{{ #
def config() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run end-to-end evaluation on the benchmark"
    )

    # environment config
    parser.add_argument("--path_to_vm", type=str, default=None)
    parser.add_argument(
        "--vm_secret_mount",
        action="append",
        default=None,
        help=(
            "Inject a local secret file into each VM as local_path:guest_path. "
            "Can be specified multiple times."
        ),
    )
    parser.add_argument(
        "--headless", action="store_true", help="Run in headless machine"
    )
    parser.add_argument(
        "--action_space", type=str, default="pyautogui", help="Action type"
    )
    parser.add_argument(
        "--observation_type",
        choices=["screenshot", "a11y_tree", "screenshot_a11y_tree", "som"],
        default="screenshot",
        help="Observation type",
    )
    parser.add_argument("--sleep_after_execution", type=float, default=0.0)
    parser.add_argument("--max_steps", type=int, default=15)

    # agent config
    parser.add_argument("--max_trajectory_length", type=int, default=3)
    parser.add_argument(
        "--test_config_base_dir", type=str, default="evaluation_examples"
    )

    # lm config
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--top_p", type=float, default=0.9)
    parser.add_argument("--max_tokens", type=int, default=128000)
    parser.add_argument("--stop_token", type=str, default=None)
    parser.add_argument(
        "--api_format",
        choices=["auto", "openai_chat", "anthropic_messages", "openai_responses"],
        default="auto",
        help="Wire format used to call the model API",
    )
    parser.add_argument(
        "--api_base_url",
        type=str,
        default=os.environ.get("PACKY_API_BASE_URL"),
        help="Model API gateway root URL; protocol paths are appended automatically",
    )
    
    # example config
    parser.add_argument("--domain", type=str, default="all")
    parser.add_argument(
        "--test_all_meta_path", type=str, default="evaluation_examples/test_all.json"
    )

    # logging related
    parser.add_argument("--result_dir", type=str, default="./results")
    parser.add_argument("--num_envs", type=int, default=1, help="Number of environments to run in parallel")  
    parser.add_argument("--log_level", type=str, choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'], 
                       default='INFO', help="Set the logging level")
    # aws config
    parser.add_argument(
        "--region", type=str, default="us-east-1", help="AWS region for the VM"
    )
    parser.add_argument(
        "--provider_name", type=str, default="docker", choices=["aws", "virtualbox", "vmware", "docker", "azure", "pyromind", "modal", "daytona"], help="Provider name"
    )
    parser.add_argument(
        "--client_password", type=str, default="", help="Client password"
    )
    parser.add_argument(
        "--screen_width", type=int, default=1920, help="Screen width"
    )
    parser.add_argument(
        "--screen_height", type=int, default=1080, help="Screen height"
    )
    parser.add_argument("--agent_variant", choices=["agent1", "agent2", "agent3", "agent4"], default=None)
    parser.add_argument(
        "--agent_config",
        type=str,
        default=None,
        help="Validated YAML config for a realtime Agent; defaults to the model-specific file.",
    )
    parser.add_argument(
        "--run_id", default=None,
        help="Four-agent experiment ID; reuse it across agents and for resume. Defaults to a new timestamp.",
    )
    parser.add_argument("--tool_format", choices=["native", "json"], default="native")
    parser.add_argument("--max_frame_queries", type=int, default=0,
                        help="Frame queries per decision; 0 (default) means unlimited")
    parser.add_argument("--thinking_summary", action="store_true",
                        help="Request adaptive thinking summaries from a compatible Anthropic Messages model")
    parser.add_argument("--max_sequence_actions", type=int, default=100)
    parser.add_argument("--recording_fragment_ms", type=int, default=100)
    parser.add_argument("--environment_ready_wait_s", type=float, default=60)
    parser.add_argument("--evaluation_settle_s", type=float, default=20)
    parser.add_argument("--install_realtime_server", action="store_true", help="Install the realtime extension in the VM after reset")
    args = parser.parse_args()
    if args.agent_variant:
        from mm_agents.realtime_config import agent_kwargs, default_config_path, load_realtime_config

        config_path = args.agent_config or default_config_path(
            args.agent_variant, args.model or "claude-sonnet-5"
        )
        try:
            realtime_config = load_realtime_config(config_path, variant=args.agent_variant)
        except (OSError, ValueError) as exc:
            parser.error(f"Invalid realtime Agent config {config_path}: {exc}")
        config_values = agent_kwargs(realtime_config)
        args.realtime_config_path = str(config_path)
        args.realtime_config = realtime_config
        args.model = config_values["model"]
        args.api_format = config_values["api_format"]
        args.max_tokens = config_values["max_tokens"]
        args.temperature = config_values["temperature"]
        args.max_trajectory_length = config_values["max_trajectory_length"]
        args.max_sequence_actions = config_values["max_sequence_actions"]
        args.max_frame_queries = config_values["max_frame_queries"]
        args.tool_format = config_values["tool_format"]
        args.thinking_summary = config_values["thinking_summary"]
        if args.action_space != "computer_13" or args.observation_type != "screenshot":
            parser.error("Four-agent experiments require --action_space computer_13 --observation_type screenshot")
        if not 1 <= args.max_sequence_actions <= 100 or args.max_frame_queries < 0:
            parser.error("Invalid sequence/query budget")
        if args.thinking_summary and not (
                args.api_format == "anthropic_messages"
                or (args.api_format == "auto" and args.model.startswith("claude"))):
            parser.error("--thinking_summary requires Anthropic Messages")
        if min(args.sleep_after_execution, args.environment_ready_wait_s, args.evaluation_settle_s) < 0:
            parser.error("Wait values must be nonnegative")
        started = datetime.datetime.now().astimezone()
        args.agent_started_at = started.isoformat(timespec="microseconds")
        if args.run_id is None:
            args.run_id = started.strftime("%Y%m%dT%H%M%S.%f%z")
        if (not args.run_id or args.run_id in {".", ".."}
                or any(c in args.run_id for c in "/\\")
                or any(ord(c) < 32 for c in args.run_id)):
            parser.error("run_id must be a single nonempty directory name")
        # Group comparisons by model and experiment; isolate each agent's resume
        # checks and scores. The timestamp is created once for all worker VMs.
        args.result_dir = os.path.join(
            args.result_dir, args.model, args.run_id, args.agent_variant
        )
    elif args.run_id is not None:
        parser.error("run_id is supported by the four-agent runner; set --agent_variant")
    elif args.api_format == "openai_responses":
        parser.error("openai_responses is supported by the four-agent runner; set --agent_variant")
    return args

args = config()  # Get command line arguments first

logger = logging.getLogger()
log_level = getattr(logging, args.log_level.upper())
logger.setLevel(log_level)

datetime_str: str = datetime.datetime.now().strftime("%Y%m%d@%H%M%S")

file_handler = logging.FileHandler(
    os.path.join("logs", "normal-{:}.log".format(datetime_str)), encoding="utf-8"
)
debug_handler = logging.FileHandler(
    os.path.join("logs", "debug-{:}.log".format(datetime_str)), encoding="utf-8"
)
stdout_handler = logging.StreamHandler(sys.stdout)

file_handler.setLevel(logging.INFO)
debug_handler.setLevel(logging.DEBUG)
stdout_handler.setLevel(log_level)

formatter = logging.Formatter(
    fmt="\x1b[1;33m[%(asctime)s \x1b[31m%(levelname)s \x1b[32m%(module)s/%(lineno)d-%(processName)s\x1b[1;33m] \x1b[0m%(message)s"
)
file_handler.setFormatter(formatter)
debug_handler.setFormatter(formatter)
stdout_handler.setFormatter(formatter)

stdout_handler.addFilter(logging.Filter("desktopenv"))

logger.addHandler(file_handler)
logger.addHandler(debug_handler)
logger.addHandler(stdout_handler)
#  }}} Logger Configs #

logger = logging.getLogger("desktopenv.experiment")


def distribute_tasks(test_all_meta: dict) -> List[tuple]:
    all_tasks = []
    for domain, examples in test_all_meta.items():
        for example_id in examples:
            all_tasks.append((domain, example_id))
    return all_tasks


def get_result_dir(
    result_dir, action_space, observation_type, use_model, agent_variant=None
):
    """Resolve the same run directory for writing, resume checks and scores.

    config() scopes result_dir to <root>/<model>/<run_id>/<agent> for four-agent runs.
    Legacy runs still place the model beneath the observation type.
    """
    parts = [result_dir, action_space, observation_type]
    if not agent_variant:
        parts.append(use_model)
    return os.path.join(*parts)


def write_run_configuration(args):
    target = get_result_dir(
        args.result_dir, args.action_space, args.observation_type,
        args.model, args.agent_variant,
    )
    os.makedirs(target, exist_ok=True)
    with open(os.path.join(target, "args.json"), "w", encoding="utf-8") as f:
        json.dump(vars(args), f, indent=4)
    if args.agent_variant:
        # The shared run_id identifies the comparison batch, not a simultaneous
        # start. Append each launch (including resume) instead of overwriting it.
        launch = {
            "run_id": args.run_id,
            "model": args.model,
            "agent_variant": args.agent_variant,
            "agent_started_at": args.agent_started_at,
        }
        with open(os.path.join(args.result_dir, "launches.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(launch) + "\n")
        batch_root = os.path.dirname(args.result_dir)
        os.makedirs(batch_root, exist_ok=True)
        manifest_path = os.path.join(batch_root, "experiment_manifest.json")
        # The four commands may start concurrently. Keep the first launch as
        # the batch creation time and serialize the shared append log.
        with open(os.path.join(batch_root, ".experiment.lock"), "a", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if not os.path.exists(manifest_path):
                manifest = {
                    "run_id": args.run_id,
                    "model": args.model,
                    "created_at": args.agent_started_at,
                    "action_space": args.action_space,
                    "observation_type": args.observation_type,
                    "agents": ["agent1", "agent2", "agent3", "agent4"],
                }
                temporary = manifest_path + f".tmp.{os.getpid()}"
                with open(temporary, "w", encoding="utf-8") as f:
                    json.dump(manifest, f, indent=2)
                    f.write("\n")
                os.replace(temporary, manifest_path)
            with open(os.path.join(batch_root, "launches.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(launch) + "\n")
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def process_signal_handler(signum, frame, env_idx):
    """Signal handler for child processes to gracefully shut down their environments."""
    logger.info(f"Process {env_idx + 1} received signal {signum}. Shutting down...")
    
    # Get the active_environments from the caller's frame
    local_vars = frame.f_locals
    active_environments = local_vars.get('active_environments', [])
    
    # Close environment in the current process context
    for env in active_environments:
        if env is not None:
            try:
                logger.info(f"Process {env_idx + 1} closing environment...")
                env.close()
                logger.info(f"Process {env_idx + 1} environment closed successfully")
            except Exception as e:
                logger.error(f"Process {env_idx + 1} error closing environment: {e}")
    
    logger.info(f"Process {env_idx + 1} shutdown complete. Exiting.")
    sys.exit(0)


def run_env_tasks(task_queue: Queue, args: argparse.Namespace, shared_scores: list):
    active_environments = []
    env = None
    try:
        screen_size = (args.screen_width, args.screen_height)
        env_kwargs = dict(
            path_to_vm=args.path_to_vm,
            action_space=args.action_space,
            provider_name=args.provider_name,
            screen_size=screen_size,
            headless=args.headless,
            os_type="Ubuntu",
            require_a11y_tree=args.observation_type in ["a11y_tree", "screenshot_a11y_tree", "som"],
            enable_proxy=True,
            client_password=args.client_password,
            vm_secret_mounts=args.vm_secret_mount,
        )
        if args.provider_name == "aws":
            from desktop_env.providers.aws.manager import IMAGE_ID_MAP
            REGION = args.region
            ami_id = IMAGE_ID_MAP[REGION].get(screen_size, IMAGE_ID_MAP[REGION][(1920, 1080)])
            env_kwargs["region"] = REGION
            env_kwargs["snapshot_name"] = ami_id
        elif args.provider_name == "daytona":
            snapshot_name = os.environ.get("DAYTONA_OSWORLD_SNAPSHOT")
            if snapshot_name:
                env_kwargs["snapshot_name"] = snapshot_name
        env = DesktopEnv(**env_kwargs)
        active_environments.append(env)
        if args.agent_variant:
            from mm_agents.realtime_agent import RealtimeAgent
            agent = RealtimeAgent(
                variant=args.agent_variant, model=args.model, max_tokens=args.max_tokens,
                temperature=args.temperature, max_trajectory_length=args.max_trajectory_length,
                api_format=args.api_format, api_base_url=args.api_base_url,
                pause=args.sleep_after_execution, max_sequence_actions=args.max_sequence_actions,
                max_frame_queries=args.max_frame_queries, tool_format=args.tool_format,
                thinking_summary=args.thinking_summary,
                system_prompt_text=args.realtime_config["system_prompt"],
            )
        else:
            from mm_agents.agent import PromptAgent
            agent = PromptAgent(
                model=args.model,
                max_tokens=args.max_tokens,
                top_p=args.top_p,
                temperature=args.temperature,
                action_space=args.action_space,
                observation_type=args.observation_type,
                max_trajectory_length=args.max_trajectory_length,
                client_password=args.client_password,
                api_format=args.api_format,
                api_base_url=args.api_base_url,
            )

        logger.info(f"Process {current_process().name} started.")
        while True:
            try:
                item = task_queue.get(timeout=5)
            except Exception:
                break
            domain, example_id = item
            try:
                config_file = os.path.join(
                    args.test_config_base_dir, f"examples/{domain}/{example_id}.json"
                )
                with open(config_file, "r", encoding="utf-8") as f:
                    example = json.load(f)
                logger.info(f"[{current_process().name}][Domain]: {domain}")
                logger.info(f"[{current_process().name}][Example ID]: {example_id}")
                logger.info(f"[{current_process().name}][Instruction]: {example['instruction']}")
                example_result_dir = os.path.join(
                    get_result_dir(
                        args.result_dir, args.action_space,
                        args.observation_type, args.model, args.agent_variant,
                    ),
                    domain,
                    example_id,
                )
                os.makedirs(example_result_dir, exist_ok=True)
                try:
                    lib_run_single.run_single_example(
                        agent,
                        env,
                        example,
                        args.max_steps,
                        example["instruction"],
                        args,
                        example_result_dir,
                        shared_scores,
                    )
                except Exception as e:
                    import traceback
                    logger.error(f"Exception in {current_process().name} {domain}/{example_id}: {e}")
                    logger.error(traceback.format_exc())
                    try:
                        if not args.agent_variant:
                            env.controller.end_recording(os.path.join(example_result_dir, "recording.mp4"))
                    except Exception as rec_e:
                        logger.error(f"Failed to end recording: {rec_e}")
                    with open(os.path.join(example_result_dir, "traj.jsonl"), "a") as f:
                        f.write(
                            json.dumps(
                                {"Error": f"{domain}/{example_id} - {e}"}
                            )
                        )
                        f.write("\n")
            except Exception as e:
                logger.error(f"Task-level error in {current_process().name}: {e}")
                import traceback
                logger.error(traceback.format_exc())
    except Exception as e:
        logger.error(f"Process-level error in {current_process().name}: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        logger.info(f"{current_process().name} cleaning up environment...")
        try:
            if env:
                env.close()
                logger.info(f"{current_process().name} environment closed successfully")
        except Exception as e:
            logger.error(f"{current_process().name} error during environment cleanup: {e}")


def signal_handler(signum, frame):
    """Handle termination signals (SIGINT, SIGTERM) to gracefully shutdown environments."""
    global is_terminating, active_environments, processes
    
    # Avoid duplicate handling
    if is_terminating:
        return
    
    is_terminating = True
    logger.info(f"Received signal {signum}. Gracefully shutting down...")
    
    # Close all registered environments in the main process
    for env in active_environments:
        try:
            logger.info(f"Closing environment...")
            env.close()
            logger.info(f"Environment closed successfully")
        except Exception as e:
            logger.error(f"Error closing environment: {e}")
    
    # Send termination signal to all child processes first
    for p in processes:
        if p.is_alive():
            try:
                logger.info(f"Sending termination signal to process {p.name}...")
                p.terminate()
            except Exception as e:
                logger.error(f"Error sending termination signal to process: {e}")
    
    # Allow a short time for processes to handle their own cleanup
    time.sleep(1)
    
    # Forcefully terminate any processes that didn't exit
    for p in processes:
        if p.is_alive():
            try:
                logger.info(f"Forcefully terminating process {p.name}...")
                import signal as sig
                os.kill(p.pid, sig.SIGKILL)
            except Exception as e:
                logger.error(f"Error forcefully terminating process: {e}")
    
    logger.info("Shutdown complete. Exiting.")
    sys.exit(0)


def test(args: argparse.Namespace, test_all_meta: dict) -> None:
    global processes
    logger.info("Args: %s", args)
    all_tasks = distribute_tasks(test_all_meta)
    logger.info(f"Total tasks: {len(all_tasks)}")
    with Manager() as manager:
        shared_scores = manager.list()
        task_queue = manager.Queue()
        for item in all_tasks:
            task_queue.put(item)
        num_envs = args.num_envs
        processes = []
        for i in range(num_envs):
            p = Process(
                target=run_env_tasks,
                args=(task_queue, args, shared_scores),
                name=f"EnvProcess-{i+1}"
            )
            p.daemon = True
            p.start()
            processes.append(p)
            logger.info(f"Started process {p.name} with PID {p.pid}")
        try:
            while True:
                alive_count = 0
                for idx, p in enumerate(processes):
                    if not p.is_alive():
                        logger.warning(f"Process {p.name} died, restarting...")
                        new_p = Process(
                            target=run_env_tasks,
                            args=(task_queue, args, shared_scores),
                            name=f"EnvProcess-Restart-{idx+1}"
                        )
                        new_p.daemon = True
                        new_p.start()
                        processes[idx] = new_p
                        logger.info(f"Restarted process {new_p.name} with PID {new_p.pid}")
                    else:
                        alive_count += 1
                if task_queue.empty():
                    logger.info("All tasks finished.")
                    break
                if alive_count == 0:
                    logger.error("All processes died, exiting.")
                    break
                time.sleep(5)
            for p in processes:
                p.join()
        except KeyboardInterrupt:
            logger.info("Main process received KeyboardInterrupt. Initiating graceful shutdown...")
            raise
        except Exception as e:
            logger.error(f"Unexpected error while waiting for processes: {e}", exc_info=True)
            for p in processes:
                if p.is_alive():
                    try:
                        logger.info(f"Terminating process {p.name} due to error...")
                        p.terminate()
                    except Exception as term_e:
                        logger.error(f"Error terminating process {p.name}: {term_e}")
            raise
        scores = list(shared_scores)
    logger.info(f"Average score: {sum(scores) / len(scores) if scores else 0}")


def get_unfinished(
    action_space, use_model, observation_type, result_dir, total_file_json,
    agent_variant=None,
):
    target_dir = get_result_dir(
        result_dir, action_space, observation_type, use_model, agent_variant
    )

    if not os.path.exists(target_dir):
        return total_file_json

    finished = {}
    for domain in os.listdir(target_dir):
        finished[domain] = []
        domain_path = os.path.join(target_dir, domain)
        if os.path.isdir(domain_path):
            for example_id in os.listdir(domain_path):
                if example_id == "onboard":
                    continue
                example_path = os.path.join(domain_path, example_id)
                if os.path.isdir(example_path):
                    if "result.txt" not in os.listdir(example_path):
                        # empty all files under example_id
                        for file in os.listdir(example_path):
                            os.remove(os.path.join(example_path, file))
                    else:
                        finished[domain].append(example_id)

    if not finished:
        return total_file_json

    for domain, examples in finished.items():
        if domain in total_file_json:
            total_file_json[domain] = [
                x for x in total_file_json[domain] if x not in examples
            ]

    return total_file_json


def get_result(
    action_space, use_model, observation_type, result_dir, total_file_json,
    agent_variant=None,
):
    target_dir = get_result_dir(
        result_dir, action_space, observation_type, use_model, agent_variant
    )
    if not os.path.exists(target_dir):
        print("New experiment, no result yet.")
        return None

    all_result = []

    for domain in os.listdir(target_dir):
        domain_path = os.path.join(target_dir, domain)
        if os.path.isdir(domain_path):
            for example_id in os.listdir(domain_path):
                example_path = os.path.join(domain_path, example_id)
                if os.path.isdir(example_path):
                    if "result.txt" in os.listdir(example_path):
                        # empty all files under example_id
                        try:
                            all_result.append(
                                float(
                                    open(
                                        os.path.join(example_path, "result.txt"), "r"
                                    ).read()
                                )
                            )
                        except:
                            all_result.append(0.0)

    if not all_result:
        print("New experiment, no result yet.")
        return None
    else:
        args.model = args.model or "gpt-4o"
        print("Current Success Rate:", sum(all_result) / len(all_result) * 100, "%")
        return all_result


if __name__ == "__main__":
    ####### The complete version of the list of examples #######
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    
    # Register signal handlers for graceful termination
    signal.signal(signal.SIGINT, signal_handler)  # Handle Ctrl+C
    signal.signal(signal.SIGTERM, signal_handler)  # Handle termination signal
    
    try:
        write_run_configuration(args)

        with open(args.test_all_meta_path, "r", encoding="utf-8") as f:
            test_all_meta = json.load(f)

        if args.domain != "all":
            test_all_meta = {args.domain: test_all_meta[args.domain]}

        test_file_list = get_unfinished(
            args.action_space,
            args.model,
            args.observation_type,
            args.result_dir,
            test_all_meta,
            args.agent_variant,
        )
        left_info = ""
        for domain in test_file_list:
            left_info += f"{domain}: {len(test_file_list[domain])}\n"
        logger.info(f"Left tasks:\n{left_info}")

        get_result(
            args.action_space,
            args.model,
            args.observation_type,
            args.result_dir,
            test_all_meta,
            args.agent_variant,
        )
        test(args, test_file_list)
    except KeyboardInterrupt:
        logger.info("Main process received KeyboardInterrupt.")
        # Signal handler will take care of cleanup
    except Exception as e:
        logger.error(f"Unexpected error in main process: {e}", exc_info=True)
        # Also trigger cleanup for unhandled exceptions
        signal_handler(signal.SIGTERM, None)
    finally:
        # Final cleanup in case any environments or processes remain
        logger.info("Main process final cleanup...")
        for env in active_environments:
            if env is not None:
                try:
                    logger.info(f"Closing environment in final cleanup...")
                    env.close()
                    logger.info(f"Environment closed successfully in final cleanup")
                except Exception as e:
                    logger.error(f"Error during final environment cleanup: {e}")
        
        # First try gentle termination
        for p in processes:
            if p is not None and p.is_alive():
                try:
                    logger.info(f"Terminating process {p.name}...")
                    p.terminate()
                except Exception as e:
                    logger.error(f"Error terminating process: {e}")
        
        # Wait a moment for processes to terminate
        time.sleep(1)
        
        # Then force kill if needed
        for p in processes:
            if p is not None and p.is_alive():
                try:
                    logger.info(f"Force killing process {p.name}...")
                    os.kill(p.pid, signal.SIGKILL)
                    logger.info(f"Process {p.name} force killed")
                except Exception as e:
                    logger.error(f"Error force killing process: {e}")
