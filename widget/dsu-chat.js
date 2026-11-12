/*
 * DSU chat widget. Plain JavaScript, no framework, no build step.
 *
 * Embed it on a page with one tag (see widget/README.md):
 *
 *   <script src="https://.../dsu-chat.js" data-api-url="https://api.example.edu" defer></script>
 *
 * It loads dsu-chat.css from the same folder and draws everything inside a Shadow DOM, so the
 * host site's CSS can't break the widget and the widget's CSS can't leak onto the site.
 *
 * SECURITY: answer text comes from an AI model working from web pages, so it is never inserted
 * as HTML. renderMarkdown() builds DOM nodes with createElement/createTextNode only, supports a
 * few formats (bold, italic, lists, links), and makes links only for http/https URLs. Anything
 * else, including HTML tags and javascript: links, shows up as plain text. Never use innerHTML,
 * outerHTML, insertAdjacentHTML, or document.write with answer, source, or user text. A streamed
 * answer is no different: each update redraws the text so far with renderMarkdown.
 */
(function () {
  "use strict";

  const VERSION = "0.11.0"; // bump when you change this file or the CSS (see README)
  const HOST_ID = "dsu-chat-widget";
  const REQUEST_TIMEOUT_MS = 60000;
  const PANEL_CLOSE_MS = 150; // the panel's closing fade in the CSS (.panel.closing)

  const MESSAGES = {
    welcome:
      "Hi! I'm Jada, how can I help you today? You can ask me a question about DSU, like housing, admissions, or registration. " +
      "I remember your last few questions in this conversation, so you can ask a follow-up " +
      "like \"how much does it cost?\"",
    loading: "Looking that up…",
    unavailable:
      "Sorry, I can't answer right now. Please try again in a few minutes, or visit desu.edu.",
    network:
      "I couldn't reach the DSU assistant. Check your internet connection and try again.",
    generic: "Something went wrong. Please try again in a few minutes, or visit desu.edu.",
    rateLimited: "You're sending questions faster than I can answer. Please wait a minute and try again.",
    retry: "Your question is back in the box, so you can send it again.",
    feedbackThanks: "Thanks!",
    feedbackFailed: "Sorry, that didn't send. Please try again.",
    askPlaceholder: "Ask a question about DSU",
    askAgainPlaceholder: "Ask a new question (include the details)",
    // Shown when a past conversation is reopened. {name} is the assistant's name.
    reopened:
      "Earlier messages. {name} answers each question on its own and doesn't see the ones " +
      "above, so include the details again.",
    keepOff:
      "Off: this list is cleared when you close this browser. Turn it on only on your own device.",
    keepOn:
      "On: the last 10 conversations are kept in this browser for 7 days. Anyone who uses this " +
      "browser can see them, so leave this off on shared computers.",
    keepFailed: "This browser won't let me keep conversations, so the setting couldn't change.",
  };

  // Thumbs icons (Material Icons thumb_up / thumb_down, Apache 2.0), drawn by svgIcon().
  const FEEDBACK_BUTTONS = [
    {
      rating: "up",
      label: "Helpful",
      path: "M1 21h4V9H1v12zm22-11c0-1.1-.9-2-2-2h-6.31l.95-4.57.03-.32c0-.41-.17-.79-.44-1.06L14.17 1 7.59 7.59C7.22 7.95 7 8.45 7 9v10c0 1.1.9 2 2 2h9c.83 0 1.54-.5 1.84-1.22l3.02-7.05c.09-.23.14-.47.14-.73v-2z",
    },
    {
      rating: "down",
      label: "Not helpful",
      path: "M15 3H6c-.83 0-1.54.5-1.84 1.22l-3.02 7.05c-.09.23-.14.47-.14.73v2c0 1.1.9 2 2 2h6.31l-.95 4.57-.03.32c0 .41.17.79.44 1.06L9.83 23l6.59-6.59c.36-.36.58-.86.58-1.41V5c0-1.1-.9-2-2-2zm4 0v12h4V3h-4z",
    },
  ];

  // Follow-ups: /chat gets the last few question-and-answer pairs of the conversation on screen,
  // so "that page" can be understood. Keep HISTORY_TURNS equal to the backend's
  // CHAT_HISTORY_TURNS; long answers are cut so the request stays small (MAX_REQUEST_BYTES).
  const HISTORY_TURNS = 3;
  const HISTORY_ANSWER_CHARS = 1500;

  /**
   * The history to send with the next question: the last `turns` answered questions of this
   * conversation's saved messages, oldest first, as [{ question, answer }]. A question without an
   * answer after it (it failed) isn't a turn.
   */
  function recentTurns(messages, turns = HISTORY_TURNS, maxAnswer = HISTORY_ANSWER_CHARS) {
    const pairs = [];
    const list = Array.isArray(messages) ? messages : [];
    for (let i = 0; i + 1 < list.length; i++) {
      const asked = list[i];
      const answered = list[i + 1];
      if (asked?.role !== "user" || typeof asked.text !== "string") continue;
      if (answered?.role !== "bot" || typeof answered.data?.answer !== "string") continue;
      const answer = answered.data.answer;
      pairs.push({
        question: asked.text,
        answer: answer.length > maxAnswer ? answer.slice(0, maxAnswer) : answer,
      });
      i++;
    }
    return turns > 0 ? pairs.slice(-turns) : [];
  }

  /** The fetch() arguments that send a thumbs up or down for one answer to POST /feedback. */
  function feedbackRequest(apiUrl, requestId, rating) {
    return [
      `${apiUrl}/feedback`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: requestId, rating }),
        signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      },
    ];
  }

  // ---------------------------------------------------------------------------------------------
  // Streaming: POST /chat/stream sends the answer as server-sent events while it's written
  // ("delta" {text}, then "done" {answer, sources, request_id, outcome} or "error" {detail}).
  // Streamed text is only ever rendered with renderMarkdown, like any other answer.
  // ---------------------------------------------------------------------------------------------

  // Answers from the stream endpoint that mean "no streaming here" (an older API, or a proxy that
  // can't pass the stream through): ask POST /chat instead. Other errors (422, 429, 503) are the
  // same /chat would give, so they're shown, not retried.
  const STREAM_FALLBACK_STATUSES = [404, 405, 501, 502, 504];

  /** Reads server-sent events from text that arrives in pieces: push() returns the events each
   * piece completes as [{ event, data }]; end() returns the last one if the text stopped without
   * a blank line. Comments and fields other than event and data are ignored. */
  function sseParser() {
    let buffer = "";
    let event = "message";
    let data = [];
    const line = (text, out) => {
      if (text === "") {
        if (data.length) out.push({ event, data: data.join("\n") });
        event = "message";
        data = [];
      } else if (!text.startsWith(":")) {
        const colon = text.indexOf(":");
        const field = colon < 0 ? text : text.slice(0, colon);
        let value = colon < 0 ? "" : text.slice(colon + 1);
        if (value.startsWith(" ")) value = value.slice(1);
        if (field === "event") event = value;
        else if (field === "data") data.push(value);
      }
    };
    return {
      push(text) {
        const out = [];
        buffer += text;
        const lines = buffer.split("\n");
        buffer = lines.pop(); // not complete yet
        for (const l of lines) line(l.endsWith("\r") ? l.slice(0, -1) : l, out);
        return out;
      },
      end(text = "") {
        const out = this.push(`${text}\n\n`);
        buffer = "";
        return out;
      },
    };
  }

  /** The fetch() options for a JSON POST to /chat or /chat/stream. */
  function postJson(payload, signal) {
    return {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal,
    };
  }

  /**
   * Asks POST /chat/stream. `onText(text)` gets the whole answer so far each time more arrives.
   * Returns { status, ok, data } like a /chat response (data is the "done" event's answer), or
   * null when streaming didn't work and /chat should be asked instead: the browser can't read a
   * stream, the API has no stream endpoint, or the stream stopped before its last event.
   */
  async function streamAnswer(fetchFn, apiUrl, payload, onText, signal) {
    if (typeof TextDecoder !== "function") return null;
    const response = await fetchFn(`${apiUrl}/chat/stream`, postJson(payload, signal));
    const type = (response.headers && response.headers.get("Content-Type")) || "";
    if (!type.toLowerCase().startsWith("text/event-stream")) {
      if (response.ok || STREAM_FALLBACK_STATUSES.includes(response.status)) return null;
      return { status: response.status, ok: false, data: await response.json().catch(() => null) };
    }
    if (!response.body || typeof response.body.getReader !== "function") return null;
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    const parser = sseParser();
    let text = "";
    for (;;) {
      const { value, done } = await reader.read();
      const events = done ? parser.end(decoder.decode()) : parser.push(decoder.decode(value, { stream: true }));
      for (const { event, data: raw } of events) {
        let data = null;
        try {
          data = JSON.parse(raw);
        } catch {
          continue; // not ours
        }
        if (event === "delta" && typeof data?.text === "string") {
          text += data.text;
          onText(text);
        } else if (event === "done" && typeof data?.answer === "string") {
          reader.cancel().catch(() => {});
          return { status: 200, ok: true, data };
        } else if (event === "error") {
          reader.cancel().catch(() => {});
          return { status: 503, ok: false, data };
        }
      }
      if (done) return null;
    }
  }

  /**
   * The answer to one question: streamed when possible, else from POST /chat. `onText` gets the
   * text so far while it streams; `onRestart()` runs before /chat is asked, so any streamed text
   * can be taken down. Returns { status, ok, data } as /chat's response; throws on a network
   * error or timeout, as fetch does.
   */
  async function requestAnswer(fetchFn, apiUrl, payload, { onText = () => {}, onRestart = () => {}, signal } = {}) {
    const streamed = await streamAnswer(fetchFn, apiUrl, payload, onText, signal).catch(() => null);
    if (streamed) return streamed;
    onRestart();
    const response = await fetchFn(`${apiUrl}/chat`, postJson(payload, signal));
    return { status: response.status, ok: response.ok, data: await response.json().catch(() => null) };
  }

  /** What to say when /chat doesn't answer. The API's own words when it gives them (question too
   * long, too many questions, busy), shown as plain text like every other message. */
  function errorMessage(status, data) {
    if ([422, 429, 503].includes(status) && typeof data?.detail === "string") return data.detail;
    if (status === 429) return MESSAGES.rateLimited;
    if (status === 503) return MESSAGES.unavailable;
    return MESSAGES.generic;
  }

  // ---------------------------------------------------------------------------------------------
  // Markdown: text -> a list of plain objects (parseMarkdown) -> DOM nodes (renderMarkdown).
  // ---------------------------------------------------------------------------------------------

  /** True only for absolute http:// and https:// URLs. */
  function isSafeUrl(href) {
    try {
      const url = new URL(href);
      return url.protocol === "https:" || url.protocol === "http:";
    } catch {
      return false; // relative or malformed
    }
  }

  const LINK = /^\[([^\]\n]+)\]\(\s*<?([^()\s<>]+)>?\s*\)/; // [text](url)
  const BARE_URL = /^https?:\/\/[^\s<>"]+/;

  /** Bold (**x**), italic (*x*), [text](url) and bare http(s) URLs. Everything else is text. */
  function parseInline(text, allowLinks = true) {
    const out = [];
    let plain = "";
    const flush = () => {
      if (plain) out.push({ type: "text", text: plain });
      plain = "";
    };
    let i = 0;
    while (i < text.length) {
      const rest = text.slice(i);

      if (rest.startsWith("**")) {
        const end = text.indexOf("**", i + 2);
        if (end > i + 2) {
          flush();
          out.push({ type: "strong", children: parseInline(text.slice(i + 2, end), allowLinks) });
          i = end + 2;
          continue;
        }
      } else if (rest[0] === "*" && rest[1] && rest[1] !== " ") {
        const end = text.indexOf("*", i + 1);
        if (end > i + 1 && text[end - 1] !== " ") {
          flush();
          out.push({ type: "em", children: parseInline(text.slice(i + 1, end), allowLinks) });
          i = end + 1;
          continue;
        }
      }

      const link = allowLinks && LINK.exec(rest);
      if (link) {
        flush();
        if (isSafeUrl(link[2])) {
          const children = parseInline(link[1], false); // no links inside a link
          out.push({ type: "link", href: new URL(link[2]).href, children });
        } else {
          out.push({ type: "text", text: link[0] }); // e.g. javascript: stays visible as text
        }
        i += link[0].length;
        continue;
      }

      const startsWord = i === 0 || /[\s(]/.test(text[i - 1]);
      const bare = allowLinks && startsWord && BARE_URL.exec(rest);
      if (bare) {
        const url = bare[0].replace(/[.,;:!?)\]'"]+$/, ""); // trailing punctuation isn't the URL
        if (isSafeUrl(url)) {
          flush();
          out.push({ type: "link", href: new URL(url).href, children: [{ type: "text", text: url }] });
          i += url.length;
          continue;
        }
      }

      plain += text[i];
      i += 1;
    }
    flush();
    return out;
  }

  /** Paragraphs, "- " / "* " bullet lists, "1. " numbered lists. Headings become bold lines. */
  function parseMarkdown(text) {
    const blocks = [];
    let para = null;
    let list = null;
    for (const raw of String(text).replace(/\r\n?/g, "\n").split("\n")) {
      const line = raw.trim();
      if (!line) {
        para = null; // a blank line ends a paragraph; a list may continue after it
        continue;
      }
      const bullet = /^[-*•]\s+(.*)$/.exec(line);
      const numbered = /^(\d+)[.)]\s+(.*)$/.exec(line);
      const heading = /^#{1,6}\s+(.*)$/.exec(line);

      if (bullet || numbered) {
        const type = bullet ? "ul" : "ol";
        if (!list || list.type !== type) {
          list = { type, start: numbered ? Number(numbered[1]) : 1, items: [] };
          blocks.push(list);
        }
        para = null;
        list.items.push(parseInline(bullet ? bullet[1] : numbered[2]));
      } else if (heading) {
        para = list = null;
        blocks.push({ type: "p", children: [{ type: "strong", children: parseInline(heading[1]) }] });
      } else if (list && !para && /^\s/.test(raw)) {
        // an indented line continues the last list item
        list.items[list.items.length - 1].push({ type: "text", text: " " }, ...parseInline(line));
      } else {
        list = null;
        if (para) {
          para.children.push({ type: "br" });
        } else {
          para = { type: "p", children: [] };
          blocks.push(para);
        }
        para.children.push(...parseInline(line));
      }
    }
    return blocks;
  }

  /** Builds safe DOM nodes for Markdown text. `doc` is the document (a fake one in tests). */
  function renderMarkdown(text, doc) {
    const fragment = doc.createDocumentFragment();
    for (const block of parseMarkdown(text)) {
      if (block.type === "p") {
        const p = doc.createElement("p");
        appendInline(doc, p, block.children);
        fragment.appendChild(p);
      } else {
        const list = doc.createElement(block.type === "ol" ? "ol" : "ul");
        if (block.type === "ol" && block.start > 1) list.setAttribute("start", String(block.start));
        for (const item of block.items) {
          const li = doc.createElement("li");
          appendInline(doc, li, item);
          list.appendChild(li);
        }
        fragment.appendChild(list);
      }
    }
    return fragment;
  }

  function appendInline(doc, parent, nodes) {
    for (const node of nodes) {
      if (node.type === "text") {
        parent.appendChild(doc.createTextNode(node.text));
      } else if (node.type === "br") {
        parent.appendChild(doc.createElement("br"));
      } else if (node.type === "link") {
        const a = externalLink(doc, node.href);
        appendInline(doc, a, node.children);
        a.appendChild(srOnly(doc, " (opens in a new tab)"));
        parent.appendChild(a);
      } else if (node.type === "strong" || node.type === "em") {
        const el = doc.createElement(node.type);
        appendInline(doc, el, node.children);
        parent.appendChild(el);
      }
    }
  }

  /** A link to a checked http(s) URL that opens in a new tab without access to this page. */
  function externalLink(doc, href) {
    const a = doc.createElement("a");
    a.setAttribute("href", href);
    a.setAttribute("target", "_blank");
    a.setAttribute("rel", "noopener noreferrer");
    return a;
  }

  /** Text that only screen readers announce. */
  function srOnly(doc, text) {
    const span = doc.createElement("span");
    span.className = "sr-only";
    span.appendChild(doc.createTextNode(text));
    return span;
  }

  /** "2022-03-04" -> "March 4, 2022"; anything else -> null. */
  function formatDate(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
    if (!m) return null;
    const date = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
    return date.toLocaleDateString("en-US", {
      year: "numeric",
      month: "long",
      day: "numeric",
      timeZone: "UTC",
    });
  }

  // ---------------------------------------------------------------------------------------------
  // Fluid answers: Show more, the Sources chip, and the New message button. Each takes the
  // elements it works on (fake ones in the tests) and only ever sets fixed text.
  // ---------------------------------------------------------------------------------------------

  const COLLAPSED_PX = 220; // how much of a long answer shows before "Show more" (also in the CSS)
  const MIN_HIDDEN_PX = 100; // an answer only a little too long is shown whole
  const MOTION_MS = 250; // Show more and scrolling. All motion stays between 150 and 300 ms.
  const NEAR_BOTTOM_PX = 48; // this close to the bottom counts as "reading the latest message"

  /** True when an answer this tall (in px) should start cut off with "Show more". */
  function needsShowMore(height) {
    return height > COLLAPSED_PX + MIN_HIDDEN_PX;
  }

  /** True when a scrolled element is at, or within NEAR_BOTTOM_PX of, its bottom. */
  function isNearBottom(el) {
    return el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_PX;
  }

  function reducedMotion() {
    return typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  /**
   * "Show more" under a long answer, which starts cut off at COLLAPSED_PX. With `animate()` true
   * the height slides open or shut; afterwards the answer has no fixed height, so it can keep
   * growing (for streamed text). Tabbing to a link in the cut-off part opens the answer, so focus
   * never lands on hidden text. Screen readers always get the whole answer.
   */
  function showMore(doc, answer, animate = () => false) {
    const button = doc.createElement("button");
    button.type = "button";
    button.className = "show-more";
    button.setAttribute("aria-controls", answer.id);
    button.setAttribute("aria-expanded", "false");
    button.textContent = "Show more";
    answer.classList.add("collapsed");
    let finish = null; // ends a height animation that's still running

    function set(expanded) {
      if (finish) finish();
      button.setAttribute("aria-expanded", String(expanded));
      button.textContent = expanded ? "Show less" : "Show more";
      answer.classList.toggle("collapsed", !expanded);
      if (!animate()) return;
      const cut = `${COLLAPSED_PX}px`;
      const full = `${answer.scrollHeight}px`;
      answer.style.maxHeight = expanded ? cut : full;
      answer.classList.add("sizing");
      void answer.offsetHeight; // start from the old height
      answer.style.maxHeight = expanded ? full : cut;
      const timer = setTimeout(() => finish(), MOTION_MS + 100); // if transitionend never comes
      const onEnd = (event) => event.target === answer && finish();
      answer.addEventListener("transitionend", onEnd);
      finish = () => {
        clearTimeout(timer);
        answer.removeEventListener("transitionend", onEnd);
        answer.classList.remove("sizing");
        answer.style.maxHeight = "";
        finish = null;
      };
    }

    button.addEventListener("click", () => set(button.getAttribute("aria-expanded") !== "true"));
    answer.addEventListener("focusin", (event) => {
      if (!answer.classList.contains("collapsed")) return;
      const box = event.target.getBoundingClientRect?.();
      const top = answer.getBoundingClientRect?.().top;
      if (!box || box.bottom > top + COLLAPSED_PX) set(true);
    });
    return button;
  }

  /** A "Sources (n)" chip that shows and hides the source card `list`, which starts hidden. */
  function sourcesChip(doc, list, id) {
    const chip = doc.createElement("button");
    chip.type = "button";
    chip.className = "chip sources-chip";
    chip.textContent = `Sources (${list.children.length})`;
    chip.setAttribute("aria-expanded", "false");
    chip.setAttribute("aria-controls", id);
    list.id = id;
    list.hidden = true;
    chip.addEventListener("click", () => {
      const open = chip.getAttribute("aria-expanded") !== "true";
      chip.setAttribute("aria-expanded", String(open));
      list.hidden = !open;
    });
    const block = doc.createElement("div");
    block.className = "sources-block";
    block.append(chip, list);
    return block;
  }

  /**
   * The "New message" button over the conversation. A message that arrives while the student has
   * scrolled up to read stays out of the way and the button appears; it scrolls to (and focuses)
   * the first message they haven't seen. Scrolling down to that message hides the button.
   */
  function newMessageNotifier(container, button, scrollTo) {
    let unseen = null;
    const clear = () => {
      unseen = null;
      button.hidden = true;
    };
    container.addEventListener("scroll", () => {
      const visible = unseen && container.scrollTop + container.clientHeight >= unseen.offsetTop + 40;
      if (visible || (unseen && isNearBottom(container))) clear();
    });
    button.addEventListener("click", () => {
      const target = unseen;
      clear();
      if (!target) return;
      scrollTo(target);
      target.setAttribute("tabindex", "-1"); // so focus can move there for keyboard users
      target.addEventListener("blur", () => target.removeAttribute("tabindex"), { once: true });
      target.focus({ preventScroll: true });
    });
    return {
      /** `el` was just added. Scroll to its start if `follow`, else offer the button (`notify`). */
      added(el, follow, notify = true) {
        if (follow) {
          clear();
          scrollTo(el);
        } else if (notify && !unseen) {
          unseen = el;
          button.hidden = false;
        }
      },
      clear,
    };
  }

  /** Scrolls `el` to `top` over MOTION_MS (eased), or at once with reduced motion. */
  function animateScroll(el, top, done = () => {}) {
    const from = el.scrollTop;
    if (reducedMotion() || typeof requestAnimationFrame !== "function" || Math.abs(top - from) < 2) {
      el.scrollTop = top;
      done();
      return;
    }
    const start = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - start) / MOTION_MS);
      el.scrollTop = from + (top - from) * (1 - (1 - t) ** 3);
      if (t < 1) requestAnimationFrame(step);
      else done();
    };
    requestAnimationFrame(step);
  }

  // ---------------------------------------------------------------------------------------------
  // The widget: launcher button, chat panel, and the call to POST /chat.
  // ---------------------------------------------------------------------------------------------

  // Saved conversations live in the browser only (see README). By default they're in
  // sessionStorage, so closing the browser clears them (shared campus computers). "Keep my
  // conversations on this device" moves them to localStorage, kept for MAX_AGE_DAYS.
  // If you change the limits, change MESSAGES.keepOn too.
  const HISTORY_KEY = "dsu-chat:conversations";
  const LEGACY_KEY = "dsu-chat:last-conversation"; // the one conversation version 0.5 saved
  const KEEP_KEY = "dsu-chat:keep-on-device"; // "yes" when the student turned the switch on
  const MAX_CONVERSATIONS = 10;
  const MAX_AGE_DAYS = 7;
  const MAX_SAVED_MESSAGES = 40; // per conversation
  const DAY_MS = 24 * 60 * 60 * 1000;

  // Icons used in SHELL. Static markup, no outside text.
  const ICON = {
    close:
      '<svg aria-hidden="true" focusable="false" viewBox="0 0 24 24" width="22" height="22"><path fill="currentColor" d="M18.3 5.7 12 12l6.3 6.3-1.4 1.4L10.6 13.4 4.3 19.7 2.9 18.3 9.2 12 2.9 5.7l1.4-1.4 6.3 6.3 6.3-6.3z"/></svg>',
    expand:
      '<svg class="icon-expand" aria-hidden="true" focusable="false" viewBox="0 0 24 24" width="22" height="22"><path fill="currentColor" d="M4 4h6v2H6v4H4zm10 0h6v6h-2V6h-4zM4 14h2v4h4v2H4zm14 0h2v6h-6v-2h4z"/></svg>' +
      '<svg class="icon-shrink" aria-hidden="true" focusable="false" viewBox="0 0 24 24" width="22" height="22"><path fill="currentColor" d="M8 4h2v6H4V8h4zm6 0h2v4h4v2h-6zM4 14h6v6H8v-4H4zm10 0h6v2h-4v4h-2z"/></svg>',
    chat:
      '<svg aria-hidden="true" focusable="false" viewBox="0 0 24 24" width="22" height="22"><path fill="currentColor" d="M4 4h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-5 4v-4H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z"/></svg>',
    chevron:
      '<svg class="chevron" aria-hidden="true" focusable="false" viewBox="0 0 24 24" width="22" height="22"><path fill="currentColor" d="M9.3 5.3 10.7 3.9 18.8 12l-8.1 8.1-1.4-1.4 6.7-6.7z"/></svg>',
    back:
      '<svg aria-hidden="true" focusable="false" viewBox="0 0 24 24" width="22" height="22"><path fill="currentColor" d="M14.7 5.3 13.3 3.9 5.2 12l8.1 8.1 1.4-1.4L8 12z"/></svg>',
    down:
      '<svg aria-hidden="true" focusable="false" viewBox="0 0 24 24" width="16" height="16"><path fill="currentColor" d="M13 4v12.2l5.6-5.6L20 12l-8 8-8-8 1.4-1.4 5.6 5.6V4z"/></svg>',
  };

  const WINDOW_BUTTONS = `
    <div class="window-buttons">
      <button class="icon-button expand" type="button" aria-pressed="false"
        aria-label="Full screen">${ICON.expand}</button>
      <button class="icon-button close" type="button" aria-label="Close chat">${ICON.close}</button>
    </div>`;

  // Static markup only. Never put answer, source, or user text in here.
  const SHELL = `
    <div class="widget">
      <button class="launcher" type="button" aria-expanded="false" aria-controls="panel">
        <span class="avatar avatar-launcher" aria-hidden="true"></span>
        <span class="launcher-label"></span>
      </button>
      <section class="panel" id="panel" role="dialog" hidden>
        <div class="view home">
          <div class="hero">
            <div class="hero-top">
              <span class="avatar avatar-hero" aria-hidden="true"></span>
              ${WINDOW_BUTTONS}
            </div>
            <h2 class="hero-title">Welcome to Delaware State University!</h2>
            <p class="hero-subtitle">How can we help you today?</p>
          </div>
          <div class="home-body">
            <button class="start" type="button">${ICON.chat}<span>Start New Conversation</span></button>
            <button class="card resume" type="button">
              <span class="card-text">
                <span class="card-title">Resume Last Conversation</span>
                <span class="card-preview">
                  <span class="avatar avatar-small" aria-hidden="true"></span>
                  <span class="card-preview-text">
                    <span class="card-snippet"></span>
                    <span class="card-meta"></span>
                  </span>
                </span>
              </span>
              ${ICON.chevron}
            </button>
            <button class="card past" type="button">
              <span class="card-text">
                <span class="card-title">See Past Conversations</span>
                <span class="card-meta past-meta"></span>
              </span>
              ${ICON.chevron}
            </button>
          </div>
        </div>
        <div class="view chat" hidden>
          <div class="header">
            <button class="icon-button back" type="button" aria-label="Back to home">${ICON.back}</button>
            <span class="avatar avatar-header" aria-hidden="true"></span>
            <h2 class="title"></h2>
            ${WINDOW_BUTTONS}
          </div>
          <p class="notice">
            Hi, I'm Jada! I'm an AI assistant that answers from DSU's website, and I can make mistakes. Don't
            share personal information. For official decisions, contact the DSU office that
            handles your question.
          </p>
          <div class="messages-wrap">
            <div class="messages" role="log" aria-live="polite" aria-label="Conversation" tabindex="0"></div>
            <button class="new-message" type="button" hidden>${ICON.down}<span>New message</span></button>
          </div>
          <form class="form" novalidate>
            <label class="sr-only" for="question">Your question</label>
            <div class="composer">
              <textarea id="question" rows="2" placeholder="Ask a question about DSU"
                aria-describedby="count form-error"></textarea>
              <div class="form-row">
                <span class="count" id="count"></span>
                <button class="send" type="submit">
                  <svg aria-hidden="true" focusable="false" viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M11 20V7.8l-5.6 5.6L4 12l8-8 8 8-1.4 1.4L13 7.8V20z"/></svg>
                  <span class="sr-only">Send</span>
                </button>
              </div>
            </div>
            <p class="form-error" id="form-error" role="alert"></p>
          </form>
        </div>
        <div class="view past-view" hidden>
          <div class="header">
            <button class="icon-button back" type="button" aria-label="Back to home">${ICON.back}</button>
            <span class="avatar avatar-header" aria-hidden="true"></span>
            <h2 class="title">Past Conversations</h2>
            ${WINDOW_BUTTONS}
          </div>
          <p class="notice">
            This list is stored only in this browser. Close the browser on a shared computer, or
            delete the conversations below, to clear it.
          </p>
          <div class="keep">
            <label class="keep-switch">
              <input class="keep-toggle" type="checkbox" role="switch" aria-describedby="keep-help" />
              <span class="keep-track" aria-hidden="true"></span>
              <span class="keep-label">Keep my conversations on this device</span>
            </label>
            <p class="keep-help" id="keep-help"></p>
          </div>
          <div class="history-body">
            <ul class="history" aria-label="Past conversations"></ul>
            <p class="history-empty" hidden>No past conversations yet.</p>
            <div class="history-actions">
              <button class="delete-all" type="button">Delete all conversations</button>
              <div class="confirm" role="group" aria-labelledby="confirm-text" hidden>
                <p id="confirm-text">Delete all saved conversations in this browser? This can't be undone.</p>
                <div class="confirm-buttons">
                  <button class="confirm-delete" type="button">Delete all</button>
                  <button class="confirm-cancel" type="button">Cancel</button>
                </div>
              </div>
            </div>
            <p class="sr-only" role="status" id="history-status"></p>
          </div>
        </div>
      </section>
    </div>`;

  /** How long ago, kept general on purpose: "Just now", "5 Mins Ago", "2 Hrs Ago", "3 Days Ago". */
  function timeAgo(ms, now = Date.now()) {
    const minutes = Math.max(0, Math.floor((now - ms) / 60000));
    if (minutes < 1) return "Just now";
    if (minutes < 60) return `${minutes} ${minutes === 1 ? "Min Ago" : "Mins Ago"}`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} ${hours === 1 ? "Hr Ago" : "Hrs Ago"}`;
    const days = Math.floor(hours / 24);
    return `${days} ${days === 1 ? "Day Ago" : "Days Ago"}`;
  }

  // ---- Saved conversations ---------------------------------------------------------------------
  // A conversation: { id, created, updated, title, messages: [{ role: "user", text } |
  // { role: "bot", data: { answer, sources } }] }. Only successful questions and answers are kept.

  /** The first question, shortened, as the conversation's title. */
  function conversationTitle(messages) {
    const first = messages.find((m) => m && m.role === "user" && typeof m.text === "string");
    const text = first ? first.text.replace(/\s+/g, " ").trim() : "";
    if (!text) return "Conversation";
    return text.length > 80 ? `${text.slice(0, 79)}…` : text;
  }

  /** Valid conversations from the last MAX_AGE_DAYS, newest first, at most MAX_CONVERSATIONS. */
  function pruneHistory(items, now) {
    return (Array.isArray(items) ? items : [])
      .filter(
        (c) =>
          c &&
          typeof c.id === "string" &&
          typeof c.updated === "number" &&
          Array.isArray(c.messages) &&
          c.messages.length > 0 &&
          now - c.updated < MAX_AGE_DAYS * DAY_MS
      )
      .sort((a, b) => b.updated - a.updated)
      .slice(0, MAX_CONVERSATIONS);
  }

  /** The list with `conversation` at the front, replacing an older copy with the same id. */
  function upsertConversation(items, conversation, now) {
    return pruneHistory([conversation, ...items.filter((c) => c.id !== conversation.id)], now);
  }

  /** Writes the list. Storage can be blocked or full, so this never throws. */
  function saveHistory(storage, items) {
    try {
      if (items.length) {
        storage.setItem(HISTORY_KEY, JSON.stringify({ version: 1, conversations: items }));
      } else {
        storage.removeItem(HISTORY_KEY);
      }
      return true;
    } catch {
      return false; // private browsing or storage full: conversations just aren't kept
    }
  }

  /**
   * Reads the saved list, drops expired conversations, and moves the single conversation older
   * versions saved (LEGACY_KEY) into the list. Never throws; returns [] if storage is unusable.
   */
  function loadHistory(storage, now) {
    let items = [];
    let legacy = null;
    try {
      const saved = JSON.parse(storage.getItem(HISTORY_KEY) || "null");
      if (saved && Array.isArray(saved.conversations)) items = saved.conversations;
      legacy = JSON.parse(storage.getItem(LEGACY_KEY) || "null");
    } catch {
      return [];
    }
    if (legacy && Array.isArray(legacy.messages) && legacy.messages.length) {
      const updated = typeof legacy.updated === "number" ? legacy.updated : now;
      items.push({
        id: `c${updated.toString(36)}`,
        created: updated,
        updated,
        title: conversationTitle(legacy.messages),
        messages: legacy.messages,
      });
    }
    const pruned = pruneHistory(items, now);
    if (saveHistory(storage, pruned) && legacy) {
      try {
        storage.removeItem(LEGACY_KEY); // only once the new list is safely written
      } catch {
        // ignore
      }
    }
    return pruned;
  }

  /** Deletes the saved list (and an older version's single conversation). Never throws. */
  function clearHistory(storage) {
    saveHistory(storage, []);
    try {
      storage.removeItem(LEGACY_KEY);
    } catch {
      // ignore
    }
  }

  /** True when the student turned on "Keep my conversations on this device". */
  function keepsOnDevice(local) {
    try {
      return local.getItem(KEEP_KEY) === "yes";
    } catch {
      return false;
    }
  }

  /**
   * The saved conversations, and whether they're kept on this device. Kept: they're in local
   * (localStorage). Not kept (the default): they're in session (sessionStorage), and anything an
   * older version left in local moves to session and is deleted from local, so it doesn't linger
   * on a shared computer. Never throws.
   */
  function openHistory(local, session, now) {
    if (keepsOnDevice(local)) return { keep: true, items: loadHistory(local, now) };
    const leftover = loadHistory(local, now); // also converts LEGACY_KEY
    clearHistory(local);
    let items = loadHistory(session, now);
    if (leftover.length) {
      const ids = new Set(items.map((c) => c.id));
      items = pruneHistory([...items, ...leftover.filter((c) => !ids.has(c.id))], now);
      saveHistory(session, items);
    }
    return { keep: false, items };
  }

  /**
   * Turns "Keep my conversations on this device" on or off, moving items to the matching
   * storage and deleting them from the other. False (and nothing changes) if the browser
   * won't store the setting.
   */
  function setKeepOnDevice(local, session, keep, items) {
    try {
      if (keep) local.setItem(KEEP_KEY, "yes");
      else local.removeItem(KEEP_KEY);
    } catch {
      return false;
    }
    if (keepsOnDevice(local) !== keep) return false; // a stand-in storage that keeps nothing
    saveHistory(keep ? local : session, items);
    clearHistory(keep ? session : local);
    return true;
  }

  /** window.localStorage or window.sessionStorage, or a stand-in that keeps nothing if the
   * browser blocks it. */
  function browserStorage(name) {
    try {
      const storage = window[name];
      if (storage) return storage;
    } catch {
      // blocked (for example, third-party storage disabled)
    }
    return { getItem: () => null, setItem() {}, removeItem() {} };
  }

  function newConversationId(now) {
    return `c${now.toString(36)}${Math.random().toString(36).slice(2, 6)}`;
  }

  /**
   * Roboto, from a fonts/ folder next to dsu-chat.css (see README, "Font"). Fonts registered with
   * document.fonts are usable inside the Shadow DOM; the unusual family name keeps the host page
   * from picking them up. If the files aren't there, the CSS falls back to Arial.
   */
  function loadFonts(baseUrl) {
    if (typeof FontFace !== "function" || !document.fonts) return;
    const faces = [
      ["400", "Roboto", "Roboto-Regular"],
      ["500", "Roboto Medium", "Roboto-Medium"],
      ["700", "Roboto Bold", "Roboto-Bold"],
    ];
    for (const [weight, localName, file] of faces) {
      const url = new URL(`fonts/${file}.ttf`, baseUrl).href;
      new FontFace("DSU Chat Roboto", `local("${localName}"), url("${url}")`, { weight })
        .load()
        .then((face) => document.fonts.add(face))
        .catch(() => {}); // not installed and not hosted: use the fallback fonts
    }
  }

  function mount(options) {
    loadFonts(options.cssUrl);
    const host = document.createElement("div");
    host.id = HOST_ID;
    host.style.display = "none"; // shown once the stylesheet loads, so it never flashes unstyled
    const root = host.attachShadow({ mode: "open" });

    const style = document.createElement("link");
    style.rel = "stylesheet";
    style.href = options.cssUrl;
    style.addEventListener("load", () => (host.style.display = ""));
    style.addEventListener("error", () => console.error("DSU chat: couldn't load", options.cssUrl));
    root.appendChild(style);

    const template = document.createElement("template");
    template.innerHTML = SHELL; // static markup, see above
    root.appendChild(template.content);
    document.body.appendChild(host);

    const $ = (selector) => root.querySelector(selector);
    const $$ = (selector) => Array.from(root.querySelectorAll(selector));
    const widget = $(".widget");
    const launcher = $(".launcher");
    const panel = $(".panel");
    const home = $(".home");
    const chat = $(".chat");
    const pastView = $(".past-view");
    const historyList = $(".history");
    const historyStatus = $("#history-status");
    const confirmBox = $(".confirm");
    const deleteAll = $(".delete-all");
    const resume = $(".resume");
    // The conversation can take focus (tabindex="0" in SHELL), so it can be scrolled from the
    // keyboard even when nothing in it is focusable, as while an inert answer streams in.
    const messages = $(".messages");
    const form = $(".form");
    const input = $("#question");
    const count = $("#count");
    const formError = $("#form-error");
    const send = $(".send");
    let busy = false;
    let closing = null; // timer while the panel plays its closing fade
    let restoring = false; // true while a saved conversation is redrawn
    let scrolling = false; // true while the conversation scrolls to a new message
    let ids = 0;
    const nextId = (prefix) => `${prefix}-${++ids}`;
    const unfitted = []; // answers added while the chat was hidden, measured once it shows
    const newMessage = newMessageNotifier(messages, $(".new-message"), scrollToMessage);
    const local = browserStorage("localStorage");
    const session = browserStorage("sessionStorage");
    // history: saved conversations, newest first. keep: whether they're kept on this device.
    let { keep, items: history } = openHistory(local, session, Date.now());
    const store = () => (keep ? local : session);
    const keepToggle = $(".keep-toggle");
    let conversation = null; // the conversation on screen, once it has a saved question
    // What .messages shows: a conversation id, "new" for a new one, or null for nothing yet.
    let shownId = null;

    $(".launcher-label").textContent = options.title;
    $(".chat .title").textContent = options.title;
    panel.setAttribute("aria-label", options.title);
    updateCount();
    updateHome();

    function open() {
      clearTimeout(closing);
      closing = null;
      panel.classList.remove("closing");
      panel.hidden = false;
      widget.classList.add("open");
      launcher.setAttribute("aria-expanded", "true");
      updateHome();
      if (!chat.hidden) input.focus();
      else if (!pastView.hidden) pastView.querySelector(".back").focus();
      else $(".start").focus();
    }

    /** Fades the panel out toward the chat button (CSS .panel.closing), then hides it. */
    function close() {
      if (!widget.classList.contains("open")) return;
      widget.classList.remove("open");
      launcher.setAttribute("aria-expanded", "false");
      launcher.focus();
      panel.classList.add("closing");
      closing = setTimeout(() => {
        panel.hidden = true;
        panel.classList.remove("closing");
        closing = null;
      }, PANEL_CLOSE_MS);
    }

    /** Shows one of the three screens: home, chat, or past-view. */
    function showView(view) {
      home.hidden = view !== home;
      chat.hidden = view !== chat;
      pastView.hidden = view !== pastView;
    }

    function showHome() {
      updateHome();
      showView(home);
      $(".start").focus();
    }

    function showChat() {
      showView(chat);
      for (const answer of unfitted.splice(0)) if (answer.isConnected) fitAnswer(answer);
      input.focus();
    }

    /** Empties the conversation area. */
    function clearMessages() {
      messages.textContent = "";
      unfitted.length = 0;
      newMessage.clear();
    }

    function startNew() {
      clearMessages();
      conversation = null;
      shownId = "new";
      input.placeholder = MESSAGES.askPlaceholder;
      addBotText(MESSAGES.welcome, Date.now());
      showChat();
    }

    /** Reopens a saved conversation. Answers go through addAnswer, the same as new ones. */
    function openConversation(saved) {
      if (shownId !== saved.id) {
        clearMessages();
        restoring = true;
        addBotText(MESSAGES.welcome, saved.created || saved.updated);
        for (const m of saved.messages) {
          // Conversations saved before version 0.7 have no per-message time: use the last update.
          const at = typeof m.at === "number" ? m.at : saved.updated;
          if (m.role === "user") userMessage(String(m.text), at);
          else if (m.data && typeof m.data.answer === "string") addAnswer(m.data, at);
        }
        restoring = false;
        conversation = saved;
        shownId = saved.id;
        // Each question is still answered on its own: say so where new questions start.
        const note = document.createElement("p");
        note.className = "reopened-note";
        note.setAttribute("role", "note");
        note.textContent = MESSAGES.reopened.replace("{name}", options.title);
        messages.appendChild(note);
        input.placeholder = MESSAGES.askAgainPlaceholder;
      }
      showChat();
      newMessage.clear();
      const last = messages.lastElementChild;
      if (last) messages.scrollTop = last.offsetTop - 16;
    }

    /** Plain text of a conversation's last answer, for previews. Built safely, then read back. */
    function lastSnippet(saved) {
      const lastBot = [...saved.messages].reverse().find((m) => m.role === "bot");
      if (lastBot && typeof lastBot.data?.answer === "string") {
        const scratch = document.createElement("div");
        scratch.appendChild(renderMarkdown(lastBot.data.answer, document));
        // One space between paragraphs and list items, so they don't run together.
        const parts = [...scratch.querySelectorAll("p, li")].map((el) => el.textContent);
        return (parts.length ? parts.join(" ") : scratch.textContent).replace(/\s+/g, " ").trim();
      }
      return saved.title || conversationTitle(saved.messages);
    }

    function resumeLast() {
      if (history.length) openConversation(history[0]);
    }

    function remember(entry) {
      const now = Date.now();
      if (!conversation) {
        conversation = { id: newConversationId(now), created: now, messages: [] };
        if (shownId === "new" || shownId === null) shownId = conversation.id;
      }
      conversation.messages.push(entry);
      conversation.messages = conversation.messages.slice(-MAX_SAVED_MESSAGES);
      conversation.updated = now;
      conversation.title = conversationTitle(conversation.messages);
      history = upsertConversation(history, conversation, now);
      saveHistory(store(), history);
    }

    /** Fills the Resume and Past cards. Text only, through textContent. */
    function updateHome() {
      const last = history[0] || null;
      const snippet = last ? lastSnippet(last) : "";
      resume.setAttribute("aria-disabled", String(!last));
      $(".card-snippet").textContent = last ? snippet : "You don't have a conversation yet.";
      $(".card-meta").textContent = last
        ? `${options.title} · ${timeAgo(last.updated)}`
        : "Start one to see it here.";
      const n = history.length;
      $(".past-meta").textContent = n
        ? `${n} saved ${n === 1 ? "conversation" : "conversations"} ${
            keep ? "on this device" : "until you close this browser"
          }`
        : "No past conversations yet";
    }

    /** The Past Conversations list. Titles are the student's own text: textContent only. */
    function renderHistory() {
      historyList.textContent = "";
      for (const saved of history) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "history-item";
        const avatar = document.createElement("span");
        avatar.className = "avatar avatar-list";
        avatar.setAttribute("aria-hidden", "true");
        const text = document.createElement("span");
        text.className = "history-text";
        const title = document.createElement("span");
        title.className = "history-title";
        title.textContent = lastSnippet(saved);
        const meta = document.createElement("span");
        meta.className = "history-meta";
        meta.dataset.at = String(saved.updated);
        meta.dataset.who = options.title;
        meta.textContent = `${options.title} · ${timeAgo(saved.updated)}`;
        text.append(title, meta);
        // Screen readers also hear the first question, so similar answers can be told apart.
        const first = srOnly(document, `. First question: ${saved.title || conversationTitle(saved.messages)}`);
        button.append(avatar, text, first, chevronIcon());
        button.addEventListener("click", () => openConversation(saved));
        const li = document.createElement("li");
        li.appendChild(button);
        historyList.appendChild(li);
      }
      const empty = history.length === 0;
      historyList.hidden = empty;
      $(".history-empty").hidden = !empty;
      deleteAll.hidden = empty;
      confirmBox.hidden = true;
    }

    function showKeep() {
      keepToggle.checked = keep;
      $("#keep-help").textContent = keep ? MESSAGES.keepOn : MESSAGES.keepOff;
    }

    function changeKeep() {
      const want = keepToggle.checked;
      if (setKeepOnDevice(local, session, want, history)) {
        keep = want;
        historyStatus.textContent = keep ? MESSAGES.keepOn : MESSAGES.keepOff;
      } else {
        historyStatus.textContent = MESSAGES.keepFailed;
      }
      showKeep();
      updateHome();
    }

    function showPast() {
      history = pruneHistory(history, Date.now());
      showKeep();
      renderHistory();
      historyStatus.textContent = "";
      showView(pastView);
      (historyList.querySelector("button") || pastView.querySelector(".back")).focus();
    }

    function askDeleteAll() {
      deleteAll.hidden = true;
      confirmBox.hidden = false;
      $(".confirm-cancel").focus();
    }

    function cancelDeleteAll() {
      confirmBox.hidden = true;
      deleteAll.hidden = false;
      deleteAll.focus();
    }

    function confirmDeleteAll() {
      history = [];
      clearHistory(local);
      clearHistory(session);
      if (conversation) {
        // The open chat was saved, so it's gone too.
        clearMessages();
        conversation = null;
        shownId = null;
      }
      renderHistory();
      historyStatus.textContent = "All saved conversations were deleted.";
      pastView.querySelector(".back").focus();
    }

    function toggleFullscreen() {
      const on = panel.classList.toggle("fullscreen");
      widget.classList.toggle("fullscreen", on);
      for (const button of $$(".expand")) {
        button.setAttribute("aria-pressed", String(on));
        button.setAttribute("aria-label", on ? "Exit full screen" : "Full screen");
      }
    }

    launcher.addEventListener("click", () => (widget.classList.contains("open") ? close() : open()));
    for (const button of $$(".close")) button.addEventListener("click", close);
    for (const button of $$(".expand")) button.addEventListener("click", toggleFullscreen);
    for (const button of $$(".back")) button.addEventListener("click", showHome);
    $(".start").addEventListener("click", startNew);
    resume.addEventListener("click", resumeLast);
    $(".past").addEventListener("click", showPast);
    deleteAll.addEventListener("click", askDeleteAll);
    keepToggle.addEventListener("change", changeKeep);
    $(".confirm-cancel").addEventListener("click", cancelDeleteAll);
    $(".confirm-delete").addEventListener("click", confirmDeleteAll);
    panel.addEventListener("keydown", (event) => {
      if (event.key === "Escape") close();
    });

    input.addEventListener("input", updateCount);
    input.addEventListener("keydown", (event) => {
      // Enter sends; Shift+Enter adds a new line.
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        form.requestSubmit();
      }
    });
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      ask();
    });

    function updateCount() {
      const length = input.value.trim().length;
      const tooLong = length > options.maxChars;
      count.textContent = `${length} / ${options.maxChars} characters`;
      count.classList.toggle("over", tooLong);
      input.setAttribute("aria-invalid", String(tooLong));
      if (!tooLong) showFormError("");
      updateSend();
    }

    /** The send arrow is grayed out until there's text, and while an answer is loading. */
    function updateSend() {
      send.disabled = busy || !input.value.trim();
      send.classList.toggle("busy", busy);
    }

    function showFormError(text) {
      formError.textContent = text;
      formError.hidden = !text;
    }

    async function ask() {
      if (busy) return;
      const question = input.value.trim();
      if (!question) {
        showFormError("Please type a question first.");
        input.focus();
        return;
      }
      if (question.length > options.maxChars) {
        showFormError(
          `Your question is too long. Please keep it under ${options.maxChars} characters.`
        );
        input.focus();
        return;
      }

      if (shownId === null) {
        // Asked without Start or Resume (for example from a script): begin a new conversation.
        clearMessages();
        conversation = null;
        shownId = "new";
        addBotText(MESSAGES.welcome);
        showChat();
      }
      showFormError("");
      const askedAt = Date.now();
      userMessage(question, askedAt);
      input.value = "";
      updateCount();
      setBusy(true);
      // Typing indicator: three dots on screen, "Looking that up…" for screen readers.
      const loading = addMessage("bot loading", null, (el) => {
        const dots = document.createElement("span");
        dots.className = "dots";
        dots.setAttribute("aria-hidden", "true");
        dots.append(...[1, 2, 3].map(() => document.createElement("span")));
        el.append(dots, srOnly(document, MESSAGES.loading));
      });

      // Only the conversation on screen: a new conversation starts with no history.
      const history = recentTurns(conversation ? conversation.messages : []);
      const streaming = streamedText(loading);
      try {
        const response = await requestAnswer(
          fetch,
          options.apiUrl,
          { question, history },
          {
            onText: streaming.show,
            onRestart: streaming.remove, // /chat answers from the start: the dots come back
            signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS), // one deadline for stream and /chat
          }
        );
        const { status, ok, data } = response;
        const shown = streaming.stop(); // the streamed bubble, if any text came
        loading.remove(); // first, so the reply takes its place instead of jumping up after it
        if (ok && data && typeof data.answer === "string") {
          const answeredAt = Date.now();
          addAnswer(data, answeredAt, shown);
          remember({ role: "user", text: question, at: askedAt });
          remember({ role: "bot", at: answeredAt, data: { answer: data.answer, sources: data.sources } });
        } else {
          if (shown) shown.remove(); // an answer that broke off isn't left half-written
          failed(errorMessage(status, data), question);
        }
      } catch {
        streaming.stop()?.remove();
        loading.remove();
        failed(MESSAGES.network, question); // offline, CORS blocked, or timed out
      } finally {
        loading.remove();
        setBusy(false);
      }
    }

    /**
     * The answer bubble while text streams in. It takes the typing indicator's place when the
     * first text arrives, and is redrawn with renderMarkdown (never as HTML) at most once a frame.
     * It is inert while it grows: screen readers hear the finished answer once, and its links
     * can't take focus until the answer is complete. stop() returns the bubble (or null), which
     * the finished answer then replaces in place.
     */
    function streamedText(loading) {
      let group = null;
      let answer = null;
      let latest = "";
      let queued = false;
      let stopped = false;
      const paint = () => {
        queued = false;
        if (stopped || !answer) return;
        answer.replaceChildren(renderMarkdown(latest, document));
      };
      return {
        show(text) {
          if (stopped) return;
          latest = text;
          if (!group) {
            const bubble = document.createElement("div");
            bubble.className = "message bot streaming";
            answer = document.createElement("div");
            answer.className = "answer";
            answer.inert = true;
            answer.setAttribute("aria-hidden", "true"); // for browsers without inert
            bubble.appendChild(answer);
            group = document.createElement("div");
            group.className = "message-group bot settled"; // the dots already slid in
            group.appendChild(bubble);
            loading.replaceWith(group);
          }
          if (queued) return;
          queued = true;
          if (typeof requestAnimationFrame === "function") requestAnimationFrame(paint);
          else setTimeout(paint, 0);
        },
        remove() {
          if (group) group.replaceWith(loading);
          group = answer = null;
          latest = "";
        },
        stop() {
          stopped = true;
          return group;
        },
      };
    }

    function setBusy(value) {
      busy = value;
      updateSend();
    }

    function failed(text, question) {
      addMessage("bot error", Date.now(), (el) => {
        const p = document.createElement("p");
        p.textContent = text;
        el.appendChild(p);
        if (!input.value) {
          input.value = question;
          updateCount();
          const hint = document.createElement("p");
          hint.textContent = MESSAGES.retry;
          el.appendChild(hint);
        }
      });
    }

    function userMessage(text, at) {
      addMessage("user", at, (el) => (el.textContent = text));
    }

    function addBotText(text, at = Date.now()) {
      addMessage("bot", at, (el) => {
        const p = document.createElement("p");
        p.textContent = text;
        el.appendChild(p);
      });
    }

    /** An answer with its sources and thumbs. `replacing`: the streamed bubble it takes over. */
    function addAnswer(data, at = Date.now(), replacing = null) {
      addMessage("bot", at, (el) => {
        const answer = document.createElement("div");
        answer.className = "answer";
        answer.appendChild(renderMarkdown(data.answer, document));
        el.appendChild(answer);

        const sources = (Array.isArray(data.sources) ? data.sources : []).filter(
          (s) => s && typeof s.url === "string" && isSafeUrl(s.url)
        );
        if (sources.length) el.appendChild(sourcesChip(document, sourceCards(sources), nextId("sources")));
      }, typeof data.request_id === "string" && data.request_id
        ? () => feedbackButtons(data.request_id)
        : null, replacing);
    }

    /**
     * Thumbs up / down on an answer's time line, sent to POST /feedback with the answer's request ID.
     * They show when the answer is hovered or a thumb has keyboard focus, and stay visible once
     * one is chosen. Rating again replaces the earlier rating.
     */
    function feedbackButtons(requestId) {
      const group = document.createElement("div");
      group.className = "feedback";
      group.setAttribute("role", "group");
      group.setAttribute("aria-label", "Was this answer helpful?");
      const status = document.createElement("span");
      status.className = "feedback-status";
      status.setAttribute("role", "status");

      const buttons = FEEDBACK_BUTTONS.map(({ rating, label, path }) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `feedback-button feedback-${rating}`;
        button.setAttribute("aria-label", label);
        button.setAttribute("aria-pressed", "false");
        button.title = label;
        button.appendChild(svgIcon(path, 16));
        button.addEventListener("click", () => rate(rating, button));
        return button;
      });

      let sending = false; // not `disabled`: that would drop keyboard focus from the button
      async function rate(rating, chosen) {
        if (sending || chosen.getAttribute("aria-pressed") === "true") return;
        sending = true;
        status.textContent = "";
        try {
          const response = await fetch(...feedbackRequest(options.apiUrl, requestId, rating));
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          buttons.forEach((b) => b.setAttribute("aria-pressed", String(b === chosen)));
          group.classList.add("chosen"); // keep the thumbs visible once one is picked
          status.textContent = MESSAGES.feedbackThanks;
        } catch {
          status.textContent = MESSAGES.feedbackFailed;
        } finally {
          sending = false;
        }
      }

      group.append(...buttons, status);
      return group;
    }

    function sourceCards(sources) {
      const list = document.createElement("ul");
      list.className = "sources";
      list.setAttribute("aria-label", "Sources");
      for (const source of sources) {
        const url = new URL(source.url);
        const a = externalLink(document, url.href);
        a.className = "source-card";

        const title = document.createElement("span");
        title.className = "source-title";
        title.textContent = String(source.title || url.hostname);
        const where = document.createElement("span");
        where.className = "source-url";
        where.textContent = url.hostname.replace(/^www\./, "") + url.pathname.replace(/\/$/, "");
        a.append(title, where);

        const updated = formatDate(source.last_updated);
        if (updated) {
          const date = document.createElement("span");
          date.className = "source-date";
          date.textContent = `Last updated ${updated}`;
          a.appendChild(date);
        }
        a.appendChild(srOnly(document, " (opens in a new tab)"));

        const li = document.createElement("li");
        li.appendChild(a);
        list.appendChild(li);
      }
      return list;
    }

    /**
     * Appends a message bubble; `fill(el)` adds its content with DOM calls, never HTML. With a time
     * (`at`), the bubble and a "You · 5 Mins" / "Ask DSU · 2 Hrs" line under it are added as one
     * group. `trailing()`, like the feedback buttons, goes at the right end of that line. The
     * loading bubble has no time. Returns the bubble.
     *
     * The view scrolls to the start of the new message, so a long answer is read from the top,
     * unless the student has scrolled up to read: then it stays put and "New message" appears.
     *
     * With `replacing` (a streamed answer's bubble), the message takes that bubble's place without
     * moving: the student may be reading it. A long one keeps its full height, with "Show less".
     * "New message" appears only if they've scrolled up past where it starts.
     */
    function addMessage(kind, at, fill, trailing, replacing = null) {
      const el = document.createElement("div");
      el.className = `message ${kind}`;
      fill(el);
      let added = el;
      if (typeof at === "number") {
        const who = kind.startsWith("user") ? "You" : options.title;
        const meta = document.createElement("p");
        meta.className = "message-meta";
        meta.dataset.at = String(at);
        meta.dataset.who = who;
        meta.textContent = `${who} · ${timeAgo(at)}`;
        let footer = meta;
        if (trailing) {
          footer = document.createElement("div");
          footer.className = "message-footer";
          footer.append(meta, trailing());
        }
        added = document.createElement("div");
        added.className = `message-group ${kind.startsWith("user") ? "user" : "bot"}`;
        added.append(el, footer);
      }
      if (replacing && replacing.isConnected) {
        added.classList.add("settled"); // already on screen: no slide-in
        replacing.replaceWith(added); // added whole, so the live region reads it once
        for (const answer of el.querySelectorAll(".answer")) fitAnswer(answer, true);
        const below = messages.scrollTop + messages.clientHeight < added.offsetTop + 40;
        newMessage.added(added, false, below && !!messages.clientHeight);
        return el;
      }
      const mine = kind.startsWith("user");
      const follow = mine || restoring || scrolling || !messages.clientHeight || isNearBottom(messages);
      if (restoring) added.classList.add("settled"); // redrawn messages don't slide in
      messages.appendChild(added); // added whole, so the live region reads it once
      for (const answer of el.querySelectorAll(".answer")) fitAnswer(answer);
      if (!restoring) newMessage.added(added, follow, !kind.includes("loading"));
      return el;
    }

    function scrollToMessage(el) {
      scrolling = true;
      animateScroll(messages, Math.max(0, el.offsetTop - 16), () => (scrolling = false));
    }

    /** Cuts a long answer off with "Show more", or, for one that was just streamed and read
     * (`open`), leaves it whole with "Show less". Needs the chat on screen to measure it. */
    function fitAnswer(answer, open = false) {
      if (!messages.clientHeight) {
        unfitted.push(answer);
        return;
      }
      if (!needsShowMore(answer.scrollHeight)) return;
      answer.id = nextId("answer");
      let animate = !open; // opening a streamed answer is instant: it's already open on screen
      const button = showMore(document, answer, () => animate && !reducedMotion());
      if (open) {
        button.click();
        animate = true;
      }
      button.addEventListener("click", () => {
        // After "Show less", bring the answer's start back if it's above the view.
        const group = answer.closest(".message-group") || answer;
        if (answer.classList.contains("collapsed") && group.offsetTop < messages.scrollTop) {
          scrollToMessage(group);
        }
      });
      answer.after(button);
    }

    /** A small filled icon from one SVG path (built with DOM calls). */
    function svgIcon(d, size) {
      const NS = "http://www.w3.org/2000/svg";
      const svg = document.createElementNS(NS, "svg");
      svg.setAttribute("viewBox", "0 0 24 24");
      svg.setAttribute("width", String(size));
      svg.setAttribute("height", String(size));
      svg.setAttribute("aria-hidden", "true");
      svg.setAttribute("focusable", "false");
      const path = document.createElementNS(NS, "path");
      path.setAttribute("fill", "currentColor");
      path.setAttribute("d", d);
      svg.appendChild(path);
      return svg;
    }

    /** A ">" icon for the Past Conversations rows (SVG built with DOM calls). */
    function chevronIcon() {
      const NS = "http://www.w3.org/2000/svg";
      const svg = document.createElementNS(NS, "svg");
      svg.setAttribute("class", "chevron");
      svg.setAttribute("viewBox", "0 0 24 24");
      svg.setAttribute("width", "22");
      svg.setAttribute("height", "22");
      svg.setAttribute("aria-hidden", "true");
      svg.setAttribute("focusable", "false");
      const path = document.createElementNS(NS, "path");
      path.setAttribute("fill", "currentColor");
      path.setAttribute("d", "M9.3 5.3 10.7 3.9 18.8 12l-8.1 8.1-1.4-1.4 6.7-6.7z");
      svg.appendChild(path);
      return svg;
    }

    // Keep "5 Mins" and "2 Hrs" current while the panel is open.
    setInterval(() => {
      if (panel.hidden) return;
      for (const el of root.querySelectorAll("[data-at]")) {
        el.textContent = `${el.dataset.who} · ${timeAgo(Number(el.dataset.at))}`;
      }
    }, 60000);
  }

  // Read our own <script> tag now; document.currentScript is only set while this file runs.
  const script = typeof document !== "undefined" ? document.currentScript : null;

  function boot() {
    if (!script || document.getElementById(HOST_ID)) return; // not a script tag, or loaded twice
    const apiUrl = (script.dataset.apiUrl || "").trim().replace(/\/+$/, "");
    if (!isSafeUrl(apiUrl)) {
      console.error('DSU chat: add data-api-url="https://..." to the script tag.');
      return;
    }
    const cssUrl = new URL("dsu-chat.css", script.src);
    cssUrl.searchParams.set("v", VERSION);
    const options = {
      apiUrl,
      cssUrl: cssUrl.href,
      title: script.dataset.title || "Jada", //make it a name to make it more personable? Jada, Nadia, Nia, 
      maxChars: Number(script.dataset.maxChars) || 1000, // keep in sync with CHAT_MAX_QUESTION_CHARS
    };
    if (document.body) mount(options);
    else document.addEventListener("DOMContentLoaded", () => mount(options));
  }

  if (typeof module === "object" && module.exports) {
    // for the tests
    module.exports = {
      isSafeUrl,
      parseMarkdown,
      renderMarkdown,
      formatDate,
      feedbackRequest,
      errorMessage,
      sseParser,
      requestAnswer,
      STREAM_FALLBACK_STATUSES,
      recentTurns,
      HISTORY_TURNS,
      MESSAGES,
      conversationTitle,
      timeAgo,
      pruneHistory,
      upsertConversation,
      loadHistory,
      saveHistory,
      openHistory,
      setKeepOnDevice,
      HISTORY_KEY,
      LEGACY_KEY,
      KEEP_KEY,
      MAX_CONVERSATIONS,
      MAX_AGE_DAYS,
      needsShowMore,
      isNearBottom,
      showMore,
      sourcesChip,
      newMessageNotifier,
      COLLAPSED_PX,
    };
  }
  if (typeof document !== "undefined") boot();
})();
