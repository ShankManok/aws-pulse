from setuptools import setup, find_packages

setup(
    name="aws-pulse-sdk",
    version="0.1.0",
    packages=find_packages(),
    install_requires=["boto3>=1.34.0"],
    python_requires=">=3.10",
    author="AWS Pulse Team",
    description="Python SDK for AWS Pulse",
)
