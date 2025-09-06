#!/usr/bin/env python3
"""Development scripts for formatting and linting."""

import subprocess
import sys
from pathlib import Path


def format_code():
    """Format code using isort and black."""
    print("Running isort...")
    subprocess.run(["uv", "run", "isort", "main.py"], check=True)
    
    print("Running black...")
    subprocess.run(["uv", "run", "black", "main.py"], check=True)
    
    print("✅ Code formatting complete!")


def lint_code():
    """Run pylint on the code."""
    print("Running pylint...")
    result = subprocess.run(["uv", "run", "pylint", "main.py"])
    
    if result.returncode == 0:
        print("✅ Pylint passed!")
    else:
        print("❌ Pylint found issues")
        sys.exit(result.returncode)


def check_all():
    """Run all formatting and linting."""
    format_code()
    lint_code()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Development scripts")
    parser.add_argument("action", choices=["format", "lint", "check"], 
                       help="Action to perform")
    
    args = parser.parse_args()
    
    if args.action == "format":
        format_code()
    elif args.action == "lint":
        lint_code()
    elif args.action == "check":
        check_all()