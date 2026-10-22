// Tests for saved conversations (Past Conversations). Run with: node --test widget/tests/
//
// The storage functions take the storage object as an argument, so these tests pass small
// in-memory stand-ins for localStorage and sessionStorage, and one that throws like a blocked
// browser does.

const assert = require("node:assert/strict");
const { test } = require("node:test");
const {
  conversationTitle,
  timeAgo,
  pruneHistory,
  upsertConversation,
  loadHistory,
  saveHistory,
  openHistory,
  setKeepOnDevice,
  HISTORY_KEY,
  KEEP_KEY,
  LEGACY_KEY,
  MAX_CONVERSATIONS,
  MAX_AGE_DAYS,
} = require("../dsu-chat.js");

const DAY = 24 * 60 * 60 * 1000;
const NOW = Date.UTC(2026, 9, 5, 12, 0, 0);

function memoryStorage(initial = {}) {
  const data = new Map(Object.entries(initial));
  return {
    data,
    getItem: (key) => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => data.set(key, String(value)),
    removeItem: (key) => data.delete(key),
  };
}

const blockedStorage = {
  getItem() {
    throw new Error("SecurityError");
  },
  setItem() {
    throw new Error("SecurityError");
  },
  removeItem() {
    throw new Error("SecurityError");
  },
};

function convo(id, updated, question = `Question ${id}`) {
  return {
    id,
    created: updated,
    updated,
    title: question,
    messages: [
      { role: "user", text: question },
      { role: "bot", data: { answer: "An answer.", sources: [] } },
    ],
  };
}

test("the title is the first question, shortened", () => {
  assert.equal(conversationTitle([{ role: "user", text: "  Where is   the library? " }]), "Where is the library?");
  const long = "a".repeat(200);
  const title = conversationTitle([{ role: "user", text: long }]);
  assert.equal(title.length, 80);
  assert.ok(title.endsWith("…"));
  assert.equal(conversationTitle([{ role: "bot", data: { answer: "hi" } }]), "Conversation");
});

test("keeps at most MAX_CONVERSATIONS, newest first", () => {
  const items = Array.from({ length: MAX_CONVERSATIONS + 5 }, (_, i) => convo(`c${i}`, NOW - i * 1000));
  const kept = pruneHistory(items.reverse(), NOW);
  assert.equal(kept.length, MAX_CONVERSATIONS);
  assert.equal(kept[0].id, "c0");
  assert.equal(kept.at(-1).id, `c${MAX_CONVERSATIONS - 1}`);
});

test("drops conversations older than MAX_AGE_DAYS and broken entries", () => {
  const items = [
    convo("fresh", NOW - DAY),
    convo("old", NOW - (MAX_AGE_DAYS + 1) * DAY),
    { id: "empty", updated: NOW, messages: [] },
    { id: 7, updated: NOW, messages: [{ role: "user", text: "x" }] },
    null,
    "junk",
  ];
  assert.deepEqual(pruneHistory(items, NOW).map((c) => c.id), ["fresh"]);
  assert.deepEqual(pruneHistory("not a list", NOW), []);
});

test("an updated conversation moves to the front without a duplicate", () => {
  const items = [convo("a", NOW - 1000), convo("b", NOW - 2000)];
  const b = { ...items[1], updated: NOW };
  const result = upsertConversation(items, b, NOW);
  assert.deepEqual(result.map((c) => c.id), ["b", "a"]);
});

test("saves and loads the list", () => {
  const storage = memoryStorage();
  const items = [convo("a", NOW - 1000), convo("b", NOW - 2000)];
  assert.equal(saveHistory(storage, items), true);
  assert.deepEqual(loadHistory(storage, NOW).map((c) => c.id), ["a", "b"]);
  // An empty list removes the key instead of storing [].
  saveHistory(storage, []);
  assert.equal(storage.getItem(HISTORY_KEY), null);
});

test("moves the single conversation older versions saved into the list", () => {
  const legacy = {
    updated: NOW - DAY,
    messages: [
      { role: "user", text: "How do I get a meal plan?" },
      { role: "bot", data: { answer: "Here's how.", sources: [] } },
    ],
  };
  const storage = memoryStorage({ [LEGACY_KEY]: JSON.stringify(legacy) });
  const items = loadHistory(storage, NOW);
  assert.equal(items.length, 1);
  assert.equal(items[0].title, "How do I get a meal plan?");
  assert.deepEqual(items[0].messages, legacy.messages);
  assert.equal(storage.getItem(LEGACY_KEY), null, "old key removed");
  assert.ok(storage.getItem(HISTORY_KEY), "new list written");
  // Loading again doesn't duplicate it.
  assert.equal(loadHistory(storage, NOW).length, 1);
});

test("expired conversations are removed from storage on load", () => {
  const storage = memoryStorage();
  saveHistory(storage, [convo("old", NOW - (MAX_AGE_DAYS + 2) * DAY)]);
  assert.deepEqual(loadHistory(storage, NOW), []);
  assert.equal(storage.getItem(HISTORY_KEY), null);
});

