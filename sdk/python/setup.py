from setuptools import setup, find_packages

setup(
    name="aws-pulse-sdk",
    version="0.2.0",
    packages=find_packages(),
    install_requires=[
        "boto3>=1.34.0",
        "botocore>=1.34.0",
        "requests>=2.31.0",
        "pydantic>=2.5.0",
    ],
    python_requires=">=3.9",
    author="AWS Pulse Team",
    description="Python SDK for AWS Pulse - intelligent notification infrastructure",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    url="https://github.com/aws-pulse/aws-pulse",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
)
