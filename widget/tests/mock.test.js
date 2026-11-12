// Tests for mock mode (dsu-chat-mock.js). Run with: node --test widget/tests/
//
// The mock file runs in a sandbox whose real fetch, XMLHttpRequest, WebSocket, and sendBeacon
// only record calls. Every state is asked for; none of them may reach the network.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const WIDGET = path.join(__dirname, "..");
const MOCK_SOURCE = fs.readFileSync(path.join(WIDGET, "dsu-chat-mock.js"), "utf8");

function loadMock(search) {
  const network = [];
  const record = (kind) =>
    function (...args) {
      network.push([kind, ...args]);
      throw new Error(`network request in mock mode: ${kind}`);
    };
  const window = {
    location: { search },
    fetch: record("fetch"),
    XMLHttpRequest: record("XMLHttpRequest"),
    WebSocket: record("WebSocket"),
    navigator: { sendBeacon: record("sendBeacon") },
  };
  const context = {
    window,
    fetch: window.fetch,
    XMLHttpRequest: window.XMLHttpRequest,
    WebSocket: window.WebSocket,
    navigator: window.navigator,
    URLSearchParams,
    Response,
    ReadableStream,
    TextEncoder,
    TypeError,
    JSON,
    String,
    Promise,
    setTimeout: (fn) => fn(), // no waiting in tests
    console: { info() {} },
  };
  vm.runInNewContext(MOCK_SOURCE, context);
  return { window, network, realFetch: context.fetch };
}

const ask = (window, question, url = "http://localhost:8000/chat") =>
  window.fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });

test("mock mode never makes a network request", async () => {
  const { window, network } = loadMock("?mock=1");
  const questions = [
    "Where is DSU? (short)",
    "How can I get a meal plan? (long answer)",
    "What's my GPA? (personal)",
    "Write me a poem (off topic)",
    "Where is DSU? (slow)",
    "Which meal plans are there? (new message)",
    "Where is DSU? (503)",
    "Where is DSU? (rate limit)",
    "Where is DSU? (network)",
    "Where is DSU? (too long)",
    "Meal plans (streaming)",
    "Meal plans (stream error)",
    "Where is DSU? (no stream)",
    "anything else",
  ];
  for (const question of questions) {
    for (const url of ["http://localhost:8000/chat", "http://localhost:8000/chat/stream"]) {
      const response = await ask(window, question, url).catch(() => null); // network rejects
      await response?.text(); // read every streamed body to the end
    }
  }
  await ask(window, "short", "https://example.com/other").catch(() => {});
  assert.deepEqual(network, []);
});

test("each state gives the reply the widget expects", async () => {
  const { window } = loadMock("?mock=1");

  const short = await ask(window, "Where is DSU? (short)");
  assert.equal(short.status, 200);
  const shortBody = await short.json();
  assert.match(shortBody.answer, /1200 N\. DuPont Highway/);
  assert.equal(shortBody.sources.length, 1);

  const long = await (await ask(window, "meal plan (long answer)")).json();
  assert.match(long.answer, /\n- /);
  assert.ok(long.sources.length >= 2);

  for (const question of ["What's my GPA? (personal)", "Write me a poem (off topic)"]) {
    const body = await (await ask(window, question)).json();
    assert.deepEqual(body.sources, []);
  }

  const slow = await ask(window, "Where is DSU? (slow)");
  assert.equal(slow.status, 200);

  // "New message": the long answer (Show more, Sources chip), slow enough to scroll up.
  const newMessage = await ask(window, "Which meal plans are there? (new message)");
  assert.equal(newMessage.status, 200);
  assert.deepEqual(await newMessage.json(), long);

  const unavailable = await ask(window, "(503)");
  assert.equal(unavailable.status, 503);

  const tooLong = await ask(window, "(too long)");
  assert.equal(tooLong.status, 422);
  assert.equal(typeof (await tooLong.json()).detail, "string");

  await assert.rejects(ask(window, "(network)"), TypeError);
});

test("the rate-limited state is a 429 the widget shows as the API's message, offline", async () => {
  const { errorMessage } = require("../dsu-chat.js");
  const { window, network } = loadMock("?mock=1");
  for (const question of ["Where is DSU? (rate limit)", "(429)"]) {
    const response = await ask(window, question);
    assert.equal(response.status, 429);
    assert.equal(response.headers.get("Retry-After"), "45");
    const body = await response.json();
    assert.match(body.detail, /wait a minute/);
    assert.equal(errorMessage(response.status, body), body.detail);
  }
  assert.deepEqual(network, []);
});

