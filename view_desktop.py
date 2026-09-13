from desktop_env.desktop_env import DesktopEnv


def main():
    env = None
    try:
        print("Starting the OSWorld desktop...")
        env = DesktopEnv(provider_name="docker", headless=True)
        env.reset()

        url = f"http://localhost:{env.vnc_port}/vnc.html?autoconnect=true&resize=scale"
        print("\nOSWorld desktop is ready:")
        print(url)
        input("\nOpen the URL in a browser. Press Enter here when you want to stop... ")
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        if env is not None:
            env.close()
        print("Environment closed.")


if __name__ == "__main__":
    main()
