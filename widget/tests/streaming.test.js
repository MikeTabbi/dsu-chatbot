// Tests for streamed answers (POST /chat/stream) and the fallback to POST /chat.
// Run with: node --test widget/tests/
//
// The fetch here is fake: it answers each URL from a table and records every call. Streamed
// bodies arrive in the pieces the test gives, so events can be split anywhere.

const assert = require("node:assert/strict");
const { test } = require("node:test");
const { sseParser, requestAnswer, renderMarkdown, isSafeUrl, STREAM_FALLBACK_STATUSES } = require(
  "../dsu-chat.js"
);

const API = "https://api.example.edu";
const PAYLOAD = { question: "Where is DSU?", history: [] };
const DONE = {
  answer: "DSU is in **Dover**.",
  sources: [{ title: "Campus Contacts", url: "https://www.desu.edu/about", last_updated: null }],
  request_id: "r1",
  outcome: "answered",
};

const event = (name, data) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`;

/** A streamed response whose body arrives in these pieces of text. */
function sse(pieces, { status = 200, type = "text/event-stream", fail = false } = {}) {
  const encoder = new TextEncoder();
  const body = new ReadableStream({
    start(controller) {
      for (const piece of pieces) controller.enqueue(encoder.encode(piece));
      if (fail) controller.error(new TypeError("network dropped"));
      else controller.close();
    },
  });
  return new Response(body, { status, headers: { "Content-Type": type } });
}

const json = (data, status = 200, headers = {}) =>
  new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json", ...headers } });

/** A fake fetch: `routes` maps "/chat" and "/chat/stream" to a response, or a function making one. */
function fakeFetch(routes) {
  const calls = [];
  const fetchFn = async (url, init) => {
    const path = url.slice(API.length);
    calls.push({ path, body: JSON.parse(init.body), signal: init.signal });
    const route = routes[path];
    if (route === undefined) throw new TypeError(`unexpected request to ${path}`);
    const response = typeof route === "function" ? route() : route;
    if (response instanceof Error) throw response;
    return response;
  };
  return { fetchFn, calls };
}

async function ask(routes) {
  const { fetchFn, calls } = fakeFetch(routes);
  const texts = [];
  let restarts = 0;
  const result = await requestAnswer(fetchFn, API, PAYLOAD, {
    onText: (t) => texts.push(t),
    onRestart: () => restarts++,
  });
  return { result, calls, texts, restarts, paths: calls.map((c) => c.path) };
}

// The event parser ----------------------------------------------------------------------------

test("events split anywhere are put back together", () => {
  const text = event("delta", { text: "Hi" }) + ": a comment\n\n" + event("done", DONE);
  for (let size = 1; size <= text.length; size++) {
    const parser = sseParser();
    const got = [];
    for (let i = 0; i < text.length; i += size) got.push(...parser.push(text.slice(i, i + size)));
    got.push(...parser.end());
    assert.deepEqual(
      got.map((e) => [e.event, JSON.parse(e.data)]),
      [
        ["delta", { text: "Hi" }],
        ["done", DONE],
      ],
      `pieces of ${size}`
    );
  }
});

test("CRLF line ends, multi-line data, and a last event with no blank line", () => {
  const parser = sseParser();
  const got = parser.push("event: delta\r\ndata: a\r\ndata: b\r\n\r\nevent: done\ndata:x");
  assert.deepEqual(got, [{ event: "delta", data: "a\nb" }]);
  assert.deepEqual(parser.end(), [{ event: "done", data: "x" }]);
});

// Streaming, and when it falls back to /chat --------------------------------------------------

test("a streamed answer gives the text so far, then the final event as the answer", async () => {
  const stream = sse([event("delta", { text: "DSU is " }), event("delta", { text: "in **Do" }), event("delta", { text: "ver**." }) + event("done", DONE)]);
  const { result, texts, paths, restarts } = await ask({ "/chat/stream": stream });
  assert.deepEqual(texts, ["DSU is ", "DSU is in **Do", "DSU is in **Dover**."]);
  assert.deepEqual(result, { status: 200, ok: true, data: DONE });
  assert.deepEqual(paths, ["/chat/stream"]); // /chat isn't asked too
  assert.equal(restarts, 0);
});

test("the question and history are sent to the stream as they would be to /chat", async () => {
  const { calls } = await ask({ "/chat/stream": () => sse([event("done", DONE)]) });
  assert.deepEqual(calls[0].body, PAYLOAD);
});

test("an error event is shown as the API's message, not retried on /chat", async () => {
  const detail = "Sorry, I can't answer right now.";
  const stream = sse([event("delta", { text: "DSU is" }), event("error", { detail, request_id: "r2" })]);
  const { result, paths } = await ask({ "/chat/stream": stream, "/chat": json(DONE) });
  assert.deepEqual(result, { status: 503, ok: false, data: { detail, request_id: "r2" } });
  assert.deepEqual(paths, ["/chat/stream"]);
});

test("rate limits, a busy API, and bad questions are shown as /chat would show them", async () => {
  for (const status of [422, 429, 503, 413]) {
    const detail = `refused ${status}`;
    const { result, paths } = await ask({
      "/chat/stream": json({ detail }, status, { "Retry-After": "45" }),
      "/chat": json(DONE),
    });
    assert.deepEqual(result, { status, ok: false, data: { detail } });
    assert.deepEqual(paths, ["/chat/stream"], `${status} must not be asked twice`);
  }
});

test("falls back to /chat when streaming fails or isn't there", async () => {
  const cases = {
    "network error": () => new TypeError("Failed to fetch"),
    ...Object.fromEntries(STREAM_FALLBACK_STATUSES.map((s) => [`HTTP ${s}`, () => json({ detail: "x" }, s)])),
    "not a stream (a proxy's 200)": () => json({ hello: "proxy" }),
    "no readable body": () => new Response(null, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
    "stopped before the final event": () => sse([event("delta", { text: "DSU" })]),
    "dropped mid-stream": () => sse([event("delta", { text: "DSU" })], { fail: true }),
  };
  for (const [name, stream] of Object.entries(cases)) {
    const { result, paths, restarts } = await ask({ "/chat/stream": stream, "/chat": () => json(DONE) });
    assert.deepEqual(paths, ["/chat/stream", "/chat"], name);
    assert.deepEqual(result, { status: 200, ok: true, data: DONE }, name);
    assert.equal(restarts, 1, `${name}: streamed text is taken down first`);
  }
});

test("the fallback's own errors come back like /chat's, and a network error throws", async () => {
  const offline = () => new TypeError("Failed to fetch");
  const { result } = await ask({ "/chat/stream": offline, "/chat": json({ detail: "busy" }, 503) });
  assert.deepEqual(result, { status: 503, ok: false, data: { detail: "busy" } });
  await assert.rejects(ask({ "/chat/stream": offline, "/chat": offline }), TypeError);
});

test("one deadline covers the stream and the fallback", async () => {
  const { fetchFn, calls } = fakeFetch({ "/chat/stream": () => new TypeError("x"), "/chat": () => json(DONE) });
  const signal = AbortSignal.timeout(60000);
  await requestAnswer(fetchFn, API, PAYLOAD, { signal });
  assert.ok(calls.every((c) => c.signal === signal));
});

// Streamed text is rendered safely at every step ------------------------------------------------

const ALLOWED_TAGS = new Set(["p", "ul", "ol", "li", "strong", "em", "a", "br", "span"]);

function fakeDocument() {
  const created = [];
  const node = (props) => ({ children: [], appendChild(c) { this.children.push(c); return c; }, ...props });
  return {
    created,
    createDocumentFragment: () => node({ tag: "#fragment" }),
    createTextNode: (text) => ({ text }),
    createElement(tag) {
      const el = node({ tag, attributes: {} });
      el.setAttribute = (name, value) => (el.attributes[name] = value);
      created.push(el);
      return el;
    },
  };
}

test("streamed HTML and javascript: links stay text however the pieces are split", async () => {
  const answer =
    'Hi <img src=x onerror="alert(1)"> <script>alert(2)</script> ' +
    "[click](javascript:alert(document.cookie)) [ok](https://www.desu.edu/a) " +
    '<a href="javascript:alert(3)">x</a> [y](data:text/html,<b>)';
  for (const size of [1, 3, 7, answer.length]) {
    const pieces = [];
    for (let i = 0; i < answer.length; i += size) pieces.push(event("delta", { text: answer.slice(i, i + size) }));
    pieces.push(event("done", { ...DONE, answer }));
    const { fetchFn } = fakeFetch({ "/chat/stream": sse(pieces) });
    // Each update is drawn the way the widget draws it: renderMarkdown of the text so far.
    const docs = [];
    const result = await requestAnswer(fetchFn, API, PAYLOAD, {
      onText: (text) => {
        const doc = fakeDocument();
        renderMarkdown(text, doc);
        docs.push(doc);
      },
    });
    assert.equal(result.data.answer, answer);
    assert.ok(docs.length >= Math.floor(answer.length / size));
    for (const doc of docs) {
      for (const el of doc.created) {
        assert.ok(ALLOWED_TAGS.has(el.tag), `created <${el.tag}>`);
        for (const name of Object.keys(el.attributes)) {
          assert.ok(["href", "target", "rel", "start", "class"].includes(name), `set ${name}`);
        }
        if (el.attributes.href) {
          // Part of the https link while it streams in (the bubble is inert until done), never
          // the javascript: or data: ones.
          assert.ok(isSafeUrl(el.attributes.href), el.attributes.href);
          assert.ok(el.attributes.href.startsWith("https://"), el.attributes.href);
        }
      }
    }
  }
});

test("the widget only ever draws streamed text with renderMarkdown", () => {
  const source = require("node:fs").readFileSync(require("node:path").join(__dirname, "..", "dsu-chat.js"), "utf8");
  const start = source.indexOf("function streamedText(");
  const body = source.slice(start, source.indexOf("\n    }\n", start));
  assert.match(body, /answer\.replaceChildren\(renderMarkdown\(latest, document\)\)/);
  assert.doesNotMatch(body, /innerHTML|outerHTML|insertAdjacentHTML|document\.write|textContent\s*=/);
  assert.match(body, /answer\.inert = true/); // its links can't take focus until it's finished
});
