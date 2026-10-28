# The attacker container. The agent's shell and http_request run here, on
# shared_net next to the task's containers, and never inside the target.
#
# Tools are baked in rather than installed per task at run time. Cybench's
# metadata/requirements.sh is not run: it would hit the network on conference
# wifi for every attendee at session start, and the chosen task (Flag Command)
# needs only jq. Add tools here, or build your own image and set
# `attacker_image` on the task.
#
# Playwright and a headless Chromium are here for the recon lab's browser
# comparison: the agent drives a browser by running a Playwright script through
# the shell tool, so every browser session passes the scope gate as a shell
# command in this container. See BROWSER.md. `--only-shell` installs the
# headless build only, which is all a container without a display can use.
FROM python:3.12-slim
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates jq netcat-openbsd \
 && pip install --no-cache-dir requests playwright==1.63.0 \
 && playwright install --with-deps --only-shell chromium \
 && chmod -R a+rX /ms-playwright \
 && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home agent
USER agent
WORKDIR /home/agent
CMD ["sleep", "infinity"]
