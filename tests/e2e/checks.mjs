import { spawnSync } from "node:child_process";

const OLLAMA_URL = requiredEnv("AGENTBOX_E2E_OLLAMA_URL");
const OPENAI_BASE_URL = requiredEnv("AGENTBOX_E2E_OPENAI_BASE_URL");
const MODEL = requiredEnv("AGENTBOX_E2E_MODEL");
const PROMPT = "Reply with the single word: pong";
const LMSTUDIO_BASE_URL = requiredEnv("AGENTBOX_E2E_LMSTUDIO_BASE_URL");
const LMSTUDIO_MODEL = requiredEnv("AGENTBOX_E2E_LMSTUDIO_MODEL");

const results = [];

function requiredEnv(name) {
  const value = process.env[name];
  if (!value) {
    console.error(`missing environment variable ${name}`);
    process.exit(2);
  }
  return value;
}

async function check(name, fn) {
  const started = Date.now();
  try {
    const detail = await fn();
    results.push({ name, state: "PASS", detail, seconds: elapsed(started) });
  } catch (error) {
    results.push({
      name,
      state: "FAIL",
      detail: String(error.message ?? error),
      seconds: elapsed(started),
    });
  }
}

function elapsed(started) {
  return ((Date.now() - started) / 1000).toFixed(1);
}

function run(command, args, options) {
  const result = spawnSync(command, args, {
    encoding: "utf8",
    timeout: options.timeoutMs,
    env: { ...process.env, ...options.env },
  });
  return {
    status: result.status,
    stdout: (result.stdout ?? "").trim(),
    stderr: (result.stderr ?? "").trim(),
  };
}

