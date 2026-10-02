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
 * outerHTML, insertAdjacentHTML, or document.write with answer, source, or user text.
 */
(function () {
  "use strict";

  const VERSION = "0.1.0"; // bump when you change this file or the CSS (see README)
  const HOST_ID = "dsu-chat-widget";
  const REQUEST_TIMEOUT_MS = 60000;

  const MESSAGES = {
    welcome:
      "Hi! Ask me a question about DSU, like housing, admissions, or registration. " +
      "I answer each question on its own and don't remember earlier ones, so include the " +
      "details every time.",
    loading: "Looking that up…",
    unavailable:
      "Sorry, I can't answer right now. Please try again in a few minutes, or visit desu.edu.",
    network:
      "I couldn't reach the DSU assistant. Check your internet connection and try again.",
    generic: "Something went wrong. Please try again in a few minutes, or visit desu.edu.",
    retry: "Your question is back in the box, so you can send it again.",
  };

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
  // The widget: launcher button, chat panel, and the call to POST /chat.
  // ---------------------------------------------------------------------------------------------

  // Static markup only. Never put answer, source, or user text in here.
  const SHELL = `
    <div class="widget">
      <button class="launcher" type="button" aria-expanded="false" aria-controls="panel">
        <svg aria-hidden="true" focusable="false" viewBox="0 0 24 24" width="22" height="22">
          <path fill="currentColor" d="M4 4h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-5 4v-4H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z"/>
        </svg>
        <span class="launcher-label"></span>
      </button>
      <section class="panel" id="panel" role="dialog" aria-labelledby="title" hidden>
        <header class="header">
          <h2 class="title" id="title"></h2>
          <button class="close" type="button" aria-label="Close chat">
            <svg aria-hidden="true" focusable="false" viewBox="0 0 24 24" width="20" height="20">
              <path fill="currentColor" d="M18.3 5.7 12 12l6.3 6.3-1.4 1.4L10.6 13.4 4.3 19.7 2.9 18.3 9.2 12 2.9 5.7l1.4-1.4 6.3 6.3 6.3-6.3z"/>
            </svg>
          </button>
        </header>
        <p class="notice">
          I'm an AI assistant that answers from DSU's website, and I can make mistakes. Don't
          share personal information. For official decisions, contact the DSU office that
          handles your question.
        </p>
        <div class="messages" role="log" aria-live="polite" aria-label="Conversation"></div>
        <form class="form" novalidate>
          <label class="sr-only" for="question">Your question</label>
          <textarea id="question" rows="2" placeholder="Ask a question about DSU"
            aria-describedby="count form-error"></textarea>
          <p class="form-error" id="form-error" role="alert"></p>
          <div class="form-row">
            <span class="count" id="count"></span>
            <button class="send" type="submit">Send</button>
          </div>
        </form>
      </section>
    </div>`;

  function mount(options) {
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
    const widget = $(".widget");
    const launcher = $(".launcher");
    const panel = $(".panel");
    const messages = $(".messages");
    const form = $(".form");
    const input = $("#question");
    const count = $("#count");
    const formError = $("#form-error");
    const send = $(".send");
    let busy = false;

    $(".launcher-label").textContent = options.title;
    $(".title").textContent = options.title;
    addBotText(MESSAGES.welcome);
    updateCount();

    function open() {
      panel.hidden = false;
      widget.classList.add("open");
      launcher.setAttribute("aria-expanded", "true");
      input.focus();
    }

    function close() {
      panel.hidden = true;
      widget.classList.remove("open");
      launcher.setAttribute("aria-expanded", "false");
      launcher.focus();
    }

    launcher.addEventListener("click", () => (panel.hidden ? open() : close()));
    $(".close").addEventListener("click", close);
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

      showFormError("");
      addMessage("user", (el) => (el.textContent = question));
      input.value = "";
      updateCount();
      setBusy(true);
      const loading = addMessage("bot loading", (el) => {
        const dots = document.createElement("span");
        dots.className = "dots";
        dots.setAttribute("aria-hidden", "true");
        dots.append(...[1, 2, 3].map(() => document.createElement("span")));
        el.append(dots, document.createTextNode(MESSAGES.loading));
      });

      try {
        const response = await fetch(`${options.apiUrl}/chat`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ question }),
          signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
        });
        const data = await response.json().catch(() => null);
        if (response.ok && data && typeof data.answer === "string") {
          addAnswer(data);
        } else if (response.status === 422 && typeof data?.detail === "string") {
          failed(data.detail, question); // the API's own message, e.g. question too long
        } else if (response.status === 503) {
          failed(MESSAGES.unavailable, question);
        } else {
          failed(MESSAGES.generic, question);
        }
      } catch {
        failed(MESSAGES.network, question); // offline, CORS blocked, or timed out
      } finally {
        loading.remove();
        setBusy(false);
      }
    }

    function setBusy(value) {
      busy = value;
      send.disabled = value;
    }

    function failed(text, question) {
      addMessage("bot error", (el) => {
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

    function addBotText(text) {
      addMessage("bot", (el) => {
        const p = document.createElement("p");
        p.textContent = text;
        el.appendChild(p);
      });
    }

    function addAnswer(data) {
      addMessage("bot", (el) => {
        const answer = document.createElement("div");
        answer.className = "answer";
        answer.appendChild(renderMarkdown(data.answer, document));
        el.appendChild(answer);

        const sources = (Array.isArray(data.sources) ? data.sources : []).filter(
          (s) => s && typeof s.url === "string" && isSafeUrl(s.url)
        );
        if (sources.length) el.appendChild(sourceCards(sources));
      });
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

    /** Appends a message bubble; `fill(el)` adds its content with DOM calls, never HTML. */
    function addMessage(kind, fill) {
      const el = document.createElement("div");
      el.className = `message ${kind}`;
      fill(el);
      messages.appendChild(el); // added whole, so the live region reads it once
      // Scroll to the start of the new message, so a long answer is read from the top.
      messages.scrollTop = el.offsetTop - messages.offsetTop - 16;
      return el;
    }
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
      title: script.dataset.title || "Ask DSU",
      maxChars: Number(script.dataset.maxChars) || 1000, // keep in sync with CHAT_MAX_QUESTION_CHARS
    };
    if (document.body) mount(options);
    else document.addEventListener("DOMContentLoaded", () => mount(options));
  }

  if (typeof module === "object" && module.exports) {
    module.exports = { isSafeUrl, parseMarkdown, renderMarkdown, formatDate }; // for the tests
  }
  if (typeof document !== "undefined") boot();
})();