test("thumbs up/down in mock mode succeed with no network request", async () => {
  const { feedbackRequest } = require("../dsu-chat.js");
  const { window, network } = loadMock("?mock=1");
  for (const rating of ["up", "down"]) {
    const response = await window.fetch(
      ...feedbackRequest("http://localhost:8000", "mock-short", rating)
    );
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { request_id: "mock-short", rating });
  }
  await window.fetch("http://localhost:8000/feedback", { method: "POST", body: "not json" });
  assert.deepEqual(network, []);
});

test("every mock answer has a request ID, so it gets thumbs buttons", async () => {
  const { window } = loadMock("?mock=1");
  for (const question of ["short", "long answer", "personal", "off topic", "slow", "new message"]) {
    const body = await (await ask(window, question)).json();
    assert.equal(typeof body.request_id, "string", question);
  }
});

test("without ?mock=1 the mock file changes nothing", () => {
  for (const search of ["", "?mock=0", "?mock=true", "?q=mock=1"]) {
    const { window, realFetch } = loadMock(search);
    assert.equal(window.fetch, realFetch, `fetch replaced for "${search}"`);
    assert.equal(window.DSU_CHAT_MOCK, undefined);
  }
});

test("the embed code can't turn on mock mode", () => {
  // dsu-chat.js is what sites embed. It must not know about mock mode at all.
  const widget = fs.readFileSync(path.join(WIDGET, "dsu-chat.js"), "utf8");
  assert.doesNotMatch(widget, /mock/i);
  // demo.html loads the mock file from its own tag, not through the widget's script tag.
  const demo = fs.readFileSync(path.join(WIDGET, "demo.html"), "utf8");
  const widgetTag = demo.match(/<script[^>]*src="dsu-chat\.js"[^>]*>/)[0];
  assert.doesNotMatch(widgetTag, /mock/i);
});

// Streaming in mock mode -------------------------------------------------------------------------

const STREAM = "http://localhost:8000/chat/stream";

/** Asks like the widget does (stream first, /chat as the fallback) and collects what it shows. */
async function widgetAsks(window, question) {
  const { requestAnswer } = require("../dsu-chat.js");
  const texts = [];
  const result = await requestAnswer(
    (url, init) => window.fetch(url, init),
    "http://localhost:8000",
    { question, history: [] },
    { onText: (t) => texts.push(t) }
  );
  return { result, texts };
}

test("every answer streams in pieces, then ends with the same answer /chat gives", async () => {
  const { window, network } = loadMock("?mock=1");
  for (const question of ["short", "long answer", "personal", "off topic", "slow", "streaming"]) {
    const regular = await (await ask(window, question)).json();
    const { result, texts } = await widgetAsks(window, question);
    assert.ok(texts.length > 3, `${question}: streamed in pieces`);
    assert.equal(texts.at(-1), regular.answer, question);
    assert.deepEqual(result, { status: 200, ok: true, data: { ...regular, outcome: "answered" } });
  }
  assert.deepEqual(network, []);
});

test("the streaming state is a real event stream, slow enough to watch", async () => {
  const { window } = loadMock("?mock=1");
  const response = await ask(window, "meal plans (streaming)", STREAM);
  assert.equal(response.headers.get("Content-Type"), "text/event-stream");
  const stepMs = Number(MOCK_SOURCE.match(/name: "streaming",[\s\S]*?stepMs: (\d+)/)[1]);
  assert.ok(stepMs >= 100); // the pause between pieces
});

test("the stream error state breaks off with the error event, offline", async () => {
  const { window, network } = loadMock("?mock=1");
  const { result, texts } = await widgetAsks(window, "meal plans (stream error)");
  assert.ok(texts.length > 0);
  assert.equal(result.ok, false);
  assert.match(result.data.detail, /can't answer right now/);
  assert.deepEqual(network, []);
});

test("the no stream state falls back to /chat, offline", async () => {
  const { window, network } = loadMock("?mock=1");
  const { result, texts } = await widgetAsks(window, "Where is DSU? (no stream)");
  assert.deepEqual(texts, []);
  assert.equal(result.ok, true);
  assert.match(result.data.answer, /1200 N\. DuPont Highway/);
  assert.deepEqual(network, []);
});

test("errors and the network state behave the same when streaming, offline", async () => {
  const { window, network } = loadMock("?mock=1");
  for (const [question, status] of [["(rate limit)", 429], ["(503)", 503], ["(too long)", 422]]) {
    const { result } = await widgetAsks(window, question);
    assert.equal(result.status, status);
    assert.equal(typeof result.data.detail, "string");
  }
  await assert.rejects(widgetAsks(window, "(network)"), TypeError);
  assert.deepEqual(network, []);
});
