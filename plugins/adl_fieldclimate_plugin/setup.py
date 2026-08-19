#!/usr/bin/env python
import os

from setuptools import find_packages, setup

PROJECT_DIR = os.path.dirname(__file__)
REQUIREMENTS_DIR = os.path.join(PROJECT_DIR, "requirements")
VERSION = "0.1.0"


def get_requirements(env):
    with open(os.path.join(REQUIREMENTS_DIR, f"{env}.txt")) as fp:
        return [
            x.strip()
            for x in fp.read().split("\n")
            if not x.strip().startswith("#") and not x.strip().startswith("-")
        ]


install_requires = get_requirements("base")

setup(
    name="adl-fieldclimate-plugin",
    version=VERSION,
    url="https://github.com/wmo-raf/adl-fieldclimate-plugin",
    author="WMO RAF",
    author_email="eotento@wmo.int",
    license="MIT",
    description="ADL Plugin fro collection observation data from FieldClimate API",
    long_description="TODO",
    platforms=["linux"],
    package_dir={"": "src"},
    packages=find_packages("src"),
    include_package_data=True,
    install_requires=install_requires
)
