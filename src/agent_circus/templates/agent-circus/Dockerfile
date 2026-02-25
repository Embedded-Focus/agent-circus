FROM node:24-trixie AS base

ARG TZ
ENV TZ="$TZ"

ARG MCP_INSPECTOR_VERSION=0.18.0

# Install basic development tools and iptables/ipset
RUN apt-get update && apt-get install -y --no-install-recommends \
  ca-certificates \
  curl \
  less \
  git \
  procps \
  sudo \
  fzf \
  zsh \
  man-db \
  unzip \
  gnupg2 \
  gh \
  iptables \
  ipset \
  iproute2 \
  dnsutils \
  aggregate \
  jq \
  nano \
  vim \
  wget \
  locales \
  rsync \
  ripgrep \
  strace \
  && sed -i 's/# en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen \
  && locale-gen \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/* \
  && update-ca-certificates

ENV LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8

# Ensure default node user has access to /usr/local/share
RUN mkdir -p /usr/local/share/npm-global && \
  chown -R node:node /usr/local/share

ARG USERNAME=node

# Persist bash history.
RUN SNIPPET="export PROMPT_COMMAND='history -a' && export HISTFILE=/commandhistory/.bash_history" \
  && mkdir /commandhistory \
  && touch /commandhistory/.bash_history \
  && chown -R $USERNAME /commandhistory

# Set `DEVCONTAINER` environment variable to help with orientation
ENV DEVCONTAINER=true

# Create workspace and config directories and set permissions
RUN mkdir -p /workspace /home/node/.claude && \
  chown -R node:node /workspace /home/node/.claude

WORKDIR /workspace

ARG GIT_DELTA_VERSION
RUN ARCH=$(dpkg --print-architecture) && \
  wget "https://github.com/dandavison/delta/releases/download/${GIT_DELTA_VERSION}/git-delta_${GIT_DELTA_VERSION}_${ARCH}.deb" && \
  sudo dpkg -i "git-delta_${GIT_DELTA_VERSION}_${ARCH}.deb" && \
  rm "git-delta_${GIT_DELTA_VERSION}_${ARCH}.deb"

# Set up non-root user
USER node

# Install global packages
ENV NPM_CONFIG_PREFIX=/usr/local/share/npm-global
ENV UV_PROJECT_ENVIRONMENT=/usr/local/share/venv
ENV UV_LINK_MODE=copy
ENV PATH=${PATH}:${NPM_CONFIG_PREFIX}/bin:${UV_PROJECT_ENVIRONMENT}/bin

# Set the default shell to zsh rather than sh
ENV SHELL=/bin/zsh

# Set the default editor and visual
ENV EDITOR=nano
ENV VISUAL=nano

# Default powerline10k theme
ARG ZSH_IN_DOCKER_VERSION
RUN sh -c "$(wget -O- https://github.com/deluan/zsh-in-docker/releases/download/v${ZSH_IN_DOCKER_VERSION}/zsh-in-docker.sh)" -- \
  -p git \
  -p fzf \
  -a "source /usr/share/doc/fzf/examples/key-bindings.zsh" \
  -a "source /usr/share/doc/fzf/examples/completion.zsh" \
  -a "export PROMPT_COMMAND='history -a' && export HISTFILE=/commandhistory/.bash_history" \
  -x

# Copy and set up firewall script
COPY init-firewall.sh /usr/local/bin/
COPY docker-entrypoint.sh /
USER root
RUN chmod +x /usr/local/bin/init-firewall.sh && \
  { \
    echo 'Defaults env_keep += "AGENT_TYPE"'; \
    echo 'Defaults secure_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"'; \
    echo 'node ALL=(root) NOPASSWD: /usr/local/bin/init-firewall.sh'; \
  } > /etc/sudoers.d/node-firewall && \
  chmod 0440 /etc/sudoers.d/node-firewall

# install additional tools
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
RUN curl -L https://github.com/mikefarah/yq/releases/download/v4.50.1/yq_linux_amd64 -o /usr/local/bin/yq \
    && chmod a+x /usr/local/bin/yq

USER node

RUN npm install -g \
    @modelcontextprotocol/inspector@${MCP_INSPECTOR_VERSION}

ENTRYPOINT [ "/docker-entrypoint.sh" ]
CMD [ "sleep", "infinity" ]

FROM base AS claude-code

ARG CLAUDE_CODE_VERSION
ARG CLAUDE_CODE_ACP_VERSION

RUN npm install -g \
    @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
    @zed-industries/claude-code-acp@${CLAUDE_CODE_ACP_VERSION}

FROM base AS codex

ARG CODEX_VERSION
ARG CODEX_ACP_VERSION

RUN npm install -g \
    @openai/codex@${CODEX_VERSION} \
    @zed-industries/codex-acp@${CODEX_ACP_VERSION}

FROM base AS mistral-vibe

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app \
    PATH=/app/bin:${PATH}

USER root

RUN --mount=type=cache,target=/root/.cache \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync \
        --locked \
        --no-dev \
        --no-install-project

ENV UV_PYTHON=/app/bin/python

USER node
