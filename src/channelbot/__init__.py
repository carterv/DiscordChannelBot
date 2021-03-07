from pathlib import Path

from dotenv import load_dotenv

from channelbot.bot import ChannelBot


def main():
    load_dotenv(dotenv_path=Path("./.env"))
    ChannelBot().run()


if __name__ == "__main__":
    main()
