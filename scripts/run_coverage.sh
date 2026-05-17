#!/bin/bash
# Script to run test coverage analysis
# Usage: ./scripts/run_coverage.sh

set -e

cd "$(dirname "$0")/.."

echo "Running tests with coverage analysis..."
python -m pytest --cov=apps --cov=config --cov-report=term --cov-report=html

echo ""
echo "Coverage report generated!"
echo "- Terminal report shown above"
echo "- HTML report available at: htmlcov/index.html"
echo ""
echo "To view HTML report in browser:"
echo "  xdg-open htmlcov/index.html"
