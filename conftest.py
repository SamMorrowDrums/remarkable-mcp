"""
Pytest configuration for remarkable-mcp tests.

Adds --run-integration flag for live SSH tests against a connected tablet.
"""

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run integration tests against a connected reMarkable tablet via SSH",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: tests requiring a connected reMarkable tablet")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-integration"):
        return
    skip_integration = pytest.mark.skip(reason="need --run-integration to run")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
