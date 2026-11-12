/*
 * Mock mode for the demo page: saved /chat replies, so the widget can be styled with no backend
 * and no API key. Open demo.html?mock=1 (see widget/README.md).
 *
 * This file is only loaded by demo.html. It is not part of the embed code, and dsu-chat.js knows
 * nothing about it. It does nothing unless the page URL has ?mock=1. When it runs, it replaces
 * window.fetch so every request gets a saved reply and nothing goes over the network.
 *
 * The answers are real /chat output from an eval run (eval/results, Claude Sonnet). Thumbs
 * up/down (POST /feedback) always succeed here, and the rating is kept nowhere.
 *
 * The widget asks POST /chat/stream first. Here every answer streams a few words at a time, as
 * server-sent events, after the state's delay; errors (429, 503, 422) are the same JSON as /chat.
 * Three states are about streaming itself: "streaming" (slow, to watch it or screenshot it mid-way),
 * "stream error" (breaks off with an error event), and "no stream" (the stream endpoint is missing,
 * so the widget falls back to POST /chat).
 */
(function () {
  "use strict";

  const UNAVAILABLE =
    "Sorry, I can't answer right now. Please try again in a few minutes, or visit desu.edu.";
  const TOO_FAST =
    "You're sending questions faster than I can answer. Please wait a minute and try again.";

  // Each state: the words that pick it (checked in this order), how long the reply takes, and
  // the reply. `reply: null` means a network error. A question with none of the words gets "short".
  const STATES = [
    {
      name: "stream error",
      words: ["stream error", "breaks off"],
      status: 200,
      reply: "long",
      breakAfter: 30, // words; then an error event, as when Claude fails mid-answer
    },
    {
      name: "no stream",
      words: ["no stream", "fallback"],
      status: 200,
      reply: "short",
      noStream: true, // /chat/stream answers 404, like an API without streaming
    },
    {
      name: "streaming",
      words: ["stream"],
      status: 200,
      reply: "long",
      stepMs: 150, // slow enough to watch the words arrive
    },
    {
      name: "new message",
      words: ["new message"],
      delayMs: 2500, // time to scroll up, so the answer shows the "New message" button
      status: 200,
      reply: "long",
    },
    {
      name: "slow",
      words: ["slow", "loading"],
      delayMs: 5000,
      status: 200,
      reply: "short", // the short answer, after a long wait
    },
    {
      name: "long",
      words: ["long answer", "meal"],
      status: 200,
      reply: {
        answer:
          "I couldn't find step-by-step sign-up instructions on the DSU pages I have, but here is what they say about choosing a plan.\n\n" +
          "**Who needs one**\n" +
          "- Students in traditional residence halls, University Courtyard Apartments, and University Village Apartments must choose a meal plan.\n" +
          "- Residential freshmen are required to have the 19 Meal plan.\n" +
          "- Commuters and off-campus students can choose one of the plans reserved for them (see below).\n\n" +
          "**Options and prices (per semester)**\n" +
          "- Resident plans:\n" +
          "  - 19 Meal ($200 Flex): $2,833.50\n" +
          "  - 200 Block ($225 Flex): $2,774.50\n" +
          "  - 125 Block ($150 Flex): $2,607.00\n" +
          "  - 75 Block Courtyard ($150 Flex): $949.50\n" +
          "- Off-campus and commuter plans (also open to faculty and staff):\n" +
          "  - Hornet Dollars: $1,153.50\n" +
          "  - 75 Block PLUS ($150 Flex): $949.50\n" +
          "  - 50 Block PLUS ($150 Flex): $708.50\n\n" +
          "Flex dollars come with every plan and can be refilled any time during the semester. Prices are subject to change, and the yearly totals don't include summer sessions.\n\n" +
          "**Next steps**\n" +
          "See the [Meal Plan Options page](https://desudining.sodexomyway.com/en-us/meal-plan/meal-plan-options) or the [Dining Services website](https://desudining.sodexomyway.com/en-us/). You can also contact DSU Dining Services to ask how to enroll or change your plan. More details are on the [Food Service-Dining page](https://www.desu.edu/student-life/housing-dining/food-service-dining) and the [Tuition & Fees page](https://www.desu.edu/admissions/financial-aid/tuition-fees).",
        sources: [
          {
            title: "Food Service-Dining",
            heading_path: "Food Service-Dining",
            url: "https://www.desu.edu/student-life/housing-dining/food-service-dining",
            last_updated: "2026-08-31",
          },
          {
            title: "Tuition & Fees",
            heading_path: "Tuition & Fees > Fees for 2026-2027 Academic Year",
            url: "https://www.desu.edu/admissions/financial-aid/tuition-fees",
            last_updated: "2026-09-08",
          },
        ],
        request_id: "mock-long",
      },
    },
    {
      name: "personal",
      words: ["personal", "gpa", "my "],
      status: 200,
      reply: {
        answer:
          "I can't see your records, so I can't tell you your GPA. You can check it yourself in DegreeWorks, which shows your degree progress and GPA. Banner Self-Service also has your grades.\n\n" +
          "If something looks wrong or you have questions about your GPA, the Registrar's Office or your academic advisor can help. You can find your advisor in Navigate.",
        sources: [],
        request_id: "mock-personal",
      },
    },
    {
      name: "off-topic",
      words: ["off-topic", "off topic", "poem"],
      status: 200,
      reply: {
        answer:
          "I can only help with questions about Delaware State University, so a pizza poem is outside what I can do. I'm happy to help with DSU topics like majors and programs, admissions, housing, or dining. For example, I can point you to DSU's list of majors and minors: https://www.desu.edu/academics/majors-programs",
        sources: [],
        request_id: "mock-off-topic",
      },
    },
    {
      name: "rate limited",
      words: ["rate limit", "429"],
      status: 429, // what the API says when one client asks too often
      headers: { "Retry-After": "45" },
      reply: { detail: TOO_FAST, request_id: "mock-429" },
    },
    {
      name: "503",
      words: ["503", "unavailable"],
      status: 503,
      reply: { detail: UNAVAILABLE, request_id: "mock-503" },
    },
    {
      name: "network",
      words: ["network", "offline"],
      status: 0,
      reply: null,
    },
    {
      name: "too long",
      words: ["too long"],
      status: 422, // what the API says when its limit is lower than the widget's
      reply: { detail: "Please keep your question under 1000 characters." },
    },
    {
      name: "short",
      words: ["short"],
      status: 200,
      reply: {
        answer:
          "Delaware State University is at:\n\n1200 N. DuPont Highway\nDover, DE 19901\n\n" +
          "You can reach the main line at 302.857.6060 during the day, or 302.857.6290 in the evening.\n\n" +
          "More contact details are on the [Campus Contacts page](https://www.desu.edu/about/campus-contacts).",
        sources: [
          {
            title: "Campus Contacts",
            heading_path: "Campus Contacts",
            url: "https://www.desu.edu/about/campus-contacts",
            last_updated: "2026-08-12",
          },
        ],
        request_id: "mock-short",
      },
    },
  ];

  const DEFAULT_DELAY_MS = 800; // long enough to see the typing indicator
  const STEP_MS = 40; // between streamed pieces
  const WORDS_PER_PIECE = 2;

  function pickState(question) {
    const q = String(question).toLowerCase();
    return (
      STATES.find((s) => s.words.some((w) => q.includes(w))) ||
      STATES.find((s) => s.name === "short")
    );
  }

  function questionFrom(init) {
    try {
      return JSON.parse(init && init.body).question || "";
    } catch {
      return "";
    }
  }

  /** The saved reply to POST /feedback: the rating is accepted, and kept nowhere. */
  function feedbackReply(init) {
    let body = {};
    try {
      body = JSON.parse(init && init.body) || {};
    } catch {}
    const reply = { request_id: body.request_id, rating: body.rating };
    const headers = { "Content-Type": "application/json" };
    return new Promise((resolve) => {
      setTimeout(() => resolve(new Response(JSON.stringify(reply), { status: 200, headers })), 300);
    });
  }

  function mockFetch(input, init) {
    const url = String(input && input.url ? input.url : input);
    if (/\/feedback$/.test(url)) return feedbackReply(init);
    let state = pickState(questionFrom(init));
    const streams = /\/chat\/stream$/.test(url);
    if (!streams && !/\/chat$/.test(url)) state = STATES.find((s) => s.name === "network"); // never go out
    const reply =
      typeof state.reply === "string" ? STATES.find((s) => s.name === state.reply).reply : state.reply;
    const status = typeof state.reply === "string" ? 200 : state.status;

    return new Promise((resolve, reject) => {
      setTimeout(() => {
        if (reply === null) {
          reject(new TypeError("Failed to fetch (mock network error)"));
        } else if (streams && state.noStream) {
          const headers = { "Content-Type": "application/json" };
          resolve(new Response(JSON.stringify({ detail: "Not Found" }), { status: 404, headers }));
        } else if (streams && status === 200) {
          resolve(streamReply(reply, state));
        } else {
          const headers = { "Content-Type": "application/json", ...state.headers };
          resolve(new Response(JSON.stringify(reply), { status, headers }));
        }
      }, state.delayMs ?? DEFAULT_DELAY_MS);
    });
  }

  /** The reply as /chat/stream sends it: a few words per "delta" event, then "done" (or "error"
   * after state.breakAfter words). */
  function streamReply(reply, state) {
    const event = (name, data) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`;
    const words = reply.answer.match(/\s*\S+/g) || [];
    const events = [];
    const end = state.breakAfter ? Math.min(state.breakAfter, words.length) : words.length;
    for (let i = 0; i < end; i += WORDS_PER_PIECE) {
      events.push(event("delta", { text: words.slice(i, i + WORDS_PER_PIECE).join("") }));
    }
    events.push(
      state.breakAfter
        ? event("error", { detail: UNAVAILABLE, request_id: "mock-stream-error" })
        : event("done", { ...reply, outcome: "answered" })
    );
    const encoder = new TextEncoder();
    let cancelled = false; // the widget stops reading once the last event arrives
    const body = new ReadableStream({
      start(controller) {
        const next = (i) => {
          if (cancelled) return;
          if (i === events.length) return controller.close();
          controller.enqueue(encoder.encode(events[i]));
          setTimeout(() => next(i + 1), state.stepMs ?? STEP_MS);
        };
        next(0);
      },
      cancel() {
        cancelled = true;
      },
    });
    return new Response(body, {
      status: 200,
      headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
    });
  }

  if (new URLSearchParams(window.location.search).get("mock") === "1") {
    window.fetch = mockFetch;
    window.DSU_CHAT_MOCK = { states: STATES.map((s) => ({ name: s.name, words: s.words })) };
    console.info("DSU chat: mock mode. Replies are saved examples; nothing is sent anywhere.");
  }
})();
