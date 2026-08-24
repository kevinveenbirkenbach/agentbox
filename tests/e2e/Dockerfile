FROM docker.io/library/node:22-bookworm

ENV BUN_INSTALL=/usr/local/bun
ENV PATH=$BUN_INSTALL/bin:$PATH

RUN curl -fsSL https://bun.sh/install | bash

RUN npm install -g @openai/codex

RUN bun install -g @oh-my-pi/pi-coding-agent

WORKDIR /work
COPY checks.mjs /work/checks.mjs

CMD ["node", "/work/checks.mjs"]