test("blocked or corrupt storage never throws", () => {
  assert.deepEqual(loadHistory(blockedStorage, NOW), []);
  assert.equal(saveHistory(blockedStorage, [convo("a", NOW)]), false);
  const corrupt = memoryStorage({ [HISTORY_KEY]: "{not json" });
  assert.deepEqual(loadHistory(corrupt, NOW), []);
});

test("times are general: minutes, hours, days", () => {
  const MIN = 60 * 1000;
  assert.equal(timeAgo(NOW - 20 * 1000, NOW), "Just now");
  assert.equal(timeAgo(NOW - MIN, NOW), "1 Min Ago");
  assert.equal(timeAgo(NOW - 59 * MIN, NOW), "59 Mins Ago");
  assert.equal(timeAgo(NOW - 60 * MIN, NOW), "1 Hr Ago");
  assert.equal(timeAgo(NOW - 5 * 60 * MIN, NOW - 1), "4 Hrs Ago");
  assert.equal(timeAgo(NOW - DAY, NOW), "1 Day Ago");
  assert.equal(timeAgo(NOW - 3 * DAY - 5 * 60 * MIN, NOW), "3 Days Ago");
  assert.equal(timeAgo(NOW + MIN, NOW), "Just now", "a clock that's slightly off never shows a negative time");
});

test("conversations are kept for 7 days", () => {
  assert.equal(MAX_AGE_DAYS, 7);
  const items = [convo("six", NOW - 6 * DAY), convo("eight", NOW - 8 * DAY)];
  assert.deepEqual(pruneHistory(items, NOW).map((c) => c.id), ["six"]);
});

test("by default, conversations are in sessionStorage", () => {
  const local = memoryStorage();
  const session = memoryStorage();
  saveHistory(session, [convo("a", NOW - 1000)]);
  const { keep, items } = openHistory(local, session, NOW);
  assert.equal(keep, false);
  assert.deepEqual(items.map((c) => c.id), ["a"]);
  assert.equal(local.data.size, 0, "nothing written to localStorage");
});

test("conversations an older version left in localStorage move to sessionStorage", () => {
  const legacy = { updated: NOW - DAY, messages: [{ role: "user", text: "Old one" }] };
  const local = memoryStorage({ [LEGACY_KEY]: JSON.stringify(legacy) });
  saveHistory(local, [convo("a", NOW - 1000)]);
  local.setItem(LEGACY_KEY, JSON.stringify(legacy));
  const session = memoryStorage();
  saveHistory(session, [convo("a", NOW - 1000), convo("b", NOW - 500)]);

  const { keep, items } = openHistory(local, session, NOW);
  assert.equal(keep, false);
  // "a" isn't duplicated; the legacy conversation is added; newest first.
  assert.equal(items.length, 3);
  assert.deepEqual(items.slice(0, 2).map((c) => c.id), ["b", "a"]);
  assert.equal(items[2].title, "Old one");
  assert.equal(local.getItem(HISTORY_KEY), null, "removed from localStorage");
  assert.equal(local.getItem(LEGACY_KEY), null, "legacy key removed");
  assert.equal(loadHistory(session, NOW).length, 3, "written to sessionStorage");
});

test("turning keep on moves conversations to localStorage", () => {
  const local = memoryStorage();
  const session = memoryStorage();
  const items = [convo("a", NOW - 1000)];
  saveHistory(session, items);

  assert.equal(setKeepOnDevice(local, session, true, items), true);
  assert.equal(local.getItem(KEEP_KEY), "yes");
  assert.deepEqual(loadHistory(local, NOW).map((c) => c.id), ["a"]);
  assert.equal(session.getItem(HISTORY_KEY), null, "removed from sessionStorage");

  // The next visit (a new session) reads them from localStorage.
  const reopened = openHistory(local, memoryStorage(), NOW);
  assert.equal(reopened.keep, true);
  assert.deepEqual(reopened.items.map((c) => c.id), ["a"]);
});

test("turning keep off moves conversations back to sessionStorage", () => {
  const local = memoryStorage({ [KEEP_KEY]: "yes" });
  const session = memoryStorage();
  const items = [convo("a", NOW - 1000)];
  saveHistory(local, items);

  assert.equal(setKeepOnDevice(local, session, false, items), true);
  assert.equal(local.getItem(KEEP_KEY), null);
  assert.equal(local.getItem(HISTORY_KEY), null, "removed from localStorage");
  assert.deepEqual(loadHistory(session, NOW).map((c) => c.id), ["a"]);
  assert.equal(openHistory(local, session, NOW).keep, false);
});

test("keep can't be turned on when the browser blocks storage", () => {
  const session = memoryStorage();
  const items = [convo("a", NOW - 1000)];
  saveHistory(session, items);
  assert.deepEqual(openHistory(blockedStorage, session, NOW).items.map((c) => c.id), ["a"]);
  assert.equal(setKeepOnDevice(blockedStorage, session, true, items), false);
  assert.ok(session.getItem(HISTORY_KEY), "left where it was");

  // The widget's stand-in for blocked storage keeps nothing, so the setting can't stick either.
  const standIn = { getItem: () => null, setItem() {}, removeItem() {} };
  assert.equal(setKeepOnDevice(standIn, session, true, items), false);
  assert.ok(session.getItem(HISTORY_KEY), "left where it was");
});
