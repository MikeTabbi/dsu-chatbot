// Tests for fluid answers: Show more, the Sources chip, and the New message button.
// Run with: node --test widget/tests/
//
// Each piece takes the elements it works on, so these tests pass small fake elements that keep
// attributes, classes, listeners, and scroll positions. Layout numbers (scrollHeight, offsetTop)
// are set by hand.

const assert = require("node:assert/strict");
const { test } = require("node:test");
const {
  needsShowMore,
  isNearBottom,
  showMore,
  sourcesChip,
  newMessageNotifier,
  COLLAPSED_PX,
} = require("../dsu-chat.js");

function fakeElement(tag) {
  const attributes = {};
  const listeners = {};
  const classes = new Set();
  const el = {
    tag,
    children: [],
    hidden: false,
    id: "",
    textContent: "",
    style: {},
    focused: false,
    setAttribute: (name, value) => (attributes[name] = String(value)),
    getAttribute: (name) => (name in attributes ? attributes[name] : null),
    removeAttribute: (name) => delete attributes[name],
    addEventListener: (type, fn) => (listeners[type] ||= []).push(fn),
    removeEventListener: (type, fn) => {
      listeners[type] = (listeners[type] || []).filter((f) => f !== fn);
    },
    dispatch(type, event = {}) {
      for (const fn of [...(listeners[type] || [])]) fn({ target: el, ...event });
    },
    click() {
      el.dispatch("click");
    },
    focus() {
      el.focused = true;
    },
    append(...nodes) {
      el.children.push(...nodes);
    },
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
      toggle(c, on = !classes.has(c)) {
        if (on) classes.add(c);
        else classes.delete(c);
        return on;
      },
    },
  };
  return el;
}

const fakeDocument = { createElement: fakeElement };

// ---- Show more -----------------------------------------------------------------------------

test("only answers well past the cut-off get Show more", () => {
  assert.equal(needsShowMore(COLLAPSED_PX), false);
  assert.equal(needsShowMore(COLLAPSED_PX + 40), false, "a few hidden lines: just show them");
  assert.equal(needsShowMore(COLLAPSED_PX + 400), true);
});

test("Show more starts collapsed and toggles the answer, labels, and aria-expanded", () => {
  const answer = fakeElement("div");
  answer.id = "answer-1";
  const button = showMore(fakeDocument, answer);

  assert.equal(button.tag, "button");
  assert.equal(button.type, "button");
  assert.equal(button.getAttribute("aria-controls"), "answer-1");
  assert.equal(button.getAttribute("aria-expanded"), "false");
  assert.equal(button.textContent, "Show more");
  assert.ok(answer.classList.contains("collapsed"));

  button.click();
  assert.equal(button.getAttribute("aria-expanded"), "true");
  assert.equal(button.textContent, "Show less");
  assert.ok(!answer.classList.contains("collapsed"));
  assert.equal(answer.style.maxHeight || "", "", "no fixed height, so the answer can keep growing");

  button.click();
  assert.equal(button.getAttribute("aria-expanded"), "false");
  assert.equal(button.textContent, "Show more");
  assert.ok(answer.classList.contains("collapsed"));
});

test("tabbing to a link in the cut-off part opens the answer", () => {
  const answer = fakeElement("div");
  answer.getBoundingClientRect = () => ({ top: 100 });
  const button = showMore(fakeDocument, answer);

  // A link inside the visible part: stays collapsed.
  answer.dispatch("focusin", { target: { getBoundingClientRect: () => ({ bottom: 150 }) } });
  assert.equal(button.getAttribute("aria-expanded"), "false");

  // A link below the cut-off: opens, so focus never sits on hidden text.
  answer.dispatch("focusin", {
    target: { getBoundingClientRect: () => ({ bottom: 100 + COLLAPSED_PX + 30 }) },
  });
  assert.equal(button.getAttribute("aria-expanded"), "true");
  assert.ok(!answer.classList.contains("collapsed"));
});

test("with motion, Show more slides the height and then lets it go", () => {
  const answer = fakeElement("div");
  answer.scrollHeight = 600;
  const button = showMore(fakeDocument, answer, () => true);
  button.click();
  assert.equal(answer.style.maxHeight, "600px", "slides to the full height");
  assert.ok(answer.classList.contains("sizing"));
  answer.dispatch("transitionend");
  assert.equal(answer.style.maxHeight, "", "then no fixed height");
  assert.ok(!answer.classList.contains("sizing"));

  button.click();
  assert.equal(answer.style.maxHeight, `${COLLAPSED_PX}px`, "slides back to the cut-off");
  answer.dispatch("transitionend");
  assert.ok(answer.classList.contains("collapsed"));
  assert.equal(answer.style.maxHeight, "");
});

