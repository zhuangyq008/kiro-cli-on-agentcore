# AgentCore Runtime image: Python wrapper + kiro-cli, linux/arm64.
# aarch64 kiro-cli builds need glibc >= 2.39, so the base is Debian trixie (glibc 2.41).
FROM public.ecr.aws/docker/library/python:3.12-slim-trixie

ARG KIRO_CLI_VERSION=2.35.2

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl unzip jq ca-certificates openssh-client \
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

USER agent
WORKDIR /home/agent
ENV PATH=/home/agent/.local/bin:$PATH

# Pinned kiro-cli; the installer verifies the release checksum.
RUN curl -fsSL https://cli.kiro.dev/install -o /tmp/kiro-install.sh \
 && bash /tmp/kiro-install.sh --version "${KIRO_CLI_VERSION}" \
 && rm /tmp/kiro-install.sh \
 && kiro-cli --version

# Loop phase agents.
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
