"""pytest plugin: write the node ids that failed/errored (any phase, any subtest) to $FAIL_OUT."""

import os

failed = set()
broken_files = set()


def pytest_runtest_logreport(report):
    if report.failed:
        failed.add(report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        broken_files.add(report.nodeid)


def pytest_unconfigure(config):
    path = os.environ.get("FAIL_OUT")
    if path and not os.environ.get("PYTEST_XDIST_WORKER"):
        with open(path, "w") as f:
            for n in sorted(failed):
                f.write(f"T {n}\n")
            for n in sorted(broken_files):
                f.write(f"F {n}\n")