// ---- Sources chip --------------------------------------------------------------------------

test("the Sources chip counts the cards and hides them until tapped", () => {
  const list = fakeElement("ul");
  list.children.push(fakeElement("li"), fakeElement("li"));
  const block = sourcesChip(fakeDocument, list, "sources-3");
  const [chip, cards] = block.children;

  assert.equal(block.className, "sources-block");
  assert.equal(chip.tag, "button");
  assert.equal(chip.type, "button");
  assert.equal(chip.textContent, "Sources (2)");
  assert.equal(chip.getAttribute("aria-controls"), "sources-3");
  assert.equal(chip.getAttribute("aria-expanded"), "false");
  assert.equal(cards, list);
  assert.equal(list.id, "sources-3");
  assert.equal(list.hidden, true);

  chip.click();
  assert.equal(chip.getAttribute("aria-expanded"), "true");
  assert.equal(list.hidden, false);

  chip.click();
  assert.equal(chip.getAttribute("aria-expanded"), "false");
  assert.equal(list.hidden, true);
});

// ---- New message button --------------------------------------------------------------------

function conversation({ scrollTop = 0, clientHeight = 400, scrollHeight = 400 } = {}) {
  const container = fakeElement("div");
  Object.assign(container, { scrollTop, clientHeight, scrollHeight });
  const button = fakeElement("button");
  button.hidden = true;
  const scrolledTo = [];
  const notifier = newMessageNotifier(container, button, (el) => {
    scrolledTo.push(el);
    container.scrollTop = el.offsetTop - 16;
  });
  return { container, button, scrolledTo, notifier };
}

function message(offsetTop) {
  const el = fakeElement("div");
  el.offsetTop = offsetTop;
  return el;
}

test("near the bottom counts as reading the latest message", () => {
  assert.equal(isNearBottom({ scrollTop: 600, clientHeight: 400, scrollHeight: 1000 }), true);
  assert.equal(isNearBottom({ scrollTop: 560, clientHeight: 400, scrollHeight: 1000 }), true);
  assert.equal(isNearBottom({ scrollTop: 100, clientHeight: 400, scrollHeight: 1000 }), false);
});

test("a new answer is scrolled to its start when the student is following along", () => {
  const { button, scrolledTo, notifier } = conversation();
  const answer = message(700);
  notifier.added(answer, true);
  assert.deepEqual(scrolledTo, [answer]);
  assert.equal(button.hidden, true);
});

test("scrolled up: the view stays put and New message appears, then takes you there", () => {
  const { container, button, scrolledTo, notifier } = conversation({ scrollHeight: 1400 });
  const first = message(900);
  const second = message(1200);
  notifier.added(first, false);
  notifier.added(second, false);
  assert.deepEqual(scrolledTo, [], "didn't jump");
  assert.equal(button.hidden, false);
  assert.equal(container.scrollTop, 0);

  button.click();
  assert.deepEqual(scrolledTo, [first], "goes to the first message not seen yet");
  assert.equal(button.hidden, true);
  assert.equal(first.focused, true, "focus moves to it for keyboard and screen-reader users");
  assert.equal(first.getAttribute("tabindex"), "-1");
  first.dispatch("blur");
  assert.equal(first.getAttribute("tabindex"), null);
});

test("scrolling down to the new message hides the button", () => {
  const { container, button, notifier } = conversation({ scrollHeight: 1400 });
  notifier.added(message(900), false);
  container.scrollTop = 300; // 300 + 400 < 900 + 40: not there yet
  container.dispatch("scroll");
  assert.equal(button.hidden, false);
  container.scrollTop = 600;
  container.dispatch("scroll");
  assert.equal(button.hidden, true);
});

test("the typing indicator never shows New message", () => {
  const { button, scrolledTo, notifier } = conversation({ scrollHeight: 1400 });
  notifier.added(message(900), false, false);
  assert.equal(button.hidden, true);
  assert.deepEqual(scrolledTo, []);
});

test("sending a question scrolls to it and clears New message", () => {
  const { button, scrolledTo, notifier } = conversation({ scrollHeight: 1400 });
  notifier.added(message(900), false);
  const question = message(1100);
  notifier.added(question, true);
  assert.deepEqual(scrolledTo, [question]);
  assert.equal(button.hidden, true);
});
