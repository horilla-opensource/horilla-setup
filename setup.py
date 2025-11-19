from setuptools import setup, find_packages

setup(
    name="horillasetup",
    version="1.0.0",
    packages=find_packages(),
    install_requires=[],
    entry_points={
        "console_scripts": [
            "horillasetup=horillasetup.ctl:main",
        ],
    },
    author="Horilla",
    author_email="support@horilla.com",
    description="CLI tool to build and manage Horilla projects",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    url="https://github.com/horilla-opensource/setup",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: GNU Lesser General Public License v3 (LGPLv3)",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.10",
)
