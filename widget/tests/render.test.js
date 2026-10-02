// Tests for the widget's Markdown rendering. Run with: node --test widget/tests/
//
// renderMarkdown() only builds nodes through the `doc` it is given, so these tests pass a tiny
// fake document that records every element it creates. If answer text could ever become HTML,
// an <img>, <script>, or javascript: link would show up here as a created element or attribute.

const assert = require("node:assert/strict");
const { test } = require("node:test");
const {
  isSafeUrl,
  parseMarkdown,
  renderMarkdown,
  formatDate,
  feedbackRequest,
} = require("../dsu-chat.js");

const ALLOWED_TAGS = new Set(["p", "ul", "ol", "li", "strong", "em", "a", "br", "span"]);

function fakeDocument() {
  const created = [];
  const node = (props) => ({ children: [], appendChild(c) { this.children.push(c); return c; }, ...props });
  return {
    created,
    createDocumentFragment: () => node({ tag: "#fragment" }),
    createTextNode: (text) => ({ text }),
    createElement(tag) {
      const el = node({ tag, attributes: {}, className: "" });
      el.setAttribute = (name, value) => (el.attributes[name] = value);
      created.push(el);
      return el;
    },
  };
}

const escape = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

// Serializes the fake tree the way a browser would show it, escaping all text.
function toHtml(n) {
  if ("text" in n) return escape(n.text);
  const inner = n.children.map(toHtml).join("");
  if (n.tag === "#fragment") return inner;
  if (n.className === "sr-only") return "";
  const attrs = Object.entries(n.attributes).map(([k, v]) => ` ${k}="${escape(v)}"`).join("");
  return `<${n.tag}${attrs}>${inner}</${n.tag}>`;
}

function render(text) {
  const doc = fakeDocument();
  const html = toHtml(renderMarkdown(text, doc));
  return { html, created: doc.created };
}

test("HTML in the answer is shown as plain text", () => {
  const { html, created } = render('Hi <img src=x onerror="alert(1)"> <script>alert(2)</script>');
  assert.equal(html, '<p>Hi &lt;img src=x onerror="alert(1)"&gt; &lt;script&gt;alert(2)&lt;/script&gt;</p>');
  assert.deepEqual(created.map((el) => el.tag), ["p"]);
});

test("javascript: and other non-http links are shown as plain text", () => {
  for (const text of [
    "[click me](javascript:alert(1))",
    "[click me](javascript:alert%281%29)",
    "[click me](JaVaScRiPt:alert%281%29)",
    "[click me](data:text/html;base64,PHNjcmlwdD4=)",
    "[click me](vbscript:msgbox)",
    "[click me](/relative/path)",
    "javascript:alert(1)",
  ]) {
    const { html, created } = render(text);
    assert.ok(!created.some((el) => el.tag === "a"), `made a link from ${text}`);
    assert.equal(html, `<p>${escape(text)}</p>`);
  }
});

test("only safe tags are ever created, whatever the input", () => {
  const nasty = [
    "**<b>bold</b>**",
    "- <iframe src=https://evil.example></iframe>",
    "[<img src=x>](https://www.desu.edu)",
    "1. [x](https://www.desu.edu\" onclick=\"alert(1))",
    "<a href=\"javascript:alert(1)\">x</a>",
  ].join("\n");
  const { created } = render(nasty);
  for (const el of created) {
    assert.ok(ALLOWED_TAGS.has(el.tag), `created <${el.tag}>`);
    for (const name of Object.keys(el.attributes)) {
      assert.ok(["href", "target", "rel", "start"].includes(name), `set ${name}`);
    }
    if (el.attributes.href) assert.ok(isSafeUrl(el.attributes.href), el.attributes.href);
  }
});

test("http(s) links open in a new tab without access to the page", () => {
  const { html } = render("See [Residential Halls](https://www.desu.edu/halls).");
  assert.equal(
    html,
    '<p>See <a href="https://www.desu.edu/halls" target="_blank" rel="noopener noreferrer">' +
      "Residential Halls</a>.</p>"
  );
});

test("bare URLs become links without trailing punctuation", () => {
  const { html } = render("Visit https://www.desu.edu/admissions.");
  assert.equal(
    html,
    '<p>Visit <a href="https://www.desu.edu/admissions" target="_blank" ' +
      'rel="noopener noreferrer">https://www.desu.edu/admissions</a>.</p>'
  );
});

test("bold, italic, lists, and paragraphs", () => {
  const { html } = render(
    "**Carpeted halls:**\n- Tubman-Lawson\n- *Warren-Franklin*\n\nSteps:\n1. Apply\n2. Pay"
  );
  assert.equal(
    html,
    "<p><strong>Carpeted halls:</strong></p>" +
      "<ul><li>Tubman-Lawson</li><li><em>Warren-Franklin</em></li></ul>" +
      "<p>Steps:</p><ol><li>Apply</li><li>Pay</li></ol>"
  );
});

test("a numbered list split by blank lines keeps its numbering", () => {
  const blocks = parseMarkdown("1. Apply\n\n2. Pay");
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].items.length, 2);
  assert.equal(render("3. Move in").html, '<ol start="3"><li>Move in</li></ol>');
});

test("unclosed markers stay as text", () => {
  assert.equal(render("5 * 3 and **open").html, "<p>5 * 3 and **open</p>");
});

test("thumbs buttons post the request ID and rating to /feedback", () => {
  const [url, init] = feedbackRequest("https://api.example.edu", "abc123", "down");
  assert.equal(url, "https://api.example.edu/feedback");
  assert.equal(init.method, "POST");
  assert.equal(init.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(init.body), { request_id: "abc123", rating: "down" });
});

test("dates for source cards", () => {
  assert.equal(formatDate("2022-03-04"), "March 4, 2022");
  assert.equal(formatDate(null), null);
  assert.equal(formatDate("<b>soon</b>"), null);
});