async function waitForOllama() {
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${OLLAMA_URL}/api/tags`);
      if (response.ok) return "reachable";
    } catch {
      // server not listening yet
    }
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  throw new Error(`${OLLAMA_URL} did not become reachable within 120s`);
}

async function pullModel() {
  const response = await fetch(`${OLLAMA_URL}/api/pull`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ model: MODEL, stream: false }),
  });
  if (!response.ok) throw new Error(`pull failed: HTTP ${response.status}`);
  const body = await response.json();
  if (body.status !== "success") throw new Error(`pull reported ${JSON.stringify(body)}`);
  return `${MODEL} pulled`;
}

async function nativeGenerate() {
  const response = await fetch(`${OLLAMA_URL}/api/generate`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ model: MODEL, prompt: PROMPT, stream: false }),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const body = await response.json();
  if (!body.response) throw new Error("empty response field");
  return firstLine(body.response);
}

async function openAiCompatibleChat() {
  const response = await fetch(`${OPENAI_BASE_URL}/chat/completions`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: "Bearer local" },
    body: JSON.stringify({ model: MODEL, messages: [{ role: "user", content: PROMPT }] }),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const body = await response.json();
  const content = body.choices?.[0]?.message?.content;
  if (!content) throw new Error(`no choices in ${JSON.stringify(body).slice(0, 200)}`);
  return firstLine(content);
}

function firstLine(text) {
  return text.trim().split("\n")[0].slice(0, 80);
}

function lastLine(text) {
  const lines = text.trim().split("\n").filter((line) => line.trim());
  return (lines[lines.length - 1] ?? "").slice(0, 80);
}

async function codexInstalled() {
  const result = run("codex", ["--version"], { timeoutMs: 60_000 });
  if (result.status !== 0) throw new Error(result.stderr || `exit ${result.status}`);
  return result.stdout;
}

function codexProviderArgs(baseUrl) {
  return [
    "-c",
    "model_provider=agentbox",
    "-c",
    "model_providers.agentbox.name=agentbox",
    "-c",
    `model_providers.agentbox.base_url=${baseUrl}`,
    "-c",
    "model_providers.agentbox.wire_api=responses",
    "-c",
    "model_providers.agentbox.requires_openai_auth=false",
  ];
}

function codexExec(baseUrl) {
  return run(
    "codex",
    [
      "exec",
      "--skip-git-repo-check",
      "--sandbox",
      "read-only",
      ...codexProviderArgs(baseUrl),
      "-m",
      MODEL,
      PROMPT,
    ],
    { timeoutMs: 300_000, env: { OPENAI_API_KEY: "local" } },
  );
}

async function codexAgainstLocalModel() {
  const result = codexExec(OPENAI_BASE_URL);
  if (result.status !== 0) throw new Error(tail(result.stderr || result.stdout));
  return lastLine(result.stdout);
}

async function piInstalled() {
  const result = run("omp", ["--version"], { timeoutMs: 60_000 });
  if (result.status !== 0) throw new Error(tail(result.stderr || result.stdout));
  return result.stdout;
}

async function piAgainstLocalModel() {
  const env = { OLLAMA_BASE_URL: OLLAMA_URL, OLLAMA_HOST: OLLAMA_URL };
  const selector = `ollama/${MODEL.split(":")[0]}`;
  const result = run("omp", ["--print", "--no-session", "--model", selector, PROMPT], {
    timeoutMs: 300_000,
    env,
  });
  if (result.status !== 0) {
    const catalog = run("omp", ["models", "ollama"], { timeoutMs: 60_000, env });
    throw new Error(`${tail(result.stderr || result.stdout)} || catalog: ${tail(catalog.stdout)}`);
  }
  return lastLine(result.stdout);
}

function tail(text) {
  const lines = text.trim().split("\n");
  return lines.slice(-3).join(" | ").slice(0, 300);
}

async function lmStudioModelAvailable() {
  const response = await fetch(`${LMSTUDIO_BASE_URL}/models`);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const body = await response.json();
  const ids = (body.data ?? []).map((entry) => entry.id);
  if (!ids.includes(LMSTUDIO_MODEL)) {
    throw new Error(`${LMSTUDIO_MODEL} not in ${ids.join(", ")}`);
  }
  return LMSTUDIO_MODEL;
}

async function lmStudioChat() {
  const response = await fetch(`${LMSTUDIO_BASE_URL}/chat/completions`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: "Bearer local" },
    body: JSON.stringify({
      model: LMSTUDIO_MODEL,
      messages: [{ role: "user", content: PROMPT }],
    }),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const body = await response.json();
  const content = body.choices?.[0]?.message?.content;
  if (!content) throw new Error("no choices");
  return firstLine(content);
}

async function piDiscoversLmStudio() {
  const result = run("omp", ["models", "lm-studio"], { timeoutMs: 120_000, env: {} });
  if (result.status !== 0) throw new Error(tail(result.stderr || result.stdout));
  if (!result.stdout.includes(LMSTUDIO_MODEL)) {
    throw new Error(`${LMSTUDIO_MODEL} not listed: ${tail(result.stdout)}`);
  }
  return `catalog lists ${LMSTUDIO_MODEL}`;
}

async function codexAgainstLmStudio() {
  const result = run(
    "codex",
    [
      "exec",
      "--skip-git-repo-check",
      "--sandbox",
      "read-only",
      ...codexProviderArgs(LMSTUDIO_BASE_URL),
      "-m",
      LMSTUDIO_MODEL,
      PROMPT,
    ],
    { timeoutMs: 300_000, env: { OPENAI_API_KEY: "local" } },
  );
  if (result.status !== 0) throw new Error(tail(result.stderr || result.stdout));
  return lastLine(result.stdout);
}

await check("ollama reachable", waitForOllama);
await check("model pull", pullModel);
await check("ollama native generate", nativeGenerate);
await check("openai-compatible chat", openAiCompatibleChat);
await check("codex installed", codexInstalled);
await check("codex against local model", codexAgainstLocalModel);
await check("pi installed", piInstalled);
await check("pi against local model", piAgainstLocalModel);
await check("lm studio model available", lmStudioModelAvailable);
await check("lm studio chat", lmStudioChat);
await check("pi discovers lm studio", piDiscoversLmStudio);
await check("codex against lm studio", codexAgainstLmStudio);

console.log("");
for (const result of results) {
  console.log(`${result.state}  ${result.name}  (${result.seconds}s)  ${result.detail}`);
}

const failed = results.filter((result) => result.state === "FAIL");
const passed = results.filter((result) => result.state === "PASS");
console.log(`\n${passed.length} passed, ${failed.length} failed`);
process.exit(failed.length === 0 ? 0 : 1);
