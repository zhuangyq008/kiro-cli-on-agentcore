# AgentCore Runtime image: Python wrapper + kiro-cli, linux/arm64.
# aarch64 kiro-cli builds need glibc >= 2.39, so the base is Debian trixie (glibc 2.41).
FROM public.ecr.aws/docker/library/python:3.12-slim-trixie

ARG KIRO_CLI_VERSION=2.35.2
# Node.js LTS and uv, so MCP servers started with `npx` / `uvx` work (kac submit --kiro-dir).
ARG NODE_VERSION=24.22.2
ARG NODE_SHA256=eb187837a2230d4047161ac0be008584e16b46f726e9c9490ee35dc6b17d9ba8
ARG UV_VERSION=0.13.7

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl unzip jq ca-certificates openssh-client xz-utils \
 && rm -rf /var/lib/apt/lists/*

# AWS CLI v2 for read-only operations tasks.
RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o /tmp/awscliv2.zip \
 && unzip -q /tmp/awscliv2.zip -d /tmp \
 && /tmp/aws/install \
 && rm -rf /tmp/aws /tmp/awscliv2.zip

RUN useradd -m -u 1000 agent \
 && mkdir -p /workspace /tmp/artifacts \
 && chown agent:agent /workspace /tmp/artifacts

# Python dependencies from the lock file, then the package itself.
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv \
 && uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt \
 && pip install --no-cache-dir -r /tmp/requirements.txt \
 && pip uninstall -y uv \
 && rm /tmp/requirements.txt
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

# MCP server toolchain (after the dependency step above, which removes its own uv).
RUN curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-arm64.tar.xz" -o /tmp/node.tar.xz \
 && echo "${NODE_SHA256}  /tmp/node.tar.xz" | sha256sum -c - \
 && mkdir -p /opt/node \
 && tar -xJf /tmp/node.tar.xz -C /opt/node --strip-components=1 --no-same-owner \
 && rm /tmp/node.tar.xz \
 && pip install --no-cache-dir "uv==${UV_VERSION}"
ENV PATH=/opt/node/bin:$PATH
RUN node --version && npm --version && npx --version && uv --version && uvx --version

USER agent
WORKDIR /home/agent
ENV PATH=/home/agent/.local/bin:$PATH

# Pinned kiro-cli; the installer verifies the release checksum.
RUN curl -fsSL https://cli.kiro.dev/install -o /tmp/kiro-install.sh \
 && bash /tmp/kiro-install.sh --version "${KIRO_CLI_VERSION}" \
 && rm /tmp/kiro-install.sh \
 && kiro-cli --version

# Loop phase agents. (kiro sub-agents are enabled at run time, only for `subagent` loop mode tasks.)
RUN python -m kiro_runtime.agents install /home/agent/.kiro/agents

# The first V3 run unpacks its bundled runtime; do it at build time so tasks start faster.
# The placeholder key is rejected by the service, which is expected here.
RUN cd /tmp \
 && (KIRO_API_KEY=ksk_build_prewarm timeout 180 kiro-cli chat --v3 --no-interactive "prewarm" >/dev/null 2>&1 || true) \
 && rm -rf /home/agent/.kiro/sessions /home/agent/.kiro/session-index /home/agent/.kiro/logs

# Git: identity for commits, and a credential helper that reads GITHUB_TOKEN at use time
# so the token never lands in a file or a remote URL.
RUN git config --global user.name "kiro-agentcore" \
 && git config --global user.email "kiro-agentcore@users.noreply.github.com" \
 && git config --global init.defaultBranch main \
 && git config --global credential.https://github.com.helper \
    '!f() { test -n "$GITHUB_TOKEN" || exit 0; echo username=x-access-token; echo "password=$GITHUB_TOKEN"; }; f'

ENV WORKSPACE_ROOT=/workspace \
    ARTIFACTS_DIR_ROOT=/tmp/artifacts \
    KIRO_BIN=/home/agent/.local/bin/kiro-cli

EXPOSE 8080
CMD ["opentelemetry-instrument", "python", "-m", "kiro_runtime.main"]
