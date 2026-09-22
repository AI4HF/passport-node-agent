FROM python:3.11-slim

WORKDIR /opt/agent

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY agent ./agent

# The agent runs one module per invocation rather than as a daemon: each has its own trigger, so the
# site schedules them separately. The module is the command's argument.
ENTRYPOINT ["python", "-m", "agent"]
CMD ["dataset-sync"]
