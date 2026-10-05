// Tests for saved conversations (Past Conversations). Run with: node --test widget/tests/
//
// The storage functions take the storage object as an argument, so these tests pass a small
// in-memory stand-in for localStorage, and one that throws like a blocked browser does.

const assert = require("node:assert/strict");
const { test } = require("node:test");
const {
  conversationTitle,
  pruneHistory,
  upsertConversation,
  loadHistory,
  saveHistory,
  HISTORY_KEY,
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
