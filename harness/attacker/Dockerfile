# The attacker container. The agent's shell and http_request run here, on
# shared_net next to the task's containers, and never inside the target.
#
# Tools are baked in rather than installed per task at run time. Cybench's
# metadata/requirements.sh is not run: it would hit the network on conference
# wifi for every attendee at session start, and the chosen task (Flag Command)
# needs only jq. Add tools here, or build your own image and set
# `attacker_image` on the task.
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates jq netcat-openbsd \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir requests
RUN useradd --create-home agent
USER agent
WORKDIR /home/agent
CMD ["sleep", "infinity"]
