from setuptools import setup, find_packages

setup(
    name="DynamicChannels",
    version="1.0",
    description="A discord bot for creating channels on demand",
    author="Carter Van Deuren",
    author_email="carter.van.deuren@gmail.com",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    entry_points={"console_scripts": ["channelbot = channelbot:main"]},
)
