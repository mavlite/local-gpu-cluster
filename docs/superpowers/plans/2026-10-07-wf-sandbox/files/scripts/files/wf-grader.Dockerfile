# Grading image for the workforce harness (Plan C grade.DockerRunner; Plan B builds it in the sandbox).
# Run with --network none. constraints.txt is the sandbox venv's `pip freeze`, so the grader and the
# agents' own test runs use identical package versions.
FROM python:3.13-slim
COPY constraints.txt /tmp/constraints.txt
RUN pip install --no-cache-dir -r /tmp/constraints.txt && rm /tmp/constraints.txt
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /w
