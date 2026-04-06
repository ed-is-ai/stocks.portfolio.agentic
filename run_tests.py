#!/usr/bin/env python3
"""
Test runner script for stock agent tests.
Runs pytest and generates test results for the dashboard.
"""

import subprocess
import sys
import json
import os
from pathlib import Path


def run_tests():
    """Run pytest and capture results."""
    print("🚀 Running Stock Agent Tests...")

    # Run pytest with JSON output
    cmd = [
        sys.executable, "-m", "pytest",
        "--json-report",
        "--json-report-file=test-results.json",
        "tests/"
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=Path(__file__).parent)

        print("📊 Test Results:")
        print(result.stdout)

        if result.stderr:
            print("⚠️  Warnings/Errors:")
            print(result.stderr)

        return result.returncode == 0

    except Exception as e:
        print(f"❌ Error running tests: {e}")
        return False


def generate_dashboard_data():
    """Generate dashboard-friendly test data from pytest JSON output."""
    results_file = Path("test-results.json")

    if not results_file.exists():
        print("⚠️  No test results file found")
        return

    try:
        with open(results_file) as f:
            data = json.load(f)

        # Process test results
        dashboard_data = {
            "summary": {
                "total": data.get("summary", {}).get("num_tests", 0),
                "passed": data.get("summary", {}).get("passed", 0),
                "failed": data.get("summary", {}).get("failed", 0),
                "skipped": data.get("summary", {}).get("skipped", 0),
                "errors": data.get("summary", {}).get("errors", 0)
            },
            "tests": []
        }

        # Process individual test results
        for test in data.get("tests", []):
            test_info = {
                "name": test.get("nodeid", "").split("::")[-1],
                "file": test.get("nodeid", "").split("::")[0],
                "status": test.get("outcome", "unknown"),
                "duration": ".3f" if test.get("duration") else "N/A",
                "error": test.get("call", {}).get("crash") or test.get("call", {}).get("traceback")
            }
            dashboard_data["tests"].append(test_info)

        # Save dashboard data
        with open("dashboard-data.json", "w") as f:
            json.dump(dashboard_data, f, indent=2)

        print("✅ Dashboard data generated: dashboard-data.json")

    except Exception as e:
        print(f"❌ Error generating dashboard data: {e}")


def main():
    """Main test runner function."""
    print("=" * 50)
    print("Stock Agent Test Suite")
    print("=" * 50)

    # Run tests
    success = run_tests()

    # Generate dashboard data
    generate_dashboard_data()

    print("\n" + "=" * 50)
    if success:
        print("✅ All tests completed successfully!")
    else:
        print("❌ Some tests failed. Check the output above.")

    print("📊 Open dashboard.html in your browser to view results.")
    print("=" * 50)

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())