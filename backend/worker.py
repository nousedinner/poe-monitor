import signal
from app import create_app
from app.scheduler import MonitorScheduler


def main():
    worker = MonitorScheduler(create_app())
    for name in ["SIGINT", "SIGTERM"]:
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: worker.shutdown())
    worker.run()


if __name__ == "__main__":
    main()
